# -*- coding: utf-8 -*-
"""财务数据采集：宽表转长表、公告日推定、同比补算。

关键约束（需求文档 FR-IND-05）：财务数据以公告日为时间轴。
akshare 财务摘要只返回报告期、不返回公告日，因此统一按法定披露截止日推定，
并以 ann_date_source='estimated' 标记，保证 as-of 切片不会用到未来数据。
"""
import math
import re
from datetime import date

from .. import db
from ..datasource import AkshareSource

FIELD_MAP = {
    "revenue": "营业总收入",
    "revenue_yoy": "营业总收入增长率",
    "net_profit": "归母净利润",
    "net_profit_yoy": "归属母公司净利润增长率",
    "deduct_net_profit": "扣非净利润",
    "gross_margin": "毛利率",
    "net_margin": "销售净利率",
    "roe": "净资产收益率(ROE)",
    "roa": "总资产报酬率(ROA)",
    "debt_ratio": "资产负债率",
    "current_ratio": "流动比率",
    "ocf": "经营现金流量净额",
    "total_equity": "股东权益合计(净资产)",
    "eps": "基本每股收益",
    "bps": "每股净资产",
    "ocf_ps": "每股经营现金流",
}

COLUMNS = [
    "stock_code", "report_period", "ann_date", "ann_date_source",
    "revenue", "revenue_yoy", "net_profit", "net_profit_yoy",
    "deduct_net_profit", "deduct_net_profit_yoy", "gross_margin", "net_margin",
    "roe", "roa", "debt_ratio", "current_ratio", "ocf",
    "total_asset", "total_equity", "eps", "bps", "ocf_ps",
]


def _f(v):
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return x


RATIO_FIELDS = {
    "revenue_yoy", "net_profit_yoy", "deduct_net_profit_yoy",
    "gross_margin", "net_margin", "roe", "roa", "debt_ratio",
}
RATIO_LIMIT = 100000.0  # 超过该量级的比率无业务意义（净利趋零时同比会爆炸）


def _clean_ratio(field, v):
    v = _f(v)
    if v is None:
        return None
    if field in RATIO_FIELDS and abs(v) > RATIO_LIMIT:
        return None
    return v


def _to_date(s):
    try:
        s = str(s)
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except (ValueError, IndexError):
        return None


def estimate_ann_date(period: date) -> date:
    """按法定披露截止日推定公告日。"""
    y, m = period.year, period.month
    if m == 3:
        return date(y, 4, 30)
    if m == 6:
        return date(y, 8, 31)
    if m == 9:
        return date(y, 10, 31)
    return date(y + 1, 4, 30)  # 年报


def parse_financial(code: str, raw) -> list:
    """把 abstract 宽表转为 fact_financial 行列表。"""
    if raw is None or len(raw) == 0 or "指标" not in raw.columns:
        return []

    df = raw.drop_duplicates(subset=["指标"], keep="first").set_index("指标")
    period_cols = [c for c in df.columns if re.fullmatch(r"\d{8}", str(c))]
    period_cols.sort()

    # 预取每个指标在各期的值，便于同比补算
    values = {}
    for field, src_name in FIELD_MAP.items():
        if src_name in df.index:
            values[field] = {str(p): _clean_ratio(field, df.loc[src_name, p]) for p in period_cols}

    rows = []
    for i, pc in enumerate(period_cols):
        period = _to_date(pc)
        if period is None:
            continue
        ann = estimate_ann_date(period)

        # 扣非净利同比：与四个季度前的同报告期比较
        deduct_yoy = None
        if i >= 4 and "deduct_net_profit" in values:
            cur_v = values["deduct_net_profit"].get(str(pc))
            prev_v = values["deduct_net_profit"].get(str(period_cols[i - 4]))
            if cur_v is not None and prev_v not in (None, 0):
                deduct_yoy = round((cur_v / prev_v - 1) * 100, 3)
                if abs(deduct_yoy) > RATIO_LIMIT:
                    deduct_yoy = None

        row = {
            "stock_code": code,
            "report_period": period,
            "ann_date": ann,
            "ann_date_source": "estimated",
            "deduct_net_profit_yoy": deduct_yoy,
            "total_asset": None,
        }
        for field in FIELD_MAP:
            row[field] = values.get(field, {}).get(str(pc))
        rows.append(tuple(row[c] for c in COLUMNS))
    return rows


def sync_financial(codes=None, limit=None, source: AkshareSource = None,
                   progress_every: int = 50, skip_existing: bool = True):
    """同步财务数据。

    skip_existing: 跳过库中已有财报的标的。采集耗时长，服务重启后重跑时
    只补缺口，避免每次都从头开始；需要更新已有财报时传 False。
    """
    src = source or AkshareSource()

    if codes is None:
        q = "SELECT stock_code FROM dim_stock WHERE list_status='L' ORDER BY stock_code"
        if limit:
            q += f" LIMIT {int(limit)}"
        codes = db.query_df(q)["stock_code"].tolist()
    else:
        codes = list(codes)[: limit or None]

    if skip_existing:
        done = db.query_df("SELECT DISTINCT stock_code FROM fact_financial")
        done_set = set(done["stock_code"]) if not done.empty else set()
        before = len(codes)
        codes = [c for c in codes if c not in done_set]
        print(f"  跳过已有财报 {before - len(codes)} 只，待采 {len(codes)} 只", flush=True)

    if not codes:
        return {"stocks": 0, "ok": 0, "fail": 0, "financial_rows": 0,
                "note": "全部标的已有财报，如需强制更新请用 financial_full"}

    task_id = db.log_task_start("sync_financial", "financial", f"n={len(codes)}")
    db.log_task_progress(task_id, total_cnt=len(codes), success_cnt=0, fail_cnt=0)
    ok, fail, total_rows, fail_list = 0, 0, 0, []

    for idx, code in enumerate(codes, 1):
        try:
            raw = src.get_financial(code)
            rows = parse_financial(code, raw)
            if rows:
                total_rows += db.bulk_upsert(
                    "fact_financial",
                    COLUMNS,
                    rows,
                    update_columns=[c for c in COLUMNS if c not in ("stock_code", "report_period", "ann_date")],
                )
            ok += 1
        except Exception as exc:  # noqa: BLE001
            fail += 1
            fail_list.append(f"{code}: {exc}")
        if idx % progress_every == 0:
            print(f"  进度 {idx}/{len(codes)}  成功 {ok} 失败 {fail} 行数 {total_rows}", flush=True)
            db.log_task_progress(task_id, success_cnt=idx, fail_cnt=fail)

    status = "success" if fail == 0 else ("partial" if ok else "failed")
    db.log_task_end(task_id, status, len(codes), ok, fail, "; ".join(fail_list[:5]))
    return {
        "stocks": len(codes), "ok": ok, "fail": fail, "financial_rows": total_rows,
        "fail_samples": fail_list[:5],
    }
