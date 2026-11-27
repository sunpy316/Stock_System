# -*- coding: utf-8 -*-
"""筛选引擎：条件树解析 + 向量化求值 + 命中明细。

条件树形如：
    {"logic":"AND","children":[
        {"field":"ma5","op":"gt","value":"ma20"},
        {"field":"turnover_rate","op":"between","value":[1,8]},
        {"logic":"OR","children":[...]}
    ]}
value 支持三种形态：常量、另一个指标名（字符串，表示指标间比较）、区间数组。
"""
import json
import time

import pandas as pd

from .. import db
from . import cross_section

NUMERIC_OPS = {"gt", "gte", "lt", "lte", "eq", "neq"}

DEFAULT_FILTERS = {
    "exclude_st": True,
    "exclude_suspended": True,
    "min_list_days": 60,
    "min_amount": 1000.0,  # 万元
    "exclude_limit": False,
}


class ScreenError(Exception):
    pass


def _series(df, field):
    if field not in df.columns:
        raise ScreenError(f"未知指标：{field}")
    s = df[field]
    if s.dtype == object:
        s = pd.to_numeric(s, errors="coerce")
    return s


def _rhs(df, value):
    """右值可以是常量，也可以是另一个指标列名。"""
    if isinstance(value, str) and value in df.columns:
        return _series(df, value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            raise ScreenError(f"阈值无法识别：{value}")
    return value


def eval_condition(node, df):
    """单个叶子条件 → 布尔 Series。"""
    field = node.get("field")
    op = node.get("op")
    value = node.get("value")
    if not field or not op:
        raise ScreenError(f"条件缺少 field 或 op：{node}")

    s = _series(df, field)

    if op in NUMERIC_OPS:
        v = _rhs(df, value)
        return {
            "gt": lambda: s > v,
            "gte": lambda: s >= v,
            "lt": lambda: s < v,
            "lte": lambda: s <= v,
            "eq": lambda: s == v,
            "neq": lambda: s != v,
        }[op]()

    if op in ("between", "not_between"):
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ScreenError(f"between 需要 [下限, 上限]，收到：{value}")
        lo, hi = float(value[0]), float(value[1])
        m = (s >= lo) & (s <= hi)
        return m if op == "between" else ~m

    if op in ("in", "not_in"):
        if not isinstance(value, (list, tuple)):
            raise ScreenError(f"{op} 需要数组，收到：{value}")
        m = s.isin(list(value))
        return m if op == "in" else ~m

    if op in ("is_true", "is_false"):
        m = s.astype(bool)
        return m if op == "is_true" else ~m

    if op == "pct_between":
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ScreenError("pct_between 需要 [下限, 上限]（百分位 0-100）")
        r = s.rank(pct=True) * 100
        return (r >= float(value[0])) & (r <= float(value[1]))

    if op == "top_n":
        n = int(value)
        if n <= 0 or s.notna().sum() == 0:
            return pd.Series(False, index=df.index)
        thr = s.nlargest(n).min()
        return s >= thr

    if op in ("cross_above", "cross_below"):
        raise ScreenError(
            "cross_above / cross_below 需要时间序列，横截面视图不支持；"
            "请改用引擎预计算的穿越标记字段"
        )

    raise ScreenError(f"不支持的运算符：{op}")


def eval_tree(node, df):
    if not node:
        return pd.Series(True, index=df.index)
    if "logic" in node:
        children = node.get("children") or []
        if not children:
            return pd.Series(True, index=df.index)
        result = eval_tree(children[0], df)
        for child in children[1:]:
            if node["logic"].upper() == "AND":
                result = result & eval_tree(child, df)
            elif node["logic"].upper() == "OR":
                result = result | eval_tree(child, df)
            else:
                raise ScreenError(f"未知逻辑：{node['logic']}")
        return result
    return eval_condition(node, df)


def collect_leaves(node, out=None):
    out = [] if out is None else out
    if not node:
        return out
    if "logic" in node:
        for c in node.get("children") or []:
            collect_leaves(c, out)
    else:
        out.append(node)
    return out


def apply_filters(df, cfg=None):
    cfg = {**DEFAULT_FILTERS, **(cfg or {})}
    mask = df["close_price"].notna()
    if cfg.get("exclude_st"):
        mask &= df["is_st"].fillna(0).astype(int) == 0
    if cfg.get("min_list_days"):
        # 上市日期缺失（如北交所部分标的）时保留，避免误杀
        ld = df["list_days"]
        mask &= ld.isna() | (ld >= float(cfg["min_list_days"]))
    if cfg.get("min_amount"):
        mask &= df["amount"].fillna(0) >= float(cfg["min_amount"]) * 1e4
    if cfg.get("exclude_limit"):
        mask &= df["limit_status"].fillna("NORMAL") == "NORMAL"
    return df[mask].copy()


def _jsonable(v):
    if v is None:
        return None
    if isinstance(v, float):
        if pd.isna(v):
            return None
        return round(v, 6)
    if pd.isna(v) if not isinstance(v, (list, dict)) else False:
        return None
    return v


def run_screen(condition, trade_date=None, filters=None, sort_by="score", limit=500,
               persist=True, strategy_id=None, run_type="manual"):
    """执行筛选，返回结果字典。"""
    t0 = time.time()
    d = trade_date or db.scalar("SELECT MAX(trade_date) FROM fact_indicator_daily")
    if d is None:
        raise ScreenError("尚无指标数据，请先执行指标计算")

    df = cross_section.build(d)
    if df.empty:
        raise ScreenError(f"{d} 无横截面数据")

    cand = apply_filters(df, filters)
    if cand.empty:
        return {"data_date": str(d), "candidate": 0, "hit": 0, "items": [], "elapsed_ms": 0}

    leaves = collect_leaves(condition)
    # 逐叶子预求值，既用于组合，也用于命中明细
    leaf_masks = {}
    for i, lf in enumerate(leaves):
        leaf_masks[i] = eval_condition(lf, cand).reindex(cand.index).fillna(False)

    mask = eval_tree(condition, cand) if condition else pd.Series(True, index=cand.index)
    mask = mask.reindex(cand.index).fillna(False)

    hits = cand[mask].copy()
    if hits.empty:
        elapsed = int((time.time() - t0) * 1000)
        return {
            "data_date": str(d), "candidate": len(cand), "hit": 0,
            "items": [], "elapsed_ms": elapsed,
        }

    # 评分：多维综合打分（盈利质量 + 估值安全 + 相对强度 + 量价低拥挤），各占 25%
    roe_s = pd.to_numeric(hits.get("roe"), errors="coerce").rank(pct=True).fillna(0.5)
    pe_raw = pd.to_numeric(hits.get("pe_ttm"), errors="coerce")
    pe_valid = pe_raw.where(pe_raw > 0)
    pe_s = (1.0 - pe_valid.rank(pct=True)).fillna(0.5)
    rs_col = hits.get("rs_20d") if "rs_20d" in hits.columns else hits.get("pct_chg_20d")
    rs_s = pd.to_numeric(rs_col, errors="coerce").rank(pct=True).fillna(0.5)
    to_raw = pd.to_numeric(hits.get("turnover_rate"), errors="coerce")
    # 适度换手且不过度拥挤得分更高
    crowd_s = (1.0 - (to_raw - 3.0).abs().rank(pct=True)).fillna(0.5)
    hits["score"] = ((roe_s * 0.25 + pe_s * 0.25 + rs_s * 0.25 + crowd_s * 0.25) * 100).round(2)
    # 真实命中总数必须在截断之前取，否则「命中」会等于 limit，误导判断
    total_hits = int(len(hits))
    hits["hit_cnt"] = len(leaves)

    if sort_by in hits.columns:
        hits = hits.sort_values(sort_by, ascending=False)
    hits = hits.head(limit)

    items = []
    for rank, (idx, row) in enumerate(hits.iterrows(), 1):
        detail = []
        for i, lf in enumerate(leaves):
            ok = bool(leaf_masks[i].get(idx, False))
            detail.append({
                "field": lf.get("field"),
                "op": lf.get("op"),
                "threshold": lf.get("value"),
                "value": _jsonable(row.get(lf.get("field"))),
                "pass": ok,
            })
        items.append({
            "rank": rank,
            "stock_code": row["stock_code"],
            "stock_name": row["stock_name"],
            "board": row["board"],
            "close_price": _jsonable(row.get("close_price")),
            "pct_chg": _jsonable(row.get("pct_chg_adj")),
            "turnover_rate": _jsonable(row.get("turnover_rate")),
            "amount_yi": _jsonable((row.get("amount") or 0) / 1e8),
            "total_mv_yi": _jsonable(row.get("total_mv_yi")),
            "pe_ttm": _jsonable(row.get("pe_ttm")),
            "roe": _jsonable(row.get("roe")),
            "revenue_yoy": _jsonable(row.get("revenue_yoy")),
            "pct_chg_20d": _jsonable(row.get("pct_chg_20d")),
            "drawdown_60d": _jsonable(row.get("drawdown_60d")),
            "rs_20d": _jsonable(row.get("rs_20d")),
            "vol_ratio": _jsonable(row.get("vol_ratio")),
            "vol_ratio_20": _jsonable(row.get("vol_ratio_20")),
            "consol_range_20": _jsonable(row.get("consol_range_20")),
            "bias_ma20": _jsonable(row.get("bias_ma20")),
            "pocket_pivot": _jsonable(row.get("pocket_pivot")),
            "upper_shadow_ratio": _jsonable(row.get("upper_shadow_ratio")),
            "breakout_ref": _jsonable(row.get("high_20d_prev")),
            "stop_loss_ref": _jsonable(row.get("ma20")),
            "low_price": _jsonable(row.get("low_price")),
            "risk_tag": (
                "偏离较大-宜等回踩"
                if row.get("bias_ma20") is not None and pd.notna(row.get("bias_ma20")) and float(row.get("bias_ma20")) > 12.0
                else ""
            ),
            "limit_status": row.get("limit_status"),
            "score": _jsonable(row["score"]),
            "detail": detail,
        })

    elapsed = int((time.time() - t0) * 1000)

    run_id = None
    if persist:
        run_id = _persist(d, condition, filters, len(cand), total_hits, elapsed, items,
                          strategy_id, run_type)

    return {
        "run_id": run_id,
        "data_date": str(d),
        "candidate": len(cand),
        "hit": total_hits,
        "returned": len(items),
        "elapsed_ms": elapsed,
        "items": items,
    }


def _persist(data_date, condition, filters, cand_cnt, hit_cnt, elapsed, items,
             strategy_id=None, run_type="manual"):
    with db.engine.begin() as conn:
        from sqlalchemy import text

        res = conn.execute(
            text(
                "INSERT INTO fact_screen_run "
                "(strategy_id, run_type, data_date, condition_json, filter_json, "
                "candidate_cnt, hit_cnt, elapsed_ms) "
                "VALUES (:sid, :rt, :d, :c, :f, :cc, :hc, :e)"
            ),
            {
                "sid": strategy_id, "rt": run_type,
                "d": data_date, "c": json.dumps(condition, ensure_ascii=False),
                "f": json.dumps(filters or {}, ensure_ascii=False),
                "cc": cand_cnt, "hc": hit_cnt, "e": elapsed,
            },
        )
        run_id = res.lastrowid

    rows = [
        (
            run_id, it["stock_code"], it["score"],
            # 注意：不能写 `it["detail"] and len(...)`。空列表是假值，
            # 该表达式会原样返回 []，PyMySQL 把它格式化成 () 拼进 SQL，
            # 报 1064 语法错。零条件筛选（detail 全空）时必踩。
            len(it["detail"] or []),
            json.dumps(it["detail"], ensure_ascii=False), it["rank"],
        )
        for it in items
    ]
    db.bulk_upsert(
        "fact_screen_result",
        ["run_id", "stock_code", "score", "hit_cnt", "hit_detail", "rank_no"],
        rows,
    )
    return run_id
