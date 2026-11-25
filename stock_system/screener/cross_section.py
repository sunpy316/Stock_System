# -*- coding: utf-8 -*-
"""横截面视图：把某交易日的技术指标、行情、基础信息与 as-of 基本面拼成一张宽表。

这是筛选引擎的唯一数据入口。所有字段在此一次性备齐，
筛选阶段只做内存列运算，不再回表（对应需求文档决策五）。
"""
import threading
import time
from pathlib import Path

import pandas as pd

from .. import db

BASE_SQL = """
SELECT
  i.stock_code, i.trade_date,
  i.ma5, i.ma10, i.ma20, i.ma60, i.ma120, i.ma250,
  i.macd_dif, i.macd_dea, i.macd_bar,
  i.kdj_k, i.kdj_d, i.kdj_j,
  i.rsi6, i.rsi12, i.rsi24,
  i.boll_up, i.boll_mid, i.boll_low, i.atr14,
  i.vol_ratio, i.vol_ma5,
  i.pct_chg_5d, i.pct_chg_20d, i.pct_chg_60d,
  i.high_60d, i.low_60d,
  q.close_price, q.pct_chg, q.pct_chg_adj, q.turnover_rate, q.amount, q.limit_status,
  s.stock_name, s.board, s.is_st, s.list_date, s.total_share, s.float_share,
  ROUND(q.close_price * s.total_share / 100000000, 4)  AS total_mv_yi,
  ROUND(q.close_price * s.float_share / 100000000, 4) AS float_mv_yi,
  DATEDIFF(:d, s.list_date) AS list_days
FROM fact_indicator_daily i
JOIN fact_daily_quote q ON q.stock_code = i.stock_code AND q.trade_date = i.trade_date
JOIN dim_stock s        ON s.stock_code = i.stock_code
WHERE i.trade_date = :d
"""

# 财务宽表：一次扫描取出最近 8 期（同一报告期只留最新公告）。
# 最新一期用于 ROE/同比等字段，其余用于 TTM 计算——合并成一次查询，
# 避免对同一张表做两遍窗口函数（实测各省约 1.3 秒）。
FIN_SERIES_SQL = """
WITH ranked AS (
  SELECT f.stock_code, f.report_period, f.ann_date,
         f.roe, f.roa, f.revenue_yoy, f.net_profit_yoy, f.debt_ratio,
         f.gross_margin, f.net_margin, f.eps, f.net_profit,
         ROW_NUMBER() OVER (PARTITION BY f.stock_code, f.report_period
                            ORDER BY f.ann_date DESC) AS r1
  FROM fact_financial f
  WHERE f.ann_date <= :d
    -- 只取近 5 年：TTM 最多回溯到「上年年报 + 上年同期」，5 年足够覆盖。
    -- 不加这条时窗口函数要排序全表 33 万行，实测 4.4s；加后 2.1s，结果完全一致。
    AND f.report_period >= DATE_SUB(:d, INTERVAL 5 YEAR)
), dedup AS (
  SELECT * FROM ranked WHERE r1 = 1
), ordered AS (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY stock_code
                               ORDER BY report_period DESC) AS r2
  FROM dedup
)
SELECT stock_code, report_period, roe, roa, revenue_yoy, net_profit_yoy,
       debt_ratio, gross_margin, net_margin, eps, net_profit, r2
FROM ordered WHERE r2 <= 8
"""


