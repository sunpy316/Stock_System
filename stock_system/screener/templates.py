# -*- coding: utf-8 -*-
"""内置策略模板（需求文档附录 B）。"""

TEMPLATES = {
    "T01": {
        "name": "均线多头 + 温和放量",
        "desc": "MA5>MA10>MA20>MA60，量比 1.2—3，换手 1%—8%",
        "condition": {
            "logic": "AND",
            "children": [
                {"field": "ma5", "op": "gt", "value": "ma10"},
                {"field": "ma10", "op": "gt", "value": "ma20"},
                {"field": "ma20", "op": "gt", "value": "ma60"},
                {"field": "vol_ratio", "op": "between", "value": [1.2, 3.0]},
                {"field": "turnover_rate", "op": "between", "value": [1, 8]},
            ],
        },
    },
    "T02": {
        "name": "放量突破平台",
        "desc": "创 60 日新高，量比 >1.5，流通市值 30—300 亿",
        "condition": {
            "logic": "AND",
            "children": [
                {"field": "close_price", "op": "gte", "value": "high_60d"},
                {"field": "vol_ratio", "op": "gt", "value": 1.5},
                {"field": "total_mv_yi", "op": "between", "value": [30, 300]},
            ],
        },
    },
    "T03": {
        "name": "低估值高盈利",
        "desc": "PE 0—20，ROE ≥15%，资产负债率 ≤60%",
        "condition": {
            "logic": "AND",
            "children": [
                {"field": "pe_ttm", "op": "between", "value": [0, 20]},
                {"field": "roe", "op": "gte", "value": 15},
                {"field": "debt_ratio", "op": "lte", "value": 60},
            ],
        },
    },
    "T04": {
        "name": "业绩加速成长",
        "desc": "净利同比 >30%，营收同比 >20%，毛利率 >25%",
        "condition": {
            "logic": "AND",
            "children": [
                {"field": "net_profit_yoy", "op": "gt", "value": 30},
                {"field": "revenue_yoy", "op": "gt", "value": 20},
                {"field": "gross_margin", "op": "gt", "value": 25},
            ],
        },
    },
    "T05": {
        "name": "超跌反弹",
        "desc": "20 日跌幅 >20%，RSI6 <25，当日收阳",
        "condition": {
            "logic": "AND",
            "children": [
                {"field": "pct_chg_20d", "op": "lte", "value": -20},
                {"field": "rsi6", "op": "lt", "value": 25},
                {"field": "pct_chg_adj", "op": "gt", "value": 0},
            ],
        },
    },
    "T06": {
        "name": "强势整理",
        "desc": "60 日涨幅 >30%，量比 <1.2，站上 MA20",
        "condition": {
            "logic": "AND",
            "children": [
                {"field": "pct_chg_60d", "op": "gt", "value": 30},
                {"field": "vol_ratio", "op": "lt", "value": 1.2},
                {"field": "close_price", "op": "gt", "value": "ma20"},
            ],
        },
    },
    "T07": {
        "name": "红利低波稳健防御",
        "desc": "PE 0—18，ROE ≥12%，负债率 ≤65%，换手率 0.5%—4%",
        "condition": {
            "logic": "AND",
            "children": [
                {"field": "pe_ttm", "op": "between", "value": [0, 18]},
                {"field": "roe", "op": "gte", "value": 12},
                {"field": "debt_ratio", "op": "lte", "value": 65},
                {"field": "turnover_rate", "op": "between", "value": [0.5, 4.0]},
            ],
        },
    },
    "T08": {
        "name": "景气成长高低切",
        "desc": "营收/净利同比 >10%，距60日高点回撤 12%—35%，放量收阳企稳",
        "condition": {
            "logic": "AND",
            "children": [
                {"field": "revenue_yoy", "op": "gt", "value": 10},
                {"field": "net_profit_yoy", "op": "gt", "value": 10},
                {"field": "drawdown_60d", "op": "between", "value": [12, 35]},
                {"field": "vol_ratio", "op": "gt", "value": 1.2},
                {"field": "pct_chg_adj", "op": "gt", "value": 0},
            ],
        },
    },
    "T09": {
        "name": "相对强势主升",
        "desc": "20日跑赢市场 >5%，MA5>MA10>MA20，量比 1.2—3.5",
        "condition": {
            "logic": "AND",
            "children": [
                {"field": "rs_20d", "op": "gt", "value": 5},
                {"field": "ma5", "op": "gt", "value": "ma10"},
                {"field": "ma10", "op": "gt", "value": "ma20"},
                {"field": "vol_ratio", "op": "between", "value": [1.2, 3.5]},
            ],
        },
    },
}

# 可直接用于界面的指标清单（字段名, 中文名, 分类）
INDICATOR_META = [
    ("close_price", "最新价", "行情"),
    ("pct_chg_adj", "涨跌幅", "行情"),
    ("turnover_rate", "换手率", "行情"),
    ("amount", "成交额", "行情"),
    ("total_mv_yi", "总市值", "规模"),
    ("float_mv_yi", "流通市值", "规模"),
    ("vol_ratio", "量比", "量能"),
    ("vol_ma5", "5日均量", "量能"),
    ("pct_chg_5d", "5日涨幅", "价格"),
    ("pct_chg_20d", "20日涨幅", "价格"),
    ("pct_chg_60d", "60日涨幅", "价格"),
    ("high_60d", "60日最高", "价格"),
    ("drawdown_60d", "60日高点回撤%", "价格"),
    ("pct_from_low_60d", "60日低点反弹%", "价格"),
    ("rs_20d", "20日相对强度", "趋势"),
    ("ma5", "MA5", "均线"),
    ("ma10", "MA10", "均线"),
    ("ma20", "MA20", "均线"),
    ("ma60", "MA60", "均线"),
    ("ma120", "MA120", "均线"),
    ("ma250", "MA250", "均线"),
    ("macd_dif", "MACD DIF", "趋势"),
    ("macd_dea", "MACD DEA", "趋势"),
    ("macd_bar", "MACD 柱", "趋势"),
    ("kdj_k", "KDJ K", "摆动"),
    ("kdj_d", "KDJ D", "摆动"),
    ("kdj_j", "KDJ J", "摆动"),
    ("rsi6", "RSI6", "摆动"),
    ("rsi12", "RSI12", "摆动"),
    ("rsi24", "RSI24", "摆动"),
    ("boll_up", "BOLL 上轨", "通道"),
    ("boll_mid", "BOLL 中轨", "通道"),
    ("boll_low", "BOLL 下轨", "通道"),
    ("atr14", "ATR14", "波动"),
    ("pe_ttm", "PE(TTM)", "估值"),
    ("pb", "PB", "估值"),
    ("roe", "ROE(TTM)", "盈利"),
    ("roa", "ROA", "盈利"),
    ("gross_margin", "毛利率", "盈利"),
    ("net_margin", "净利率", "盈利"),
    ("revenue_yoy", "营收同比", "成长"),
    ("net_profit_yoy", "净利同比", "成长"),
    ("debt_ratio", "资产负债率", "财务"),
]
