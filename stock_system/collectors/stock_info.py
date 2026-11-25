# -*- coding: utf-8 -*-
"""个股概况补采：上市日期、总股本、流通股本、所属行业。"""
from .. import db
from ..datasource import AkshareSource

KEEP_FIELDS = ("上市时间", "总股本", "流通股", "行业")


def _parse_number(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fill_stock_info(source: AkshareSource, codes):
    """为给定股票批量补拉概况。返回成功条数。"""
    ok = 0
    for code in codes:
        try:
            info = source.get_stock_info(code)
        except Exception:  # noqa: BLE001
            continue
        if not info:
            continue

        list_date = None
        raw_date = info.get("上市时间")
        if raw_date:
            s = str(raw_date).replace("-", "")[:8]
            if len(s) == 8 and s.isdigit():
                list_date = f"{s[:4]}-{s[4:6]}-{s[6:]}"

        total_share = _parse_number(info.get("总股本"))
        float_share = _parse_number(info.get("流通股"))
        industry = info.get("行业")

        with db.engine.begin() as conn:
            from sqlalchemy import text

            conn.execute(
                text(
                    "UPDATE dim_stock SET list_date=COALESCE(:ld, list_date), "
                    "total_share=COALESCE(:ts, total_share), "
                    "float_share=COALESCE(:fs, float_share), "
                    "industry_code=COALESCE(:ind, industry_code) "
                    "WHERE stock_code=:code"
                ),
                {
                    "ld": list_date,
                    "ts": total_share,
                    "fs": float_share,
                    "ind": industry,
                    "code": code,
                },
            )
        ok += 1
    return ok
