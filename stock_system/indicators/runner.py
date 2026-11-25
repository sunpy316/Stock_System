# -*- coding: utf-8 -*-
"""指标计算任务：读取复权行情，算指标，落 fact_indicator_daily。"""
import math
from datetime import datetime, timedelta

from .. import db
from .calc import calc_technical

COLS = [
    "stock_code", "trade_date",
    "ma5", "ma10", "ma20", "ma60", "ma120", "ma250",
    "macd_dif", "macd_dea", "macd_bar",
    "kdj_k", "kdj_d", "kdj_j",
    "rsi6", "rsi12", "rsi24",
    "boll_up", "boll_mid", "boll_low", "atr14",
    "vol_ratio", "vol_ma5",
    "pct_chg_5d", "pct_chg_20d", "pct_chg_60d",
    "high_60d", "low_60d", "rs_20d",
]

UPDATE_COLS = [c for c in COLS if c not in ("stock_code", "trade_date")]


def _n(v):
    """转 float，NaN / ±inf / 异常一律返回 None（NULL）。

    PyMySQL 对 inf 会直接抛 "inf can not be used with MySQL"，
    整个批次都会失败，因此必须在入库前拦掉。
    """
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x or math.isinf(x):
        return None
    return x


def _d(s):
    """把 20240101 / 2024-01-01 统一成 2024-01-01。"""
    if not s:
        return None
    s = str(s).replace("-", "")
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


def _iter_groups(start, end, codes, batch=120):
    """按「一批股票」取数，逐只产出 (code, DataFrame)。

    内存策略：不再把 1300 多万行一次性 concat 进 pandas（常驻 2~3GB，2GB 云
    服务器会 OOM），也不使用流式游标（SSCursor 逐行读取，实测慢 3 倍）。
    改为每批 120 只股票一条 SQL，单批约 20~40 万行、几十 MB，既快又不涨内存。
    「只算一批」也能把 SQL 交给主键 (stock_code, trade_date) 做范围扫描。
    """
    for i in range(0, len(codes), batch):
        part = codes[i: i + batch]
        holders = ",".join(f":c{j}" for j in range(len(part)))
        where = [f"stock_code IN ({holders})"]
        params = {f"c{j}": c for j, c in enumerate(part)}
        if start:
            where.append("trade_date >= :start")
            params["start"] = start
        if end:
            where.append("trade_date <= :end")
            params["end"] = end

        sql = (
            "SELECT stock_code, trade_date, open_adj, high_adj, low_adj, close_adj, volume "
            f"FROM fact_daily_quote_adj WHERE {' AND '.join(where)} "
            "ORDER BY stock_code, trade_date"
        )
        df = db.query_df(sql, params)
        if df.empty:
            continue
        for code, g in df.groupby("stock_code", sort=True):
            yield code, g.reset_index(drop=True)


def _all_codes(codes):
    """待处理股票清单。指定 codes 时直接用；否则取 dim_stock 全集（瞬时）。"""
    if codes:
        return sorted({str(c) for c in codes})
    return db.query_df(
        "SELECT stock_code FROM dim_stock ORDER BY stock_code"
    )["stock_code"].astype(str).tolist()


def run(start=None, end=None, codes=None, batch=120, progress_every=5):
    """重算指定区间指标。start 为空时从复权行情的第一个交易日开始。

    batch 是「每批股票数」，直接决定单次取数的内存峰值：120 只约 30 万行、
    几十 MB，2GB 云机可承受；调到 300 会到 70 万行，内存压力翻倍，非必要别动。
    """
    start, end = _d(start), _d(end)

    # 预热窗口：均线最长 250 根，MACD/RSI 也需要前置数据。
    # 只加载 >= start 会让单日重算拿到 1 根 K 线，全部算成 NULL。
    # 这里往前多取 550 个自然日（约 370 个交易日）用于预热，写库时再裁回 [start, end]。
    load_start = None
    if start:
        load_start = (
            datetime.strptime(start, "%Y-%m-%d") - timedelta(days=550)
        ).strftime("%Y-%m-%d")

    task_id = db.log_task_start("calc_indicator", "indicator", f"start={start} end={end}")
    total_rows, stocks_done, failed = 0, 0, []

    try:
        code_list = _all_codes(codes)
        total_stocks = len(code_list)
        db.log_task_progress(task_id, total_cnt=total_stocks, success_cnt=0, fail_cnt=0)

        for code, g in _iter_groups(load_start, end, code_list, batch=batch):
            try:
                ind = calc_technical(g)
                ind.insert(0, "trade_date", g["trade_date"].values)
                if start:
                    # 预热行不写库，只保留请求区间
                    ind = ind[ind["trade_date"].astype(str) >= start]
                    if ind.empty:
                        continue
                ind.insert(0, "stock_code", code)

                rows = []
                for rec in ind[COLS].itertuples(index=False):
                    rows.append(tuple(_n(v) if i > 1 else v for i, v in enumerate(rec)))

                total_rows += db.bulk_upsert(
                    "fact_indicator_daily", COLS, rows, update_columns=UPDATE_COLS
                )
                stocks_done += 1
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{code}: {exc}")

            if stocks_done and stocks_done % progress_every == 0:
                print(f"  已处理 {stocks_done} 只，累计 {total_rows} 行", flush=True)
                db.log_task_progress(
                    task_id, total_cnt=total_stocks,
                    success_cnt=stocks_done, fail_cnt=len(failed),
                )

        status = "success" if not failed else ("partial" if stocks_done else "failed")
        db.log_task_end(task_id, status, total_stocks, stocks_done, len(failed),
                        "; ".join(failed[:5]))
        return {
            "rows": total_rows,
            "stocks": stocks_done,
            "fail": len(failed),
            "fail_samples": failed[:5],
        }
    except Exception as exc:  # noqa: BLE001
        db.log_task_end(task_id, "failed", total_stocks, stocks_done, 0, str(exc))
        raise


def latest_date():
    return db.scalar("SELECT MAX(trade_date) FROM fact_indicator_daily")
