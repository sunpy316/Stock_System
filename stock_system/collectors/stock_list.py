# -*- coding: utf-8 -*-
"""股票列表采集：市场/板块判定、ST 标记、增量写入。"""
from .. import db
from ..datasource import AkshareSource


def classify(code: str):
    """按代码前缀判定市场与板块。"""
    if code.startswith("68"):
        return "SH", "科创板"
    if code.startswith("60"):
        return "SH", "主板"
    if code.startswith("30"):
        return "SZ", "创业板"
    if code.startswith(("00", "001", "002", "003")):
        return "SZ", "主板"
    if code.startswith(("8", "4", "92")):
        return "BJ", "北交所"
    return "UNKNOWN", "未知"


def _num(v):
    try:
        x = float(v)
        return None if x != x else x
    except (TypeError, ValueError):
        return None


def _clean(v):
    """NaN/空串转 None，避免把脏值写进库。"""
    if v is None:
        return None
    try:
        if v != v:  # NaN
            return None
    except TypeError:
        pass
    s = str(v).strip()
    return s or None


def sync_share_capital(source: AkshareSource = None, markets=("sh", "sz", "bj")):
    """用各市场快照的市值反推股本，补齐列表接口缺失的沪市与北交所。

    总股本 = 总市值 / 最新价；流通股本 = 流通市值 / 最新价。
    股本变动不频繁，建议每周执行一次。
    """
    src = source or AkshareSource()
    task_id = db.log_task_start("sync_share_capital", "share_capital", f"markets={markets}")
    rows, total, failed = [], 0, []

    for mkt in markets:
        try:
            snap = src.get_spot_by_market(mkt)
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{mkt}: {exc}")
            continue
        for r in snap.itertuples(index=False):
            ts = r.total_mv / r.price if r.price else None
            fs = r.float_mv / r.price if r.price else None
            if ts:
                rows.append((ts, fs, r.stock_code))
                total += 1

    if rows:
        with db.raw_conn() as conn:
            cur = conn.cursor()
            try:
                cur.executemany(
                    # 只补空值，不覆盖交易所列表接口给出的准确股本
                    "UPDATE dim_stock SET total_share=%s, float_share=%s "
                    "WHERE stock_code=%s AND total_share IS NULL",
                    rows,
                )
                conn.commit()
            finally:
                cur.close()

    status = "success" if not failed else ("partial" if rows else "failed")
    db.log_task_end(task_id, status, total, total, len(failed), "; ".join(failed))
    return {"updated": total, "failed": failed}


def sync_stock_list(source: AkshareSource = None, with_info: int = 0):
    """同步全市场股票列表。

    沪深两个接口自带上市日期与股本，多数情况下无需再逐只补采；
    with_info 仅用于补拉列表接口未覆盖的字段（如北交所个股概况）。
    """
    src = source or AkshareSource()
    task_id = db.log_task_start("sync_stock_list", "stock_list")
    try:
        df = src.get_stock_list()
        existing = db.query_df("SELECT stock_code FROM dim_stock")
        known = set(existing["stock_code"]) if not existing.empty else set()

        rows = []
        new_codes = []
        board_counter = {}
        for r in df.itertuples(index=False):
            code = str(r.stock_code).zfill(6)
            name = str(r.stock_name).strip()
            market, board = classify(code)
            is_st = 1 if ("ST" in name.upper()) else 0
            rows.append(
                (
                    code, name, market, board, "L", is_st,
                    r.list_date,
                    _num(r.total_share), _num(r.float_share),
                    _clean(r.industry_name),
                )
            )
            board_counter[board] = board_counter.get(board, 0) + 1
            if code not in known:
                new_codes.append(code)

        n = db.bulk_upsert(
            "dim_stock",
            ["stock_code", "stock_name", "market", "board", "list_status", "is_st",
             "list_date", "total_share", "float_share", "industry_code"],
            rows,
            update_columns=["stock_name", "market", "board", "is_st",
                            "list_date", "total_share", "float_share", "industry_code"],
            coalesce_columns=["list_date", "total_share", "float_share", "industry_code"],
        )

        filled = 0
        if with_info and new_codes:
            from .stock_info import fill_stock_info

            filled = fill_stock_info(src, new_codes[:with_info])

        db.log_task_end(task_id, "success", len(rows), n, 0)
        return {
            "total": len(rows),
            "written": n,
            "new": len(new_codes),
            "info_filled": filled,
            "by_board": board_counter,
        }
    except Exception as exc:  # noqa: BLE001
        db.log_task_end(task_id, "failed", 0, 0, 0, str(exc))
        raise
