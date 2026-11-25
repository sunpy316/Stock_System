# -*- coding: utf-8 -*-
"""数据源适配层接口定义。

上层采集器只依赖本接口，替换数据源（如 Tushare）时只需新增实现，
不修改采集器与筛选引擎。
"""
from abc import ABC, abstractmethod

import pandas as pd


class BaseDataSource(ABC):
    """行情与财务数据源抽象。所有方法返回字段名已标准化的 DataFrame。"""

    @abstractmethod
    def get_stock_list(self) -> pd.DataFrame:
        """全市场股票列表：code, name。"""

    @abstractmethod
    def get_trade_calendar(self) -> pd.DataFrame:
        """交易日历：trade_date（datetime.date），仅包含交易日。"""

    @abstractmethod
    def get_daily_quote(self, code: str, start: str, end: str, adjust: str = "") -> pd.DataFrame:
        """单只股票日线。

        adjust: "" 不复权 / "qfq" 前复权 / "hfq" 后复权
        返回列：trade_date, open_price, high_price, low_price, close_price,
                pre_close, volume, amount, amplitude, pct_chg, chg, turnover_rate
        """

    @abstractmethod
    def get_financial(self, code: str) -> pd.DataFrame:
        """单只股票财务摘要（原始宽表：选项/指标/各报告期）。"""

    @abstractmethod
    def get_stock_info(self, code: str) -> dict:
        """个股概况：上市时间、总股本、流通股本、行业等。"""
