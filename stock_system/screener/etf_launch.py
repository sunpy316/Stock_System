# -*- coding: utf-8 -*-
"""高流动性 A 股权益类 ETF 启动信号扫描模块（独立轻表，不侵入个股核心表）。"""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import math
import threading
import urllib.request
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from .. import db

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://quote.eastmoney.com/",
}

EXCLUDE_KEYWORDS = [
    "货币", "添益", "日利", "短融", "国债", "政金", "城投", "信用债",
    "可转债", "十年", "30年", "债券", "现金", "黄金", "豆粕", "原油",
    "纳指", "纳斯达克", "标普", "日经", "德国", "法国", "沙特", "东南亚",
    "亚太", "美国", "道琼斯", "中概", "恒生", "港股", "香港", "H股", "中韩",
    "快线", "保证金", "理财", "基准做市", "公司债", "地债", "可转", "白银", "有色期货",
    "巴西", "印度", "越南",
]

_SCAN_LOCK = threading.Lock()
_SCAN_RUNNING = False


def ensure_etf_table() -> None:
    """幂等创建 ETF 启动信号结果表。"""
    with db.engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS fact_etf_launch_signal (
                    trade_date DATE NOT NULL COMMENT '信号交易日',
                    etf_code VARCHAR(16) NOT NULL COMMENT 'ETF代码',
                    etf_name VARCHAR(64) NOT NULL COMMENT 'ETF名称',
                    tier TINYINT NOT NULL COMMENT '分层: 1=主升突破 2=右侧转折/口袋支点',
                    close_price DECIMAL(12,4) DEFAULT NULL,
                    pct_chg DECIMAL(8,3) DEFAULT NULL COMMENT '涨跌幅(%)',
                    amt_yi DECIMAL(10,2) DEFAULT NULL COMMENT '成交额(亿元)',
                    vol_ratio DECIMAL(8,2) DEFAULT NULL COMMENT '5日量比',
                    vol_ratio_20 DECIMAL(8,2) DEFAULT NULL COMMENT '20日量比',
                    consol_range_20 DECIMAL(8,2) DEFAULT NULL COMMENT '近20日振幅(%)',
                    boll_width_20 DECIMAL(8,2) DEFAULT NULL COMMENT '前1日布林带带宽(%)',
                    bias_ma20 DECIMAL(8,2) DEFAULT NULL COMMENT '20日乖离率(%)',
                    upper_shadow_ratio DECIMAL(8,3) DEFAULT NULL COMMENT '上影线占比',
                    ma20 DECIMAL(12,4) DEFAULT NULL,
                    ma60 DECIMAL(12,4) DEFAULT NULL,
                    breakout_ref DECIMAL(12,4) DEFAULT NULL COMMENT '突破参考位(前20日高点)',
                    stop_loss_ref DECIMAL(12,4) DEFAULT NULL COMMENT '防守止损位(MA20)',
                    low_price DECIMAL(12,4) DEFAULT NULL COMMENT '当日最低价',
                    ma60_gap_pct DECIMAL(8,2) DEFAULT NULL COMMENT '距60日线空间(%)',
                    signals VARCHAR(128) NOT NULL DEFAULT '' COMMENT '触发信号标签',
                    risk_tag VARCHAR(64) NOT NULL DEFAULT '' COMMENT '防追高风险提示',
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (trade_date, etf_code),
                    KEY idx_etf_date_tier (trade_date, tier)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='ETF启动信号扫描结果'
                """
            )
        )


def _fetch_json(url: str, timeout: int = 8) -> Optional[Dict[str, Any]]:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_etf_kline(secid: str, lmt: int = 100) -> Optional[List[Dict[str, Any]]]:
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get?"
        f"secid={secid}&fields1=f1,f2,f3,f4,f5,f6"
        "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
        f"&klt=101&fqt=1&end=20500101&lmt={lmt}"
    )
    data = _fetch_json(url, timeout=8)
    if not data or not data.get("data") or not data["data"].get("klines"):
        return None
    rows = []
    for line in data["data"]["klines"]:
        p = line.split(",")
        rows.append({
            "date": p[0],
            "open": float(p[1]),
            "close": float(p[2]),
            "high": float(p[3]),
            "low": float(p[4]),
            "vol": float(p[5]),
            "amt": float(p[6]),
            "pct": float(p[8]),
        })
    return rows


def _analyze_etf(item: Dict[str, Any], rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not rows or len(rows) < 65:
        return None
    last = rows[-1]
    closes = [r["close"] for r in rows]
    highs = [r["high"] for r in rows]
    lows = [r["low"] for r in rows]
    vols = [r["vol"] for r in rows]

    ma20 = sum(closes[-20:]) / 20.0
    ma60 = sum(closes[-60:]) / 60.0
    vol_ma5_prev = sum(vols[-6:-1]) / 5.0
    vol_ma20_prev = sum(vols[-21:-1]) / 20.0
    vol_r5 = last["vol"] / vol_ma5_prev if vol_ma5_prev > 0 else 0.0
    vol_r20 = last["vol"] / vol_ma20_prev if vol_ma20_prev > 0 else 0.0

    prev20_high = max(highs[-21:-1])
    prev60_high = max(highs[-61:-1])
    prev20_low = min(lows[-21:-1])
    consol_20 = (prev20_high - prev20_low) / prev20_low * 100.0 if prev20_low > 0 else 999.0

    prev20_closes = closes[-21:-1]
    prev20_mean = sum(prev20_closes) / 20.0
    prev20_std = math.sqrt(sum((x - prev20_mean) ** 2 for x in prev20_closes) / 20.0)
    bb_w = (4.0 * prev20_std / prev20_mean) * 100.0 if prev20_mean > 0 else 999.0

    down_vols_10 = [r["vol"] for r in rows[-11:-1] if r["pct"] < 0]
    max_down_vol_10 = max(down_vols_10) if down_vols_10 else 0.0
    is_pp = last["pct"] > 0 and max_down_vol_10 > 0 and last["vol"] > max_down_vol_10

    day_range = last["high"] - last["low"]
    upper_sh = (last["high"] - max(last["open"], last["close"])) / day_range if day_range > 0 else 0.0
    bias_ma20 = (last["close"] / ma20 - 1.0) * 100.0 if ma20 > 0 else 0.0
    ma60_gap = (ma60 / last["close"] - 1.0) * 100.0 if last["close"] > 0 else 0.0

    brk20 = last["close"] > prev20_high
    brk60 = last["close"] > prev60_high
    abv20 = last["close"] > ma20
    abv60 = last["close"] > ma60

    if not (abv20 and (brk20 or brk60 or is_pp) and vol_r5 >= 1.4 and upper_sh <= 0.35):
        return None

    tier = 1 if abv60 else 2
    sig_tags = []
    if brk60:
        sig_tags.append("突破60日高")
    elif brk20:
        sig_tags.append("突破20日高")
    if is_pp:
        sig_tags.append("口袋支点")
    if consol_20 <= 15:
        sig_tags.append("紧凑蓄势")

    risk_tag = "偏离较大-宜等回踩" if bias_ma20 > 8.0 else ""

    return {
        "trade_date": last["date"],
        "etf_code": item["code"],
        "etf_name": item["name"],
        "tier": tier,
        "close_price": round(last["close"], 4),
        "pct_chg": round(last["pct"], 2),
        "amt_yi": round(last["amt"] / 1e8, 2),
        "vol_ratio": round(vol_r5, 2),
        "vol_ratio_20": round(vol_r20, 2),
        "consol_range_20": round(consol_20, 1),
        "boll_width_20": round(bb_w, 1),
        "bias_ma20": round(bias_ma20, 2),
        "upper_shadow_ratio": round(upper_sh, 3),
        "ma20": round(ma20, 4),
        "ma60": round(ma60, 4),
        "breakout_ref": round(prev20_high, 4),
        "stop_loss_ref": round(ma20, 4),
        "low_price": round(last["low"], 4),
        "ma60_gap_pct": round(ma60_gap, 2),
        "signals": ",".join(sig_tags),
        "risk_tag": risk_tag,
        "_sort_key": (tier == 1, brk60, brk20, vol_r20, last["amt"]),
    }


def run_etf_scan(min_amt_yi: float = 1.5, max_workers: int = 10) -> Dict[str, Any]:
    """扫描高流动性 A 股权益类 ETF 启动信号并落库。"""
    global _SCAN_RUNNING
    with _SCAN_LOCK:
        if _SCAN_RUNNING:
            return {"status": "busy", "message": "ETF 扫描任务正在执行中"}
        _SCAN_RUNNING = True

    ensure_etf_table()
    task_id = db.log_task_start("etf_launch_scan", "etf", f"min_amt_yi={min_amt_yi}")
    try:
        all_diff = []
        for pn in (1, 2, 3):
            url = (
                "https://push2.eastmoney.com/api/qt/clist/get?"
                f"pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f6"
                "&fs=b:MK0021,b:MK0022,b:MK0023,b:MK0024"
                "&fields=f12,f13,f14,f2,f3,f6"
            )
            data = _fetch_json(url, timeout=10)
            if data and data.get("data") and data["data"].get("diff"):
                all_diff.extend(data["data"]["diff"])

        items = []
        seen = set()
        min_amt = min_amt_yi * 1e8
        for d in all_diff:
            code = str(d.get("f12") or "")
            if not code or code in seen:
                continue
            seen.add(code)
            name = str(d.get("f14") or "")
            amt = d.get("f6")
            if not isinstance(amt, (int, float)) or amt < min_amt:
                continue
            if any(k in name for k in EXCLUDE_KEYWORDS):
                continue
            items.append({"secid": f"{d['f13']}.{code}", "code": code, "name": name})

        db.log_task_progress(task_id, total_cnt=len(items), success_cnt=0, fail_cnt=0)

        analyzed = []
        kline_cache: Dict[str, List[Dict[str, Any]]] = {}
        latest_dates = Counter()
        fail_cnt = 0
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = {ex.submit(_fetch_etf_kline, it["secid"], 100): it for it in items}
            for fut in as_completed(futs):
                it = futs[fut]
                try:
                    rows = fut.result()
                    if rows:
                        kline_cache[it["code"]] = rows
                        latest_dates[rows[-1]["date"]] += 1
                    res = _analyze_etf(it, rows)
                    if res:
                        analyzed.append(res)
                except Exception:  # noqa: BLE001
                    fail_cnt += 1

        if not latest_dates:
            db.log_task_end(task_id, "failed", len(items), 0, fail_cnt, "未获取到 ETF 日线数据")
            return {"status": "failed", "scanned": len(items), "hit": 0}

        target_date = latest_dates.most_common(1)[0][0]
        hits = [r for r in analyzed if r["trade_date"] == target_date]
        hits.sort(key=lambda x: x["_sort_key"], reverse=True)

        cols = [
            "trade_date", "etf_code", "etf_name", "tier", "close_price", "pct_chg",
            "amt_yi", "vol_ratio", "vol_ratio_20", "consol_range_20", "boll_width_20",
            "bias_ma20", "upper_shadow_ratio", "ma20", "ma60", "breakout_ref",
            "stop_loss_ref", "low_price", "ma60_gap_pct", "signals", "risk_tag",
        ]
        with db.engine.begin() as conn:
            conn.execute(
                text("DELETE FROM fact_etf_launch_signal WHERE trade_date = :d"),
                {"d": target_date},
            )
        if hits:
            rows_to_write = [tuple(r[c] for c in cols) for r in hits]
            db.bulk_upsert(
                "fact_etf_launch_signal",
                cols,
                rows_to_write,
                update_columns=[c for c in cols if c not in ("trade_date", "etf_code")],
            )

        pool_res = None
        try:
            from .launch_pool import sync_etf_launch_pool

            pool_res = sync_etf_launch_pool(
                trade_date=target_date,
                scanned_hits=hits,
                kline_cache=kline_cache,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[etf-launch] 同步 ETF 信号池失败：{exc}", flush=True)

        tier1_cnt = sum(1 for r in hits if r["tier"] == 1)
        tier2_cnt = sum(1 for r in hits if r["tier"] == 2)
        db.log_task_end(task_id, "success", len(items), len(items) - fail_cnt, fail_cnt)
        return {
            "status": "success",
            "trade_date": target_date,
            "scanned": len(items),
            "hit": len(hits),
            "tier1": tier1_cnt,
            "tier2": tier2_cnt,
            "pool": pool_res,
        }
    except Exception as exc:  # noqa: BLE001
        db.log_task_end(task_id, "failed", 0, 0, 1, str(exc))
        raise
    finally:
        with _SCAN_LOCK:
            _SCAN_RUNNING = False


def query_etf_signals(trade_date: Optional[str] = None) -> Dict[str, Any]:
    """查询指定交易日（或最新交易日）的 ETF 启动信号。"""
    ensure_etf_table()
    dates_df = db.query_df(
        "SELECT DISTINCT trade_date FROM fact_etf_launch_signal ORDER BY trade_date DESC LIMIT 30"
    )
    dates = [str(d)[:10] for d in dates_df["trade_date"].tolist()] if not dates_df.empty else []
    target = trade_date or (dates[0] if dates else None)
    if not target:
        return {"trade_date": None, "dates": [], "tier1": [], "tier2": [], "items": [], "running": _SCAN_RUNNING}

    df = db.query_df(
        "SELECT * FROM fact_etf_launch_signal WHERE trade_date = :d "
        "ORDER BY tier ASC, vol_ratio_20 DESC, amt_yi DESC",
        {"d": target},
    )
    items = []
    if not df.empty:
        for rec in df.to_dict(orient="records"):
            for k, v in list(rec.items()):
                if hasattr(v, "isoformat"):
                    rec[k] = v.isoformat()
                elif v != v:  # NaN
                    rec[k] = None
            items.append(rec)

    tier1 = [x for x in items if int(x.get("tier") or 2) == 1]
    tier2 = [x for x in items if int(x.get("tier") or 2) == 2]
    return {
        "trade_date": target,
        "dates": dates,
        "tier1": tier1,
        "tier2": tier2,
        "items": items,
        "running": _SCAN_RUNNING,
    }