def _ttm_net_profit(series_df):
    """按累计值口径计算 TTM 归母净利润（向量化）。

    财务数据是累计值：Q1、H1、Q3、年报。直接相加四个报告期会重复计算。
    正确公式：
        最新为年报      → TTM = 年报值
        其他            → TTM = 上年年报 + 本期累计 − 上年同期累计

    逐股循环在 5000 只规模下要多花近 1 秒，这里改用整表 merge。
    """
    if series_df.empty:
        return pd.DataFrame(columns=["stock_code", "np_ttm"])

    g = series_df.copy()
    g["report_period"] = pd.to_datetime(g["report_period"], errors="coerce")
    g["net_profit"] = pd.to_numeric(g["net_profit"], errors="coerce")
    g["year"] = g["report_period"].dt.year
    g["month"] = g["report_period"].dt.month

    latest = g[g["r2"] == 1][["stock_code", "net_profit", "year", "month"]].copy()
    latest["prev_year"] = latest["year"] - 1

    # 上年同期累计
    prev = g[["stock_code", "year", "month", "net_profit"]].rename(
        columns={"year": "prev_year", "net_profit": "prev_np"}
    )
    latest = latest.merge(prev, on=["stock_code", "prev_year", "month"], how="left")

    # 上年年报
    annual = g[g["month"] == 12][["stock_code", "year", "net_profit"]].rename(
        columns={"year": "prev_year", "net_profit": "annual_np"}
    )
    annual = annual.drop_duplicates(subset=["stock_code", "prev_year"], keep="last")
    latest = latest.merge(annual, on=["stock_code", "prev_year"], how="left")

    cum = latest["net_profit"]
    computed = latest["annual_np"] + cum - latest["prev_np"]
    latest["np_ttm"] = computed.where(
        cum.notna() & latest["annual_np"].notna() & latest["prev_np"].notna()
    )
    # 最新一期为年报时，TTM 就是年报值本身
    latest.loc[latest["month"] == 12, "np_ttm"] = cum[latest["month"] == 12]

    return latest[["stock_code", "np_ttm"]]


def _build(trade_date):
    """构建横截面（不走缓存）。"""
    d = str(trade_date)
    t0 = time.time()
    base = db.query_df(BASE_SQL, {"d": d})
    t1 = time.time()
    if base.empty:
        return base

    series = db.query_df(FIN_SERIES_SQL, {"d": d})
    t2 = time.time()
    if series.empty:
        fin = pd.DataFrame(columns=["stock_code", "roe", "roa", "revenue_yoy",
                                    "net_profit_yoy", "debt_ratio", "gross_margin",
                                    "net_margin", "eps", "net_profit", "report_period"])
        np_ttm = pd.DataFrame(columns=["stock_code", "np_ttm"])
    else:
        fin = series[series["r2"] == 1].drop(columns=["r2"])
        np_ttm = _ttm_net_profit(series)

    df = base.merge(fin, on="stock_code", how="left")
    df = df.merge(np_ttm, on="stock_code", how="left")

    # PE(TTM)：净利润 ≤ 0 时置空，不做负数市盈率
    mv_yuan = df["total_mv_yi"] * 1e8
    np_ttm = pd.to_numeric(df["np_ttm"], errors="coerce")
    df["pe_ttm"] = (mv_yuan / np_ttm).where(np_ttm > 0)
    df["pb"] = None  # 待补：需要最新一期归母净资产
    df["dividend_yield"] = None

    # 高低切与位置指标：距60日高点回撤(%)、距60日低点涨幅(%)
    high_60 = pd.to_numeric(df["high_60d"], errors="coerce")
    low_60 = pd.to_numeric(df["low_60d"], errors="coerce")
    close_p = pd.to_numeric(df["close_price"], errors="coerce")
    df["drawdown_60d"] = ((high_60 - close_p) / high_60.replace(0, pd.NA) * 100).round(2)
    df["pct_from_low_60d"] = ((close_p - low_60) / low_60.replace(0, pd.NA) * 100).round(2)

    # 横截面相对强度：个股 20 日涨幅 - 全市场 20 日涨幅中位数
    pct_20 = pd.to_numeric(df["pct_chg_20d"], errors="coerce")
    market_med_20 = pct_20.median(skipna=True)
    df["rs_20d"] = (pct_20 - market_med_20).round(2) if pd.notna(market_med_20) else None

    df["is_st"] = df["is_st"].astype(int)
    df["list_days"] = pd.to_numeric(df["list_days"], errors="coerce")

    # 耗时埋点：线上排查「筛选要 30 秒」时，直接 journalctl 看是哪一段慢。
    # base 慢 → 行情/指标 join；fin 慢 → 财务窗口函数；merge 慢 →  pandas 侧。
    t3 = time.time()
    print(
        f"[xsec] {d} rows={len(df)} base={t1 - t0:.2f}s fin={t2 - t1:.2f}s "
        f"merge={t3 - t2:.2f}s total={t3 - t0:.2f}s",
        flush=True,
    )
    return df


