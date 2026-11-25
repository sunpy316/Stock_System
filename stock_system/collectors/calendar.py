# -*- coding: utf-8 -*-
"""交易日历采集：写入交易日并计算前后交易日。"""
from datetime import timedelta

from .. import db
from ..datasource import AkshareSource


def sync_calendar(source: AkshareSource = None):
    src = source or AkshareSource()
    task_id = db.log_task_start("sync_calendar", "calendar")
    try:
        df = src.get_trade_calendar()
        dates = sorted(df["trade_date"].tolist())

        rows = []
        for i, d in enumerate(dates):
            prev_d = dates[i - 1] if i > 0 else None
            next_d = dates[i + 1] if i < len(dates) - 1 else None
            rows.append((d, 1, prev_d, next_d))

        n = db.bulk_upsert(
            "dim_trade_calendar",
            ["cal_date", "is_open", "prev_trade_date", "next_trade_date"],
            rows,
            update_columns=["is_open", "prev_trade_date", "next_trade_date"],
        )
        db.log_task_end(task_id, "success", len(rows), n, 0)
        latest = dates[-1] if dates else None
        return {"total": len(rows), "written": n, "first": dates[0] if dates else None, "last": latest}
    except Exception as exc:  # noqa: BLE001
        db.log_task_end(task_id, "failed", 0, 0, 0, str(exc))
        raise
