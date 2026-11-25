# -*- coding: utf-8 -*-
"""数据质量校验：DQ-01 ~ DQ-07。

异常写入 sys_data_quality，供界面查询与人工复核；不静默丢弃数据。
"""
from .. import config, db

RULES = {
    "DQ-01": "OHLC 关系不成立",
    "DQ-02": "价格或量额为负/零",
    "DQ-03": "涨跌幅超出板块限制",
    "DQ-04": "交易日行情缺失",
    "DQ-05": "财务报告期重复",
    "DQ-06": "复权因子异常",
    "DQ-07": "全市场覆盖率不足",
}


def _record(rule, rows):
    if not rows:
        return 0
    payload = [(rule, r.get("stock_code"), r.get("trade_date"), r.get("detail")) for r in rows]
    with db.engine.begin() as conn:
        from sqlalchemy import text

        conn.execute(
            text(
                "INSERT INTO sys_data_quality (rule_code, stock_code, trade_date, detail) "
                "VALUES (:r, :s, :d, :x)"
            ),
            [{"r": a, "s": b, "d": c, "x": (d or "")[:500]} for a, b, c, d in payload],
        )
    return len(rows)


def check_ohlc(trade_date):
    df = db.query_df(
        """
        SELECT stock_code, trade_date, open_price, high_price, low_price, close_price
        FROM fact_daily_quote
        WHERE trade_date = :d AND close_price IS NOT NULL
          AND (low_price > LEAST(open_price, close_price)
               OR high_price < GREATEST(open_price, close_price)
               OR low_price > high_price)
        """,
        {"d": trade_date},
    )
    rows = [
        {"stock_code": r.stock_code, "trade_date": trade_date,
         "detail": f"O={r.open_price} H={r.high_price} L={r.low_price} C={r.close_price}"}
        for r in df.itertuples()
    ]
    return _record("DQ-01", rows)


def check_price_positive(trade_date):
    df = db.query_df(
        """
        SELECT stock_code, open_price, high_price, low_price, close_price, volume, amount
        FROM fact_daily_quote
        WHERE trade_date = :d
          AND (close_price <= 0 OR open_price <= 0 OR high_price <= 0 OR low_price <= 0
               OR volume < 0 OR amount < 0)
        """,
        {"d": trade_date},
    )
    rows = [
        {"stock_code": r.stock_code, "trade_date": trade_date,
         "detail": f"C={r.close_price} V={r.volume} A={r.amount}"}
        for r in df.itertuples()
    ]
    return _record("DQ-02", rows)


def check_limit(trade_date):
    """涨跌幅超出所属板块限制 + 容差。新股上市初期按无限制放开。"""
    df = db.query_df(
        """
        SELECT q.stock_code, q.pct_chg, s.board, s.is_st
        FROM fact_daily_quote q
        JOIN dim_stock s ON s.stock_code = q.stock_code
        WHERE q.trade_date = :d AND q.pct_chg IS NOT NULL
        """,
        {"d": trade_date},
    )
    rows = []
    for r in df.itertuples():
        limit = config.ST_LIMIT if r.is_st else config.LIMIT_RULES.get(r.board, 10.0)
        # 新股与北交所首日等情形允许超限，这里只标记显著异常
        if abs(float(r.pct_chg)) > limit + 5.0:
            rows.append(
                {"stock_code": r.stock_code, "trade_date": trade_date,
                 "detail": f"pct={r.pct_chg} limit={limit}"}
            )
    return _record("DQ-03", rows)


def check_adj_factor(trade_date):
    """复权因子应大于 0；相邻日比值不应剧烈跳变（除权日除外，此处仅做量级检查）。"""
    df = db.query_df(
        """
        SELECT stock_code, close_price, adj_factor
        FROM fact_daily_quote
        WHERE trade_date = :d AND adj_factor IS NOT NULL
          AND (adj_factor <= 0 OR adj_factor > 1000)
        """,
        {"d": trade_date},
    )
    rows = [
        {"stock_code": r.stock_code, "trade_date": trade_date,
         "detail": f"factor={r.adj_factor} close={r.close_price}"}
        for r in df.itertuples()
    ]
    return _record("DQ-06", rows)


def check_coverage(trade_date):
    """覆盖率 = 当日有行情标的数 / 在市标的数。低于 99.5% 记为异常。"""
    quoted = db.scalar(
        "SELECT COUNT(DISTINCT stock_code) FROM fact_daily_quote WHERE trade_date=:d",
        {"d": trade_date}, default=0,
    )
    listed = db.scalar(
        "SELECT COUNT(*) FROM dim_stock WHERE list_status='L' "
        "AND (list_date IS NULL OR list_date <= :d)",
        {"d": trade_date}, default=0,
    )
    if not listed:
        return {"quoted": quoted, "listed": listed, "coverage": None, "passed": False}
    coverage = quoted / listed
    passed = coverage >= 0.995
    if not passed:
        _record(
            "DQ-07",
            [{"stock_code": None, "trade_date": trade_date,
              "detail": f"覆盖率 {coverage:.4f}（{quoted}/{listed}）"}],
        )
    return {
        "quoted": quoted, "listed": listed,
        "coverage": round(coverage, 4), "passed": passed,
    }


def run_all(trade_date):
    return {
        "trade_date": str(trade_date),
        "DQ-01": check_ohlc(trade_date),
        "DQ-02": check_price_positive(trade_date),
        "DQ-03": check_limit(trade_date),
        "DQ-06": check_adj_factor(trade_date),
        "coverage": check_coverage(trade_date),
    }
