# -*- coding: utf-8 -*-
"""采集任务命令行入口。

示例：
    python -m stock_system.scripts.sync --task base
    python -m stock_system.scripts.sync --task quote --limit 30 --start 20261001
    python -m stock_system.scripts.sync --task financial --limit 30
    python -m stock_system.scripts.sync --task check --date 2026-10-30
"""
import argparse
import json
import sys
from datetime import date

sys.stdout.reconfigure(encoding="utf-8")

from .. import db
from ..datasource.akshare_source import AkshareSource
from ..collectors.calendar import sync_calendar
from ..collectors.daily_quote import sync_daily_quote
from ..collectors.financial import sync_financial
from ..collectors.stock_list import sync_share_capital, sync_stock_list
from ..quality.checks import run_all


def main():
    p = argparse.ArgumentParser(description="A股数据采集")
    p.add_argument("--task", required=True,
                   choices=["base", "stock_list", "shares", "calendar", "quote", "financial",
                            "indicator", "check"])
    p.add_argument("--limit", type=int, default=None, help="限制股票数量（调试用）")
    p.add_argument("--codes", default=None, help="指定股票代码，逗号分隔")
    p.add_argument("--start", default=None, help="起始日期 YYYYMMDD")
    p.add_argument("--end", default=None, help="结束日期 YYYYMMDD")
    p.add_argument("--date", default=None, help="校验日期 YYYY-MM-DD")
    p.add_argument("--with-info", type=int, default=0, help="补拉前 N 只新股概况")
    p.add_argument("--chunk-size", type=int, default=0,
                   help="分片大小，0=不分片。熔断时按片回退，建议 300")
    p.add_argument("--provider", default=None, choices=["em", "tx"],
                   help="行情源：em=东方财富，tx=腾讯。默认取配置 STOCK_QUOTE_PROVIDER")
    p.add_argument("--progress", default=None,
                   help="断点文件路径（配合 --chunk-size）。默认 logs/.quote_progress")
    args = p.parse_args()

    codes = [c.strip() for c in args.codes.split(",")] if args.codes else None
    result = {}

    if args.task in ("base", "stock_list"):
        result["stock_list"] = sync_stock_list(with_info=args.with_info)
    if args.task in ("base", "shares"):
        result["shares"] = sync_share_capital()
    if args.task in ("base", "calendar"):
        result["calendar"] = sync_calendar()
    if args.task == "quote":
        progress = args.progress
        if args.chunk_size and not progress:
            progress = "logs/.quote_progress"
        src = AkshareSource(provider=args.provider) if args.provider else None
        result["quote"] = sync_daily_quote(
            codes=codes, start=args.start, end=args.end, limit=args.limit,
            chunk_size=args.chunk_size, progress_file=progress, source=src,
        )
    if args.task == "financial":
        result["financial"] = sync_financial(codes=codes, limit=args.limit)
    if args.task == "indicator":
        from ..indicators.runner import run as calc_indicator

        result["indicator"] = calc_indicator(start=args.start, end=args.end, codes=codes)
    if args.task == "check":
        d = args.date or str(db.scalar("SELECT MAX(trade_date) FROM fact_daily_quote"))
        result["check"] = run_all(d)

    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    # 限流熔断用退出码 3 告知外层脚本：等一会儿再跑，会从断点续上
    if any(isinstance(v, dict) and v.get("rate_limited") for v in result.values()):
        sys.exit(3)


if __name__ == "__main__":
    main()