# ---------------- 缓存 ----------------
# 同一交易日的横截面在「数据不变」的前提下是**只读快照**：采集完成后 invalidate()
# 一定会清掉，所以 TTL 不需要短。原先 180 秒意味着隔三分钟点一次筛选就是一次冷构建，
# 线上冷库上要 30 秒。这里放宽到 6 小时，靠 invalidate() 保证时效。
_CACHE = {}
_CACHE_TTL = 6 * 3600  # 秒
_CACHE_MAX = 3
# 磁盘快照目录：服务重启后直接从盘上恢复，省掉 30 秒的冷构建
_CACHE_DIR = Path(__file__).resolve().parents[2] / "cache" / "xsec"
# 冷启动时可能有多个请求（含预热线程）同时到达，串行化避免重复扫表
_BUILD_LOCK = threading.Lock()


def invalidate():
    """清空横截面缓存（内存 + 磁盘）。数据采集完成后调用，避免读到旧快照。"""
    _CACHE.clear()
    try:
        for p in _CACHE_DIR.glob("xsec_*.pkl"):
            p.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 - 清盘失败不影响主流程
        pass


def _disk_path(key):
    return _CACHE_DIR / f"xsec_{key}.pkl"


def _load_disk(key):
    """读磁盘快照。同一交易日的横截面建好就不变，重启后可直接复用。

    这是冷启动的关键：线上重建一次要 30 秒（1300 万行表按日扫描），
    而 5500 行的快照读盘只要几十毫秒。
    """
    p = _disk_path(key)
    if not p.exists():
        return None
    try:
        import pickle

        with p.open("rb") as f:
            return pickle.load(f)
    except Exception:  # noqa: BLE001 - 文件损坏就当没有
        return None


def _save_disk(key, df):
    try:
        import pickle

        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _disk_path(key).with_suffix(".tmp")
        with tmp.open("wb") as f:
            pickle.dump(df, f, protocol=4)
        tmp.replace(_disk_path(key))
        # 只保留最近 5 个交易日的快照
        files = sorted(_CACHE_DIR.glob("xsec_*.pkl"))
        for old in files[:-5]:
            old.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 - 落盘失败只损失下次冷启动速度
        pass


def build(trade_date):
    """返回该交易日的横截面宽表（内存 → 磁盘 → 现算 三级缓存）。"""
    key = str(trade_date)
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1].copy()

    with _BUILD_LOCK:
        # 拿到锁后再查一次：等锁期间别人可能已经构建好了
        hit = _CACHE.get(key)
        if hit and time.time() - hit[0] < _CACHE_TTL:
            return hit[1].copy()

        df = _load_disk(key)
        if df is not None and not df.empty:
            _CACHE[key] = (time.time(), df)
            print(f"[xsec] {key} 命中磁盘快照 rows={len(df)}", flush=True)
            return df.copy()

        df = _build(key)
        if not df.empty:
            _save_disk(key, df)
            # 只保留最近几个交易日，避免缓存无限增长
            if len(_CACHE) >= _CACHE_MAX:
                oldest = min(_CACHE.items(), key=lambda kv: kv[1][0])[0]
                _CACHE.pop(oldest, None)
            _CACHE[key] = (time.time(), df)
        return df.copy()


def warmup(trade_date=None):
    """预热横截面，供服务启动与每日采集结束后调用。

    首次构建是筛选慢的根因（线上冷库可达 30 秒）。在没人用的时候先建好，
    用户打开页面时就直接命中缓存。失败只记日志，不影响调用方。
    """
    try:
        d = trade_date or db.scalar("SELECT MAX(trade_date) FROM fact_indicator_daily")
        if d is None:
            return None
        t0 = time.time()
        df = build(d)
        print(
            f"[xsec] 预热完成 {d} rows={len(df)} 用时 {time.time() - t0:.2f}s",
            flush=True,
        )
        return int(len(df))
    except Exception as exc:  # noqa: BLE001
        print(f"[xsec] 预热失败：{exc}", flush=True)
        return None


def available_dates(limit=10):
    """返回有指标数据的最近若干交易日。"""
    return db.query_df(
        f"SELECT DISTINCT trade_date FROM fact_indicator_daily "
        f"ORDER BY trade_date DESC LIMIT {int(limit)}"
    )
