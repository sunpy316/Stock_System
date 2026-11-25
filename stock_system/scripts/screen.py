# -*- coding: utf-8 -*-
"""筛选命令行入口。

示例：
    python -m stock_system.scripts.screen --template T01
    python -m stock_system.scripts.screen --file cond.json --date 2026-10-30
    python -m stock_system.scripts.screen --json '{"logic":"AND","children":[...]}'
"""
import argparse
import json
import sys

sys.stdout.reconfigure(encoding="utf-8")

from ..screener import ScreenError, run_screen

TEMPLATES = {
    "T01": {
        "logic": "AND",
        "children": [
            {"field": "ma5", "op": "gt", "value": "ma10"},
            {"field": "ma10", "op": "gt", "value": "ma20"},
            {"field": "ma20", "op": "gt", "value": "ma60"},
            {"field": "vol_ratio", "op": "between", "value": [1.2, 3.0]},
            {"field": "turnover_rate", "op": "between", "value": [1, 8]},
        ],
    },
    "T02": {
        "logic": "AND",
        "children": [
            {"field": "close_price", "op": "gte", "value": "high_60d"},
            {"field": "vol_ratio", "op": "gt", "value": 1.5},
            {"field": "total_mv_yi", "op": "between", "value": [30, 300]},
        ],
    },
    "T03": {
        "logic": "AND",
        "children": [
            {"field": "pe_ttm", "op": "between", "value": [0, 20]},
            {"field": "roe", "op": "gte", "value": 15},
            {"field": "debt_ratio", "op": "lte", "value": 60},
        ],
    },
    "T04": {
        "logic": "AND",
        "children": [
            {"field": "net_profit_yoy", "op": "gt", "value": 30},
            {"field": "revenue_yoy", "op": "gt", "value": 20},
            {"field": "gross_margin", "op": "gt", "value": 25},
        ],
    },
    "T05": {
        "logic": "AND",
        "children": [
            {"field": "pct_chg_20d", "op": "lte", "value": -20},
            {"field": "rsi6", "op": "lt", "value": 25},
            {"field": "pct_chg_adj", "op": "gt", "value": 0},
        ],
    },
    "T06": {
        "logic": "AND",
        "children": [
            {"field": "pct_chg_60d", "op": "gt", "value": 30},
            {"field": "vol_ratio", "op": "lt", "value": 1.2},
            {"field": "close_price", "op": "gt", "value": "ma20"},
        ],
    },
}


def fmt(v, digits=2):
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{digits}f}"
    return str(v)


def main():
    p = argparse.ArgumentParser(description="条件选股")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--template", choices=sorted(TEMPLATES))
    g.add_argument("--file", help="条件树 JSON 文件")
    g.add_argument("--json", dest="inline", help="条件树 JSON 字符串")
    p.add_argument("--date", default=None, help="数据日期 YYYY-MM-DD")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--no-persist", action="store_true")
    p.add_argument("--show-detail", action="store_true", help="打印命中明细")
    args = p.parse_args()

    if args.template:
        cond = TEMPLATES[args.template]
        label = args.template
    elif args.file:
        cond = json.load(open(args.file, encoding="utf-8"))
        label = args.file
    else:
        cond = json.loads(args.inline)
        label = "inline"

    try:
        res = run_screen(cond, trade_date=args.date, limit=args.limit,
                         persist=not args.no_persist)
    except ScreenError as e:
        print(f"[条件错误] {e}")
        sys.exit(1)

    print(f"\n策略 {label}   数据日期 {res['data_date']}   "
          f"候选 {res['candidate']}   命中 {res['hit']}   耗时 {res['elapsed_ms']}ms")
    if not res["items"]:
        print("（无命中）")
        return

    head = f"{'排名':<4}{'代码':<8}{'名称':<10}{'收盘':>9}{'涨跌%':>8}{'换手%':>8}{'市值亿':>10}{'PE':>8}{'ROE':>7}{'评分':>7}"
    print(head)
    print("-" * 86)
    for it in res["items"]:
        print(
            f"{it['rank']:<4}{it['stock_code']:<8}{it['stock_name']:<10}"
            f"{fmt(it['close_price']):>9}{fmt(it['pct_chg']):>8}{fmt(it['turnover_rate']):>8}"
            f"{fmt(it['total_mv_yi'], 0):>10}{fmt(it['pe_ttm'], 1):>8}{fmt(it['roe'], 1):>7}"
            f"{fmt(it['score'], 1):>7}"
        )

    if args.show_detail and res["items"]:
        it = res["items"][0]
        print(f"\n命中明细 · {it['stock_name']} ({it['stock_code']})")
        for d in it["detail"]:
            flag = "✓" if d["pass"] else "✕"
            print(f"  {flag} {d['field']} {d['op']} {d['threshold']}  → 实际 {d['value']}")


if __name__ == "__main__":
    main()
