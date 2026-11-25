# -*- coding: utf-8 -*-
"""akshare 接口连通性探测：确认四个关键接口可用并锁定字段名。"""
import sys
import time
import traceback

sys.stdout.reconfigure(encoding="utf-8")
import akshare as ak

print("akshare", ak.__version__)


def probe(name, fn):
    t0 = time.time()
    try:
        df = fn()
        cost = time.time() - t0
        print(f"\n[OK] {name}  耗时 {cost:.2f}s  行数 {len(df)}")
        print("  字段:", list(df.columns))
        if len(df):
            print("  首行:", df.iloc[0].to_dict())
        return df
    except Exception as e:
        cost = time.time() - t0
        print(f"\n[FAIL] {name}  耗时 {cost:.2f}s  {type(e).__name__}: {e}")
        traceback.print_exc(limit=2)
        return None


probe("股票列表 stock_info_a_code_name", lambda: ak.stock_info_a_code_name())

probe("交易日历 tool_trade_date_hist_sina", lambda: ak.tool_trade_date_hist_sina())

probe(
    "日线 stock_zh_a_hist(600519, 20261020-20261030)",
    lambda: ak.stock_zh_a_hist(
        symbol="600519", period="daily", start_date="20261020", end_date="20261030", adjust=""
    ),
)

probe("财务摘要 stock_financial_abstract(600519)", lambda: ak.stock_financial_abstract(symbol="600519"))

probe("个股信息 stock_individual_info_em(600519)", lambda: ak.stock_individual_info_em(symbol="600519"))
