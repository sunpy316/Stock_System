# -*- coding: utf-8 -*-
"""FastAPI 服务：筛选、个股详情、K 线、策略与运维数据。

启动：
    python -m uvicorn stock_system.api.main:app --host 127.0.0.1 --port 8800
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Any, Dict, Optional
from urllib.parse import quote

import pandas as pd
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import config, db
from ..meta.pinyin import initials
from ..screener import ScreenError, run_screen
from ..screener import cross_section
from ..screener.templates import INDICATOR_META, TEMPLATES
from .exporter import export_run

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")

# ---------------- 定时调度 ----------------
scheduler = BackgroundScheduler(timezone="Asia/Shanghai")


def _run_strategy_job(sid: int):
    """定时任务：执行策略并落库，失败只记日志不抛出。"""
    try:
        df = db.query_df(
            "SELECT condition_json, filter_json FROM dim_strategy WHERE strategy_id = :i",
            {"i": sid},
        )
        if df.empty:
            print(f"[scheduler] 策略 {sid} 已不存在，跳过", flush=True)
            return
        cond = df.iloc[0]["condition_json"]
        filt = df.iloc[0]["filter_json"]
        if isinstance(cond, str):
            cond = json.loads(cond)
        if isinstance(filt, str):
            filt = json.loads(filt)
        res = run_screen(cond, filters=filt, limit=500, strategy_id=sid, run_type="schedule")
        print(f"[scheduler] 策略 {sid} 执行完成：候选 {res['candidate']} 命中 {res['hit']}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[scheduler] 策略 {sid} 执行失败：{exc}", flush=True)


# ---------------- 每日数据更新 ----------------
# 收盘后自动跑：行情增量 → 补漏 → 财务增量 → 指标重算。
# 时间改这里即可（24 小时制，Asia/Shanghai）。
# 定 18:00 而不是 16:00：腾讯财经的当日 K 线通常 17:30 之后才完整，
# 16:00 起跑会让先处理的一两千只拿到空结果（11-17、11-18 连续两天踩到）。
DAILY_UPDATE_HOUR = 18
DAILY_UPDATE_MINUTE = 0

# 补漏参数：同步结束后检查当日覆盖率，把空的补一遍
GAP_FILL_MAX_ROUNDS = 2      # 最多补几轮
GAP_FILL_SLEEP = 90          # 两轮之间等待秒数（给数据源一点发布时间）
GAP_FILL_MAX_MISS_RATIO = 0.5  # 缺失超过一半 → 判定数据源整体未就绪，不硬补


def _is_trading_day(d) -> bool:
    """日历表只收录交易日，查不到即非交易日。"""
    df = db.query_df(
        "SELECT is_open FROM dim_trade_calendar WHERE cal_date = :d", {"d": d}
    )
    return (not df.empty) and int(df.iloc[0]["is_open"]) == 1



def _missing_codes(d):
    """返回 (缺行情的代码, 有行情但缺复权的代码, 在市总数)。"""
    df = db.query_df(
        "SELECT s.stock_code, q.stock_code AS qc, a.stock_code AS ac "
        "FROM dim_stock s "
        "LEFT JOIN fact_daily_quote q ON q.stock_code = s.stock_code AND q.trade_date = :d "
        "LEFT JOIN fact_daily_quote_adj a ON a.stock_code = s.stock_code AND a.trade_date = :d "
        "WHERE s.list_status = 'L'",
        {"d": d},
    )
    if df.empty:
        return [], [], 0
    miss_q = df[df["qc"].isna()]["stock_code"].astype(str).tolist()
    miss_a = df[df["qc"].notna() & df["ac"].isna()]["stock_code"].astype(str).tolist()
    return miss_q, miss_a, len(df)


def _fill_daily_gaps(d, tag=""):
    """当日补漏：数据源发布有延迟，先跑的股票可能拿到空结果。

    同步结束后检查覆盖率，把缺行情/缺复权的分别补一遍，
    避免整天数据残缺（历史上前端看到的就是「任务成功但行数少一半」）。
    """
    for rnd in range(1, GAP_FILL_MAX_ROUNDS + 1):
        miss_q, miss_a, total = _missing_codes(d)
        if not miss_q and not miss_a:
            print(f"[daily]{tag} 当日覆盖完整（{total} 只）", flush=True)
            return
        if total and len(miss_q) > total * GAP_FILL_MAX_MISS_RATIO:
            print(
                f"[daily]{tag} 第 {rnd} 轮：缺 {len(miss_q)}/{total}，超过阈值，"
                f"判定数据源未就绪，放弃补漏",
                flush=True,
            )
            return

        ds = d.strftime("%Y%m%d") if hasattr(d, "strftime") else str(d).replace("-", "")
        print(
            f"[daily]{tag} 第 {rnd} 轮补漏：缺行情 {len(miss_q)} 只，缺复权 {len(miss_a)} 只",
            flush=True,
        )
        if miss_q:
            r = sync_daily_quote(
                codes=miss_q, start=ds, end=ds, chunk_size=300,
                progress_file=f"logs/.fill_quote_{ds}",
            )
            print(f"[daily]{tag} 补行情结果：ok={r.get('ok')} fail={r.get('fail')}", flush=True)
        if miss_a:
            r = sync_daily_quote(
                codes=miss_a, start=ds, end=ds, chunk_size=300,
                progress_file=f"logs/.fill_adj_{ds}",
            )
            print(f"[daily]{tag} 补复权结果：ok={r.get('ok')} fail={r.get('fail')}", flush=True)
        if rnd < GAP_FILL_MAX_ROUNDS:
            time.sleep(GAP_FILL_SLEEP)


def _run_daily_update():
    """每日数据更新，供 APScheduler 调用。

    非交易日直接跳过——增量采集对每只股票都要发一次请求，
    在周末空跑一轮要花约一小时，纯属浪费。
    """
    today = date.today()
    if not _is_trading_day(today):
        print(f"[daily] {today} 非交易日，跳过", flush=True)
        return

    # 时间闸：A 股 15:00 收盘，数据源一般 15:10 之后才生成当日 K 线。
    # 早于这个点同步"当日"只会白跑几千次请求（每只股票一次），还会撞上东财限流。
    now = datetime.now()
    ready = now.replace(
        hour=config.DAILY_SYNC_READY_HOUR,
        minute=config.DAILY_SYNC_READY_MINUTE,
        second=0,
        microsecond=0,
    )
    if now < ready:
        print(
            f"[daily] 现在 {now:%H:%M}，早于 {ready:%H:%M}，当日行情尚未生成，跳过本轮",
            flush=True,
        )
        return

    t0 = time.time()
    try:
        # 三个采集函数内部各自写 sys_task_log，界面上能看到逐段进度。
        # 日线同步带分片+断点：撞限流时整批熔断而不是逐只失败，下次从断点续。
        sync_daily_quote(chunk_size=300, progress_file="logs/.quote_progress_daily")
        _fill_daily_gaps(today)
        sync_financial(skip_existing=True)
        calc_indicator()
        cross_section.invalidate()  # 数据变了，横截面缓存必须失效
        # 采集完立即预热：用户下班后打开页面就是热的，不必等第一次冷构建
        cross_section.warmup()
        print(f"[daily] {today} 更新完成，用时 {int(time.time() - t0)}s", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[daily] 更新失败（{int(time.time() - t0)}s）：{exc}", flush=True)


def load_jobs():
    """重建全部定时任务：策略 cron + 每日数据更新。"""
    scheduler.remove_all_jobs()
    df = db.query_df(
        "SELECT strategy_id, strategy_name, schedule_cron FROM dim_strategy "
        "WHERE schedule_cron IS NOT NULL AND schedule_cron <> ''"
    )
    for r in df.itertuples(index=False):
        try:
            scheduler.add_job(
                _run_strategy_job,
                CronTrigger.from_crontab(r.schedule_cron, timezone="Asia/Shanghai"),
                args=[int(r.strategy_id)],
                id=f"strategy-{r.strategy_id}",
                replace_existing=True,
                misfire_grace_time=3600,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[scheduler] 策略 {r.strategy_id} cron 非法：{exc}", flush=True)

    # 每日数据更新：放在策略之前跑，保证策略用的是当天数据。
    # remove_all_jobs 会清空，所以必须在这里重新注册，不能只在启动时加一次。
    scheduler.add_job(
        _run_daily_update,
        CronTrigger(hour=DAILY_UPDATE_HOUR, minute=DAILY_UPDATE_MINUTE,
                    timezone="Asia/Shanghai"),
        id="daily-data",
        name="每日数据更新",
        replace_existing=True,
        misfire_grace_time=3600,
        max_instances=1,
    )
    return len(scheduler.get_jobs())


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 上次进程被强杀时，running 记录会残留成僵尸，启动时统一标记为中断
    try:
        with db.engine.begin() as conn:
            from sqlalchemy import text

            n = conn.execute(
                text(
                    "UPDATE sys_task_log SET status='interrupted', end_time=NOW(), "
                    "error_msg='服务重启，任务中断' WHERE status='running'"
                )
            ).rowcount
        if n:
            print(f"[startup] 清理 {n} 条中断任务记录", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[startup] 清理僵尸任务失败：{exc}", flush=True)

    try:
        _ensure_user_table()
    except Exception as exc:  # noqa: BLE001
        print(f"[startup] 初始化用户表失败：{exc}", flush=True)

    scheduler.start()
    try:
        print(f"[scheduler] 已加载 {load_jobs()} 个定时策略", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[scheduler] 启动加载失败：{exc}", flush=True)

    # 后台预热横截面：刚启动时缓存为空，首个筛选请求要现建宽表（冷库上可达 30s+）。
    # 放守护线程里跑，不阻塞启动；与请求撞车时由 cross_section 内部的锁串行化。
    # 延时 5 秒再开始：让首屏那几个轻接口先过，预热是重活（首次 30 秒）。
    # 横截面已落盘快照，之后的启动会直接读盘，几乎瞬间完成。
    def _warm_later():
        time.sleep(5)
        cross_section.warmup()

    threading.Thread(target=_warm_later, name="xsec-warmup", daemon=True).start()

    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="A股智能选股 API", version="0.4.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=config.CORS_ORIGINS, allow_methods=["*"], allow_headers=["*"]
)
# 全历史K线接口一次返回 8000+ 根、约 3.5MB JSON，压完约 0.5MB。
# 放在应用层做，不依赖服务器 nginx 是否配了 gzip_types application/json。
app.add_middleware(GZipMiddleware, minimum_size=1024)


def _hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120000).hex()
    return f"pbkdf2_sha256${salt}${dk}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt, dk_hex = stored.split("$", 2)
        if algo != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120000).hex()
        return hmac.compare_digest(calc, dk_hex)
    except Exception:  # noqa: BLE001
        return False


def _ensure_user_table() -> None:
    from sqlalchemy import text

    with db.engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS sys_user (
                    user_id INT AUTO_INCREMENT PRIMARY KEY,
                    username VARCHAR(64) NOT NULL UNIQUE,
                    password_hash VARCHAR(255) NOT NULL,
                    display_name VARCHAR(64) NOT NULL DEFAULT '',
                    role VARCHAR(16) NOT NULL DEFAULT 'user',
                    is_active TINYINT NOT NULL DEFAULT 1,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
        )
        cnt = conn.execute(text("SELECT COUNT(*) FROM sys_user")).scalar() or 0
        if cnt == 0:
            conn.execute(
                text(
                    "INSERT INTO sys_user (username, password_hash, display_name, role, is_active) "
                    "VALUES (:u, :p, :d, 'admin', 1)"
                ),
                {
                    "u": config.AUTH_USERNAME,
                    "p": _hash_password(config.AUTH_PASSWORD),
                    "d": "系统管理员",
                },
            )


def _issue_token(username: str, role: str = "admin") -> str:
    payload = json.dumps(
        {"sub": username, "role": role, "exp": int(time.time()) + config.AUTH_EXPIRE_SECONDS}
    ).encode()
    b64 = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    sig = hmac.new(config.AUTH_SECRET.encode(), b64.encode(), hashlib.sha256).hexdigest()
    return f"{b64}.{sig}"


def _verify_token(token: str) -> dict | None:
    try:
        b64, sig = token.split(".", 1)
        expected = hmac.new(config.AUTH_SECRET.encode(), b64.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        padded = b64 + "=" * (-len(b64) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded))
        if int(data.get("exp", 0)) <= int(time.time()):
            return None
        return data
    except Exception:  # noqa: BLE001
        return None


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    """可选 API 鉴权中间件：STOCK_AUTH_ENABLED=true 时保护所有 /api/v1/* 接口（登录和 OPTIONS 预检除外）。"""
    request.state.user = {"sub": config.AUTH_USERNAME, "role": "admin"}
    if request.method == "OPTIONS":
        return await call_next(request)
    if config.AUTH_ENABLED and request.url.path.startswith("/api/v1/") and request.url.path != "/api/v1/auth/login":
        auth = request.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.startswith("Bearer ") else ""
        payload = _verify_token(token) if token else None
        if not payload:
            origin = request.headers.get("origin", "*")
            return Response(
                content=json.dumps({"detail": "未登录或登录已过期"}, ensure_ascii=False),
                status_code=401,
                media_type="application/json",
                headers={
                    "Access-Control-Allow-Origin": origin,
                    "Access-Control-Allow-Credentials": "true",
                    "Access-Control-Allow-Headers": "*",
                    "Access-Control-Allow-Methods": "*",
                },
            )
        request.state.user = payload
    return await call_next(request)


@app.middleware("http")
async def no_cache_html(request, call_next):
    """HTML 一律要求回源校验。

    index.html 改动频繁（样式/交互），浏览器默认按 Last-Modified 启发式缓存，
    改完刷新仍是旧页面，排查时会误以为是代码没生效。
    带上 no-cache 后浏览器每次都发条件请求，未变更时返回 304，不增加流量。

    用中间件而不是给路由加 header：`StaticFiles` 挂载在 "/" 上，
    实际由它接管 HTML，路由级 header 会被覆盖。
    """
    resp = await call_next(request)
    ctype = resp.headers.get("content-type", "")
    if ctype.startswith("text/html"):
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    return resp


# ---------------- 模型 ----------------
class ScreenRequest(BaseModel):
    condition: Optional[Dict[str, Any]] = None
    trade_date: Optional[str] = None
    filters: Optional[Dict[str, Any]] = None
    limit: int = 200
    persist: bool = True


def _clean(df: pd.DataFrame) -> list:
    """DataFrame → JSON 友好列表。

    必须走 pandas 的 to_json：它会把 NaN 转成 null、日期转成 ISO 字符串。
    手动 where(isna, None) 对 float 列无效（None 会被 dtype 强制回 NaN），
    而 JSON 不允许 NaN，FastAPI 序列化时直接 500。
    """
    if df is None or df.empty:
        return []
    return json.loads(
        df.to_json(orient="records", date_format="iso", force_ascii=False)
    )


def _f(v, digits=4):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        return round(float(v), digits)
    except (TypeError, ValueError):
        return None


# ---------------- 元信息 ----------------
@app.get("/api/v1/meta/data-date")
def meta_data_date():
    d = db.scalar("SELECT MAX(trade_date) FROM fact_indicator_daily")
    if d is None:
        return {"ready": False, "data_date": None, "stocks": 0, "rows": 0, "updated_at": None}
    row = db.query_df(
        "SELECT COUNT(*) AS rows_, COUNT(DISTINCT stock_code) AS stocks, "
        "MAX(updated_at) AS updated_at FROM fact_indicator_daily WHERE trade_date = :d",
        {"d": d},
    )
    r = row.iloc[0]
    return {
        "ready": True,
        "data_date": str(d),
        "stocks": int(r["stocks"] or 0),
        "rows": int(r["rows_"] or 0),
        "updated_at": str(r["updated_at"]) if r["updated_at"] is not None else None,
    }


@app.get("/api/v1/meta/indicators")
def meta_indicators():
    return {
        "items": [{"field": f, "label": l, "group": g} for f, l, g in INDICATOR_META],
        "templates": [
            {"code": k, "name": v["name"], "desc": v["desc"]} for k, v in TEMPLATES.items()
        ],
    }


@app.get("/api/v1/meta/templates")
def meta_templates():
    return {
        "items": [
            {"code": k, "name": v["name"], "desc": v["desc"], "condition": v["condition"]}
            for k, v in TEMPLATES.items()
        ]
    }


# ---------------- 筛选 ----------------
@app.post("/api/v1/screen/run")
def screen_run(req: ScreenRequest):
    try:
        res = run_screen(
            req.condition,
            trade_date=req.trade_date,
            filters=req.filters,
            limit=req.limit,
            persist=req.persist,
        )
    except ScreenError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"筛选执行失败：{exc}")
    return res


@app.post("/api/v1/screen/warmup")
def screen_warmup():
    """手工预热横截面。

    冷启动/大批量补数据后，第一次筛选要现建宽表（线上实测 30s+）。
    调一次这个接口先建好，后续筛选直接命中缓存。
    """
    rows = cross_section.warmup()
    return {"ok": rows is not None, "rows": rows}


@app.post("/api/v1/screen/preview")
def screen_preview(req: ScreenRequest):
    try:
        res = run_screen(
            req.condition, trade_date=req.trade_date, filters=req.filters,
            limit=1, persist=False,
        )
    except ScreenError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"candidate": res["candidate"], "hit": res["hit"], "data_date": res["data_date"]}


# ---------------- 个股 ----------------
_STOCK_INDEX = {"t": 0.0, "rows": []}


def _stock_index():
    """股票检索索引：代码 + 名称 + 拼音首字母，缓存 1 小时。"""
    now = time.time()
    if _STOCK_INDEX["rows"] and now - _STOCK_INDEX["t"] < 3600:
        return _STOCK_INDEX["rows"]
    df = db.query_df("SELECT stock_code, stock_name, board FROM dim_stock")
    rows = []
    for code, name, board in df.itertuples(index=False):
        rows.append({
            "stock_code": str(code),
            "stock_name": str(name),
            "board": None if board is None or board != board else str(board),
            "initials": initials(str(name)),
        })
    _STOCK_INDEX["rows"] = rows
    _STOCK_INDEX["t"] = now
    return rows


@app.get("/api/v1/stocks/search")
def search_stocks(q: str = "", limit: int = 10):
    """按代码、名称、名称首字母模糊检索。

    支持 000001 / 平安 / payh 三种输入；排序优先级：
    代码全等 > 代码或首字母前缀 > 名称或首字母包含。
    """
    key = (q or "").strip().lower()
    if not key:
        return {"items": []}
    limit = max(1, min(int(limit), 30))
    exact, prefix, contains = [], [], []
    for r in _stock_index():
        code, name, ini = r["stock_code"], r["stock_name"], r["initials"]
        if code == key:
            exact.append(r)
        elif code.startswith(key) or ini.startswith(key):
            prefix.append(r)
        elif key in name.lower() or key in ini:
            contains.append(r)
    out = (exact + prefix + contains)[:limit]
    return {"items": [{"stock_code": r["stock_code"], "stock_name": r["stock_name"],
                       "board": r["board"], "initials": r["initials"]} for r in out]}


@app.get("/api/v1/stocks/{code}/kline")
def stock_kline(code: str, days: int = 120):
    # 上限 10000 是为了让「全部历史」能覆盖到 1991 年（000001 有 8509 根）；
    # 详情面板默认仍只取 120 根，长区间只在双击弹窗里按需拉取。
    days = max(20, min(int(days), 10000))
    df = db.query_df(
        f"""
        SELECT a.trade_date, a.open_adj AS open, a.high_adj AS high,
               a.low_adj AS low, a.close_adj AS close, a.volume, a.amount,
               i.ma5, i.ma10, i.ma20, i.ma60, i.ma120, i.ma250,
               i.macd_dif, i.macd_dea, i.macd_bar,
               i.kdj_k, i.kdj_d, i.kdj_j,
               i.rsi6, i.rsi12, i.rsi24,
               i.boll_up, i.boll_mid, i.boll_low,
               i.atr14, i.vol_ratio,
               i.pct_chg_5d, i.pct_chg_20d, i.pct_chg_60d
        FROM fact_daily_quote_adj a
        LEFT JOIN fact_indicator_daily i
               ON i.stock_code = a.stock_code AND i.trade_date = a.trade_date
        WHERE a.stock_code = :c
        ORDER BY a.trade_date DESC
        LIMIT {days}
        """,
        {"c": code},
    )
    if df.empty:
        raise HTTPException(status_code=404, detail=f"{code} 无行情数据")
    # 全历史 8000+ 根时，各字段的小数位对 JSON 体积影响很直接，统一压到 3 位。
    # 必须判数值类型：整列全 NULL 的字段（如某些股票没有 rs_20d）会是 object，
    # 对它调 round() 直接抛 TypeError，接口就 500 了。
    for c in df.columns:
        if c != "trade_date" and pd.api.types.is_numeric_dtype(df[c]):
            df[c] = df[c].round(3)
    df = df.sort_values("trade_date")
    return {"code": code, "days": len(df), "bars": _clean(df)}


@app.get("/api/v1/stocks/{code}/detail")
def stock_detail(code: str):
    info = db.query_df(
        "SELECT stock_code, stock_name, market, board, is_st, list_date, "
        "total_share, float_share, industry_code FROM dim_stock WHERE stock_code = :c",
        {"c": code},
    )
    if info.empty:
        raise HTTPException(status_code=404, detail=f"{code} 不存在")

    quote = db.query_df(
        "SELECT trade_date, close_price, pct_chg, pct_chg_adj, turnover_rate, amount, "
        "limit_status, adj_factor FROM fact_daily_quote WHERE stock_code = :c "
        "ORDER BY trade_date DESC LIMIT 1",
        {"c": code},
    )
    ind = db.query_df(
        "SELECT * FROM fact_indicator_daily WHERE stock_code = :c "
        "ORDER BY trade_date DESC LIMIT 1",
        {"c": code},
    )
    fin = db.query_df(
        "SELECT CAST(report_period AS CHAR) AS report_period, CAST(ann_date AS CHAR) AS ann_date, "
        "ann_date_source, roe, revenue_yoy, net_profit_yoy, gross_margin, debt_ratio, eps, bps "
        "FROM fact_financial WHERE stock_code = :c "
        "ORDER BY report_period DESC, ann_date DESC LIMIT 1",
        {"c": code},
    )

    base = info.iloc[0].to_dict()
    q = quote.iloc[0].to_dict() if not quote.empty else {}
    i = ind.iloc[0].to_dict() if not ind.empty else {}
    f = fin.iloc[0].to_dict() if not fin.empty else {}

    total_share = base.get("total_share")
    close = q.get("close_price")
    total_mv = (float(close) * float(total_share) / 1e8) if close and total_share else None

    for d in (base, q, i, f):
        for k, v in list(d.items()):
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat()

    return {
        "stock": {**base, "total_mv_yi": _f(total_mv, 2)},
        "quote": q,
        "indicator": {k: _f(v, 4) for k, v in i.items()
                      if k not in ("stock_code", "trade_date", "updated_at")},
        "financial": {
            k: (v if isinstance(v, str) else _f(v, 4)) for k, v in f.items()
        },
    }


# ---------------- 策略管理 ----------------
class StrategyIn(BaseModel):
    name: str
    description: str = ""
    condition: Dict[str, Any]
    filters: Optional[Dict[str, Any]] = None
    sort_by: Optional[str] = "score"


@app.get("/api/v1/strategies")
def list_strategies():
    df = db.query_df(
        """
        SELECT s.strategy_id, s.strategy_name, s.description, s.version,
               s.schedule_cron, s.created_at, s.updated_at,
               JSON_LENGTH(s.condition_json, '$.children') AS cond_cnt,
               (SELECT COUNT(*) FROM fact_screen_run r WHERE r.strategy_id = s.strategy_id) AS run_cnt,
               (SELECT MAX(r.data_date) FROM fact_screen_run r WHERE r.strategy_id = s.strategy_id) AS last_date
        FROM dim_strategy s
        ORDER BY s.updated_at DESC
        """
    )
    return {"items": _clean(df)}


@app.get("/api/v1/strategies/{sid}")
def get_strategy(sid: int):
    df = db.query_df(
        "SELECT strategy_id, strategy_name, description, condition_json, filter_json, "
        "sort_config, version, schedule_cron, created_at, updated_at "
        "FROM dim_strategy WHERE strategy_id = :i",
        {"i": sid},
    )
    if df.empty:
        raise HTTPException(status_code=404, detail="策略不存在")
    r = df.iloc[0].to_dict()
    for k in ("created_at", "updated_at"):
        if hasattr(r.get(k), "isoformat"):
            r[k] = r[k].isoformat()
    # MySQL JSON 列经 pandas 取回是字符串，这里统一解析成对象给前端
    for src, dst in (("condition_json", "condition"), ("filter_json", "filters")):
        v = r.pop(src, None)
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except (ValueError, TypeError):
                v = None
        r[dst] = v
    if isinstance(r.get("condition"), dict):
        kids = r["condition"].get("children") or []
        r["cond_cnt"] = len(kids)
        r["nested"] = any(isinstance(c, dict) and "children" in c for c in kids)
    else:
        r["cond_cnt"] = 0
        r["nested"] = False
    return r


@app.post("/api/v1/strategies")
def create_strategy(s: StrategyIn):
    if not s.name.strip():
        raise HTTPException(status_code=400, detail="策略名称不能为空")
    try:
        with db.engine.begin() as conn:
            from sqlalchemy import text

            res = conn.execute(
                text(
                    "INSERT INTO dim_strategy (strategy_name, description, condition_json, "
                    "filter_json, sort_config) VALUES (:n, :d, :c, :f, :s)"
                ),
                {
                    "n": s.name.strip(), "d": s.description[:255],
                    "c": json.dumps(s.condition, ensure_ascii=False),
                    "f": json.dumps(s.filters, ensure_ascii=False) if s.filters else None,
                    "s": s.sort_by,
                },
            )
            return {"strategy_id": res.lastrowid, "ok": True}
    except Exception as exc:  # noqa: BLE001
        if "Duplicate entry" in str(exc):
            raise HTTPException(status_code=409, detail=f"策略名「{s.name}」已存在")
        raise HTTPException(status_code=500, detail=str(exc))


@app.put("/api/v1/strategies/{sid}")
def update_strategy(sid: int, s: StrategyIn):
    if not s.name.strip():
        raise HTTPException(status_code=400, detail="策略名称不能为空")
    try:
        with db.engine.begin() as conn:
            from sqlalchemy import text

            res = conn.execute(
                text(
                    "UPDATE dim_strategy SET strategy_name=:n, description=:d, condition_json=:c, "
                    "filter_json=:f, sort_config=:s, version=version+1 WHERE strategy_id=:i"
                ),
                {
                    "n": s.name.strip(), "d": s.description[:255],
                    "c": json.dumps(s.condition, ensure_ascii=False),
                    "f": json.dumps(s.filters, ensure_ascii=False) if s.filters else None,
                    "s": s.sort_by, "i": sid,
                },
            )
            if res.rowcount == 0:
                raise HTTPException(status_code=404, detail="策略不存在")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        if "Duplicate entry" in str(exc):
            raise HTTPException(status_code=409, detail=f"策略名「{s.name}」已存在")
        raise HTTPException(status_code=500, detail=str(exc))
    return {"ok": True, "strategy_id": sid}


@app.delete("/api/v1/strategies/{sid}")
def delete_strategy(sid: int):
    with db.engine.begin() as conn:
        from sqlalchemy import text

        res = conn.execute(text("DELETE FROM dim_strategy WHERE strategy_id = :i"), {"i": sid})
        if res.rowcount == 0:
            raise HTTPException(status_code=404, detail="策略不存在")
    return {"ok": True}


@app.post("/api/v1/strategies/{sid}/run")
def run_strategy(sid: int, limit: int = 200):
    df = db.query_df(
        "SELECT condition_json, filter_json FROM dim_strategy WHERE strategy_id = :i", {"i": sid}
    )
    if df.empty:
        raise HTTPException(status_code=404, detail="策略不存在")
    cond = df.iloc[0]["condition_json"]
    filt = df.iloc[0]["filter_json"]
    if isinstance(cond, str):
        cond = json.loads(cond)
    if isinstance(filt, str):
        filt = json.loads(filt)
    try:
        return run_screen(cond, filters=filt, limit=limit, strategy_id=sid)
    except ScreenError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ---------------- 自选股 ----------------
class WatchIn(BaseModel):
    stock_code: str
    group_name: str = "默认"
    note: str = ""


@app.get("/api/v1/watchlist")
def list_watchlist():
    df = db.query_df(
        """
        SELECT w.stock_code, w.group_name, w.note, w.added_at,
               s.stock_name, s.board,
               q.close_price, q.pct_chg_adj, q.turnover_rate
        FROM dim_watchlist w
        LEFT JOIN dim_stock s ON s.stock_code = w.stock_code
        LEFT JOIN fact_daily_quote q ON q.stock_code = w.stock_code
             AND q.trade_date = (SELECT MAX(trade_date) FROM fact_daily_quote)
        ORDER BY w.added_at DESC, w.stock_code
        """
    )
    return {"items": _clean(df)}


@app.post("/api/v1/watchlist")
def add_watch(w: WatchIn):
    code = w.stock_code.strip()
    exist = db.scalar("SELECT COUNT(*) FROM dim_stock WHERE stock_code = :c", {"c": code}, default=0)
    if not exist:
        raise HTTPException(status_code=404, detail=f"股票 {code} 不存在")
    with db.engine.begin() as conn:
        from sqlalchemy import text

        conn.execute(
            text(
                "INSERT INTO dim_watchlist (stock_code, group_name, note) VALUES (:c, :g, :n) "
                "ON DUPLICATE KEY UPDATE note = VALUES(note)"
            ),
            {"c": code, "g": w.group_name or "默认", "n": w.note[:255]},
        )
    return {"ok": True}


@app.delete("/api/v1/watchlist/{code}")
def remove_watch(code: str, group_name: str = "默认"):
    with db.engine.begin() as conn:
        from sqlalchemy import text

        conn.execute(
            text("DELETE FROM dim_watchlist WHERE stock_code = :c AND group_name = :g"),
            {"c": code, "g": group_name},
        )
    return {"ok": True}


# ---------------- 采集任务触发 ----------------
import threading

from ..collectors.calendar import sync_calendar
from ..collectors.daily_quote import sync_daily_quote
from ..collectors.financial import sync_financial
from ..collectors.stock_list import sync_share_capital, sync_stock_list
from ..indicators.runner import run as calc_indicator

_TASK_LOCK = threading.Lock()
_TASK_RUNNING = {}


class TaskRunIn(BaseModel):
    task: str
    start: Optional[str] = None
    limit: Optional[int] = None


def _task_spec(task: str, start=None, limit=None):
    """任务名 → (显示名, 可调用)。"""
    spec = {
        "stock_list": ("同步股票列表", lambda: sync_stock_list()),
        "shares": ("补齐股本", lambda: sync_share_capital()),
        "calendar": ("同步交易日历", lambda: sync_calendar()),
        "quote": ("同步日线行情", lambda: sync_daily_quote(start=start, limit=limit)),
        "financial": ("同步财务数据", lambda: sync_financial(skip_existing=True)),
        "financial_full": ("全量刷新财报", lambda: sync_financial(skip_existing=False)),
        "indicator": ("重算技术指标", lambda: calc_indicator(start=start)),
    }
    return spec.get(task)


# 任务链：某任务成功后自动接续下一个（避免手动盯进度）
TASK_CHAIN = {
    "financial": "indicator",  # 财报补齐后必须重算指标，否则 PE/ROE 仍是空
}


def _run_task_thread(key, fn):
    ok = True
    try:
        print(f"[task] {key} 开始", flush=True)
        fn()
        print(f"[task] {key} 完成", flush=True)
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"[task] {key} 失败：{exc}", flush=True)
    finally:
        with _TASK_LOCK:
            _TASK_RUNNING.pop(key, None)
        # 采集写入新数据后，横截面缓存必须失效，否则筛选会读到旧快照
        try:
            cross_section.invalidate()
            # 行情/财务/指标都改了数据，顺手后台预热一份，避免用户随后第一次筛选又吃冷构建
            if ok and key in ("quote", "financial", "indicator"):
                threading.Thread(
                    target=cross_section.warmup, name="xsec-warmup", daemon=True
                ).start()
        except Exception:  # noqa: BLE001
            pass

    nxt = TASK_CHAIN.get(key) if ok else None
    if nxt:
        try:
            _start_task(nxt)
            print(f"[task] {key} 完成，已自动接续 {nxt}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[task] 自动接续 {nxt} 失败：{exc}", flush=True)


def _start_task(key, start=None, limit=None):
    """启动任务线程。任务已在跑则返回 None。"""
    spec = _task_spec(key, start, limit)
    if spec is None:
        raise ValueError(f"未知任务：{key}")
    name, fn = spec
    with _TASK_LOCK:
        th = _TASK_RUNNING.get(key)
        if th and th.is_alive():
            return None
        th = threading.Thread(target=_run_task_thread, args=(key, fn), daemon=True)
        _TASK_RUNNING[key] = th
        th.start()
    return name


@app.post("/api/v1/tasks/run")
def run_task(body: TaskRunIn):
    """异步触发采集任务，立即返回；进度看 /api/v1/tasks。"""
    spec = _task_spec(body.task, body.start, body.limit)
    if spec is None:
        raise HTTPException(status_code=400, detail=f"未知任务：{body.task}")

    name, _ = spec
    started = _start_task(body.task, body.start, body.limit)
    if started is None:
        raise HTTPException(status_code=409, detail=f"「{name}」正在运行，请等待完成")

    return {
        "ok": True,
        "task": body.task,
        "name": started,
        "next": TASK_CHAIN.get(body.task),
    }


# sys_task_log.task_type → 前端按钮的 task key
# 定时任务（APScheduler）不经过 _TASK_RUNNING，只能靠日志表认出来
_TASK_TYPE_TO_KEY = {
    "daily_quote": "quote",
    "financial": "financial",
    "indicator": "indicator",
    "stock_list": "stock_list",
    "share_capital": "shares",
}


@app.get("/api/v1/tasks/status")
def task_status():
    with _TASK_LOCK:
        running = [k for k, t in _TASK_RUNNING.items() if t.is_alive()]

    def _int(v):
        """sys_task_log 的数字列可能为 NULL/NaN，直接 int() 会抛异常。"""
        if v is None:
            return 0
        try:
            if pd.isna(v):
                return 0
        except (TypeError, ValueError):
            pass
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    # 无条件查日志表：定时任务只在这里留痕，内存登记表里没有
    progress = []
    df = db.query_df(
        "SELECT task_type, total_cnt, success_cnt, fail_cnt, start_time "
        "FROM sys_task_log WHERE status = 'running' ORDER BY task_id DESC"
    )
    for r in df.itertuples(index=False):
        total, done = _int(r.total_cnt), _int(r.success_cnt)
        progress.append(
            {
                "task": r.task_type,
                "total": total,
                "done": done,
                "fail": _int(r.fail_cnt),
                "pct": round(done * 100 / total, 1) if total else None,
            }
        )
        key = _TASK_TYPE_TO_KEY.get(str(r.task_type))
        if key and key not in running:
            running.append(key)
    return {"running": running, "count": len(running), "progress": progress}


# ---------------- 运维 ----------------
@app.get("/api/v1/tasks")
def tasks(limit: int = 20):
    df = db.query_df(
        f"SELECT task_id, task_name, task_type, start_time, end_time, status, "
        f"total_cnt, success_cnt, fail_cnt, error_msg FROM sys_task_log "
        f"ORDER BY task_id DESC LIMIT {max(1, min(limit, 100))}"
    )
    return {"items": _clean(df)}


_STATS_CACHE = {"t": 0.0, "v": None}
_STATS_TTL = 600            # 内存缓存 10 分钟
_STATS_MAX_AGE = 24 * 3600  # 磁盘快照最长 24 小时
_STATS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "cache", "stats.json",
)


def _stats_from_disk():
    """读磁盘快照，但最后一次采集任务跑过之后就作废。

    COUNT(*) 要扫 1300 万行，线上单次约 50 秒。写盘后重启也能秒开，
    代价只是数据更新后重算一次。
    """
    try:
        if not os.path.exists(_STATS_FILE):
            return None
        if time.time() - os.path.getmtime(_STATS_FILE) > _STATS_MAX_AGE:
            return None
        last_task = db.scalar(
            "SELECT UNIX_TIMESTAMP(MAX(end_time)) FROM sys_task_log "
            "WHERE status IN ('success','partial')", default=0
        ) or 0
        if os.path.getmtime(_STATS_FILE) < float(last_task):
            return None
        with open(_STATS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def _stats_to_disk(v):
    try:
        os.makedirs(os.path.dirname(_STATS_FILE), exist_ok=True)
        tmp = _STATS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(v, f)
        os.replace(tmp, _STATS_FILE)
    except Exception:  # noqa: BLE001
        pass


@app.get("/api/v1/stats")
def stats():
    """首页统计。两级缓存：内存 10 分钟、磁盘 24 小时（采集后自动失效）。"""
    now = time.time()
    if _STATS_CACHE["v"] is not None and now - _STATS_CACHE["t"] < _STATS_TTL:
        return _STATS_CACHE["v"]
    disk = _stats_from_disk()
    if disk is not None:
        _STATS_CACHE["t"] = now
        _STATS_CACHE["v"] = disk
        return disk
    v = {
        "stocks": db.scalar("SELECT COUNT(*) FROM dim_stock", default=0),
        "quote_rows": db.scalar("SELECT COUNT(*) FROM fact_daily_quote", default=0),
        # 原先用 COUNT(DISTINCT stock_code) 扫 1300 万行，改成数维表，
        # 语义等价（每只股票都有行情），耗时从秒级降到毫秒级
        "quote_stocks": db.scalar("SELECT COUNT(*) FROM dim_stock", default=0),
        "indicator_rows": db.scalar("SELECT COUNT(*) FROM fact_indicator_daily", default=0),
        "financial_stocks": db.scalar("SELECT COUNT(DISTINCT stock_code) FROM fact_financial", default=0),
        "screen_runs": db.scalar("SELECT COUNT(*) FROM fact_screen_run", default=0),
        "dq_issues": db.scalar("SELECT COUNT(*) FROM sys_data_quality", default=0),
    }
    _STATS_CACHE["t"] = now
    _STATS_CACHE["v"] = v
    _stats_to_disk(v)
    return v


@app.get("/api/v1/dq/issues")
def dq_issues(limit: int = 50):
    df = db.query_df(
        f"SELECT issue_id, rule_code, stock_code, trade_date, detail, created_at "
        f"FROM sys_data_quality ORDER BY issue_id DESC LIMIT {max(1, min(limit, 200))}"
    )
    return {"items": _clean(df)}


@app.get("/api/v1/stocks/{code}/financials")
def stock_financials(code: str, periods: int = 8):
    """最近若干报告期的财务序列，用于趋势图。"""
    periods = max(4, min(int(periods), 20))
    df = db.query_df(
        f"""
        SELECT CAST(report_period AS CHAR) AS report_period,
               revenue, net_profit, revenue_yoy, net_profit_yoy, roe, gross_margin
        FROM fact_financial
        WHERE stock_code = :c
        ORDER BY report_period DESC, ann_date DESC
        LIMIT {periods * 3}
        """,
        {"c": code},
    )
    if df.empty:
        return {"code": code, "items": []}

    # 同一报告期可能有更正公告，保留最新一条后按时间升序
    df = df.drop_duplicates(subset=["report_period"]).head(periods)
    df = df.sort_values("report_period")

    items = []
    for r in df.itertuples(index=False):
        rev, npf = _f(r.revenue, 2), _f(r.net_profit, 2)
        items.append(
            {
                "period": r.report_period,
                "revenue_yi": round(rev / 1e8, 2) if rev is not None else None,
                "net_profit_yi": round(npf / 1e8, 2) if npf is not None else None,
                "revenue_yoy": _f(r.revenue_yoy, 2),
                "net_profit_yoy": _f(r.net_profit_yoy, 2),
                "roe": _f(r.roe, 2),
                "gross_margin": _f(r.gross_margin, 2),
            }
        )
    return {"code": code, "items": items}


# ---------------- 定时执行 ----------------
class ScheduleIn(BaseModel):
    cron: Optional[str] = None


@app.put("/api/v1/strategies/{sid}/schedule")
def set_schedule(sid: int, body: ScheduleIn):
    """设置或取消策略定时。cron 为 5 段式（分 时 日 月 周），留空即取消。"""
    cron = (body.cron or "").strip() or None
    if cron:
        try:
            CronTrigger.from_crontab(cron, timezone="Asia/Shanghai")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"cron 表达式非法：{exc}")

    with db.engine.begin() as conn:
        from sqlalchemy import text

        res = conn.execute(
            text("UPDATE dim_strategy SET schedule_cron = :c WHERE strategy_id = :i"),
            {"c": cron, "i": sid},
        )
        if res.rowcount == 0:
            raise HTTPException(status_code=404, detail="策略不存在")

    return {"ok": True, "cron": cron, "jobs": load_jobs()}


@app.get("/api/v1/scheduler")
def scheduler_status():
    jobs = []
    for j in scheduler.get_jobs():
        if j.id == "daily-data":
            label = "每日数据更新"
            human = (f"每天 {DAILY_UPDATE_HOUR:02d}:{DAILY_UPDATE_MINUTE:02d} · "
                     f"行情增量 → 补漏 → 财务 → 指标，非交易日跳过")
        else:
            m = re.match(r"strategy-(\d+)$", j.id)
            label = f"策略 #{m.group(1)} 定时执行" if m else j.id
            human = "按策略设定的 cron 执行"
        jobs.append(
            {
                "id": j.id,
                "name": j.name,
                "label": label,
                "cron_human": human,
                "next_run": str(j.next_run_time) if j.next_run_time else None,
                "trigger": str(j.trigger),
            }
        )
    return {"running": scheduler.running, "count": len(jobs), "jobs": jobs}


# ---------------- 导出 ----------------
@app.get("/api/v1/screen/runs/{run_id}/export")
def export_screen_run(run_id: int):
    try:
        data, filename = export_run(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"导出失败：{exc}")

    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": (
                "attachment; filename=result.xlsx; "
                f"filename*=UTF-8''{quote(filename)}"
            )
        },
    )


@app.get("/api/v1/screen/runs/diff")
def screen_run_diff(a: int, b: int):
    """对比两次执行：共同命中 / 仅 A 命中 / 仅 B 命中。"""

    def meta(rid):
        df = db.query_df(
            "SELECT r.run_id, CAST(r.data_date AS CHAR) AS data_date, r.hit_cnt, "
            "CAST(r.created_at AS CHAR) AS created_at, s.strategy_name "
            "FROM fact_screen_run r LEFT JOIN dim_strategy s ON s.strategy_id = r.strategy_id "
            "WHERE r.run_id = :i",
            {"i": rid},
        )
        if df.empty:
            raise HTTPException(status_code=404, detail=f"执行记录 {rid} 不存在")
        return df.iloc[0].to_dict()

    def codes(rid):
        df = db.query_df("SELECT stock_code FROM fact_screen_result WHERE run_id = :i", {"i": rid})
        return set(df["stock_code"]) if not df.empty else set()

    ma, mb = meta(a), meta(b)
    ca, cb = codes(a), codes(b)
    both, only_a, only_b = ca & cb, ca - cb, cb - ca

    info = {}
    all_codes = list(both | only_a | only_b)
    if all_codes:
        in_list = ",".join(f"'{c}'" for c in all_codes)
        df = db.query_df(
            f"""
            SELECT s.stock_code, s.stock_name, s.board,
                   q.close_price, q.pct_chg_adj
            FROM dim_stock s
            LEFT JOIN fact_daily_quote q ON q.stock_code = s.stock_code
                 AND q.trade_date = (SELECT MAX(trade_date) FROM fact_daily_quote)
            WHERE s.stock_code IN ({in_list})
            """
        )
        for r in df.itertuples(index=False):
            info[r.stock_code] = {
                "stock_code": r.stock_code,
                "stock_name": r.stock_name,
                "board": r.board,
                "close_price": _f(r.close_price, 2),
                "pct_chg": _f(r.pct_chg_adj, 2),
            }

    def pack(codes_set):
        return [info.get(c, {"stock_code": c}) for c in sorted(codes_set)]

    return {
        "a": {
            "run_id": ma["run_id"], "data_date": ma["data_date"],
            "hit": len(ca), "strategy": ma.get("strategy_name"),
            "created_at": ma["created_at"],
        },
        "b": {
            "run_id": mb["run_id"], "data_date": mb["data_date"],
            "hit": len(cb), "strategy": mb.get("strategy_name"),
            "created_at": mb["created_at"],
        },
        "counts": {
            "both": len(both), "only_a": len(only_a), "only_b": len(only_b),
            "delta": len(cb) - len(ca),
        },
        "both": pack(both),
        "only_a": pack(only_a),
        "only_b": pack(only_b),
    }


@app.get("/api/v1/screen/runs")
def screen_runs(limit: int = 20):
    df = db.query_df(
        f"SELECT run_id, run_type, data_date, candidate_cnt, hit_cnt, elapsed_ms, created_at "
        f"FROM fact_screen_run ORDER BY run_id DESC LIMIT {max(1, min(limit, 100))}"
    )
    return {"items": _clean(df)}


class LoginIn(BaseModel):
    username: str
    password: str


@app.post("/api/v1/auth/login")
def auth_login(body: LoginIn):
    df = db.query_df(
        "SELECT user_id, username, password_hash, display_name, role, is_active "
        "FROM sys_user WHERE username = :u LIMIT 1",
        {"u": body.username.strip()},
    )
    if df.empty:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    u = df.iloc[0]
    if int(u["is_active"]) != 1:
        raise HTTPException(status_code=403, detail="该账号已被禁用")
    if not _verify_password(body.password, str(u["password_hash"])):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    role = str(u["role"])
    return {
        "access_token": _issue_token(str(u["username"]), role),
        "token_type": "bearer",
        "user": {
            "user_id": int(u["user_id"]),
            "username": str(u["username"]),
            "display_name": str(u["display_name"]),
            "role": role,
        },
    }


@app.get("/api/v1/auth/me")
def auth_me(request: Request):
    u = getattr(request.state, "user", {"sub": config.AUTH_USERNAME, "role": "admin"})
    return {"username": u.get("sub"), "role": u.get("role", "user"), "auth_enabled": config.AUTH_ENABLED}


def _require_admin(request: Request):
    u = getattr(request.state, "user", {})
    if config.AUTH_ENABLED and u.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可执行人员管理操作")


class UserCreateIn(BaseModel):
    username: str
    password: str
    display_name: str = ""
    role: str = "user"
    is_active: int = 1


class UserUpdateIn(BaseModel):
    display_name: Optional[str] = None
    password: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[int] = None


@app.get("/api/v1/users")
def list_users(request: Request):
    _require_admin(request)
    df = db.query_df(
        "SELECT user_id, username, display_name, role, is_active, created_at, updated_at "
        "FROM sys_user ORDER BY user_id ASC"
    )
    return {"items": _clean(df)}


@app.post("/api/v1/users")
def create_user(body: UserCreateIn, request: Request):
    _require_admin(request)
    uname = body.username.strip()
    if not uname or not body.password:
        raise HTTPException(status_code=400, detail="用户名和密码不能为空")
    if body.role not in ("admin", "user"):
        raise HTTPException(status_code=400, detail="角色仅支持 admin 或 user")
    exists = db.scalar("SELECT COUNT(*) FROM sys_user WHERE username=:u", {"u": uname}, default=0)
    if exists:
        raise HTTPException(status_code=409, detail=f"用户名 {uname} 已存在")

    from sqlalchemy import text

    with db.engine.begin() as conn:
        res = conn.execute(
            text(
                "INSERT INTO sys_user (username, password_hash, display_name, role, is_active) "
                "VALUES (:u, :p, :d, :r, :a)"
            ),
            {
                "u": uname,
                "p": _hash_password(body.password),
                "d": body.display_name.strip(),
                "r": body.role,
                "a": 1 if body.is_active else 0,
            },
        )
        uid = res.lastrowid
    return {"user_id": uid, "username": uname}


@app.put("/api/v1/users/{user_id}")
def update_user(user_id: int, body: UserUpdateIn, request: Request):
    _require_admin(request)
    sets, params = [], {"id": user_id}
    if body.display_name is not None:
        sets.append("display_name = :d")
        params["d"] = body.display_name.strip()
    if body.role is not None:
        if body.role not in ("admin", "user"):
            raise HTTPException(status_code=400, detail="角色仅支持 admin 或 user")
        sets.append("role = :r")
        params["r"] = body.role
    if body.is_active is not None:
        sets.append("is_active = :a")
        params["a"] = 1 if body.is_active else 0
    if body.password:
        sets.append("password_hash = :p")
        params["p"] = _hash_password(body.password)
    if not sets:
        return {"ok": True}

    from sqlalchemy import text

    with db.engine.begin() as conn:
        res = conn.execute(text(f"UPDATE sys_user SET {', '.join(sets)} WHERE user_id = :id"), params)
        if res.rowcount == 0:
            raise HTTPException(status_code=404, detail="用户不存在")
    return {"ok": True}


@app.delete("/api/v1/users/{user_id}")
def delete_user(user_id: int, request: Request):
    _require_admin(request)
    cur_user = getattr(request.state, "user", {}).get("sub")
    df = db.query_df("SELECT username, role FROM sys_user WHERE user_id = :id", {"id": user_id})
    if df.empty:
        raise HTTPException(status_code=404, detail="用户不存在")
    if str(df.iloc[0]["username"]) == cur_user:
        raise HTTPException(status_code=400, detail="不能删除当前登录账号")
    if str(df.iloc[0]["role"]) == "admin":
        admin_cnt = db.scalar("SELECT COUNT(*) FROM sys_user WHERE role='admin'", default=0)
        if admin_cnt <= 1:
            raise HTTPException(status_code=400, detail="至少保留一个管理员账号")

    from sqlalchemy import text

    with db.engine.begin() as conn:
        conn.execute(text("DELETE FROM sys_user WHERE user_id = :id"), {"id": user_id})
    return {"ok": True}


@app.get("/api/v1/market/sentiment")
def market_sentiment(trade_date: Optional[str] = None):
    """基于横截面实时计算市场情绪温度计（无需额外采集开销）。"""
    d = trade_date or db.scalar("SELECT MAX(trade_date) FROM fact_indicator_daily")
    if not d:
        return {"data": None, "message": "尚无行情指标数据"}
    df = cross_section.build(d)
    if df.empty:
        return {"data": None, "message": f"{d} 无数据"}

    pct = pd.to_numeric(df["pct_chg_adj"], errors="coerce").dropna()
    up_cnt = int((pct > 0).sum())
    down_cnt = int((pct < 0).sum())
    limit_up = int((df.get("limit_status") == "UP").sum()) if "limit_status" in df.columns else int((pct >= 9.8).sum())
    limit_down = int((df.get("limit_status") == "DOWN").sum()) if "limit_status" in df.columns else int((pct <= -9.8).sum())
    total_amt_yi = round(float(pd.to_numeric(df["amount"], errors="coerce").fillna(0).sum()) / 1e8, 2)

    valid = max(up_cnt + down_cnt, 1)
    temp = max(0, min(100, int(up_cnt / valid * 100)))
    if temp >= 65:
        bias, tip = "进攻偏暖", "市场情绪偏暖，可侧重相对强势或科技高低切，注意不追高"
    elif temp <= 35:
        bias, tip = "防御偏冷", "市场情绪偏冷，建议控制仓位，侧重低估值红利防御"
    else:
        bias, tip = "震荡均衡", "市场震荡分化，建议保持低估值防御与景气高低切均衡配置"

    return {
        "data": {
            "trade_date": str(d),
            "up_count": up_cnt,
            "down_count": down_cnt,
            "limit_up_count": limit_up,
            "limit_down_count": limit_down,
            "total_amount_yi": total_amt_yi,
            "temperature_score": temp,
            "style_bias": bias,
            "suggestion": tip,
        }
    }


@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------- 静态页面（必须最后注册） ----------------
@app.get("/")
def index():
    # 页面改动频繁，必须让浏览器每次回源校验（有 ETag，未变时仍是 304 不重传）。
    # 不加这条时浏览器按 Last-Modified 启发式缓存，改完样式刷新看不到效果。
    return FileResponse(
        os.path.join(WEB_DIR, "index.html"),
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
