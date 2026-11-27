import sys
import types
import unittest
import pandas as pd

try:
    import pymysql  # noqa: F401
except ImportError:
    # 本地未安装 MySQL 驱动时，为纯计算/表达式单元测试提供轻量 db 占位
    mock_db = types.ModuleType("stock_system.db")
    mock_pkg = types.ModuleType("stock_system")
    mock_pkg.__path__ = ["stock_system"]
    mock_pkg.db = mock_db
    sys.modules.setdefault("stock_system", mock_pkg)
    sys.modules.setdefault("stock_system.db", mock_db)

from stock_system.indicators.calc import calc_technical
from stock_system.screener.engine import eval_tree
from stock_system.screener.templates import TEMPLATES


class TestScreenerAndIndicators(unittest.TestCase):
    def test_t07_t08_t09_templates_eval(self):
        df = pd.DataFrame(
            [
                {
                    "stock_code": "600000",
                    "pe_ttm": 8.0,
                    "roe": 14.0,
                    "debt_ratio": 55.0,
                    "turnover_rate": 1.5,
                    "revenue_yoy": 5.0,
                    "net_profit_yoy": 4.0,
                    "drawdown_60d": 5.0,
                    "vol_ratio": 1.0,
                    "pct_chg_adj": 0.5,
                    "rs_20d": 1.0,
                    "ma5": 10.0,
                    "ma10": 10.2,
                    "ma20": 10.1,
                },
                {
                    "stock_code": "300750",
                    "pe_ttm": 25.0,
                    "roe": 18.0,
                    "debt_ratio": 45.0,
                    "turnover_rate": 3.0,
                    "revenue_yoy": 20.0,
                    "net_profit_yoy": 25.0,
                    "drawdown_60d": 18.0,
                    "vol_ratio": 1.8,
                    "pct_chg_adj": 2.5,
                    "rs_20d": 8.0,
                    "ma5": 20.0,
                    "ma10": 19.0,
                    "ma20": 18.0,
                },
            ]
        )
        m7 = eval_tree(TEMPLATES["T07"]["condition"], df)
        self.assertEqual(m7.tolist(), [True, False])

        m8 = eval_tree(TEMPLATES["T08"]["condition"], df)
        self.assertEqual(m8.tolist(), [False, True])

        m9 = eval_tree(TEMPLATES["T09"]["condition"], df)
        self.assertEqual(m9.tolist(), [False, True])

    def test_calc_technical_handles_non_positive_prices(self):
        df = pd.DataFrame(
            {
                "trade_date": pd.date_range("2026-01-01", periods=30, freq="B"),
                "open_adj": [-1.0] * 5 + [10.0 + i * 0.1 for i in range(25)],
                "high_adj": [-0.5] * 5 + [10.5 + i * 0.1 for i in range(25)],
                "low_adj": [-1.5] * 5 + [9.5 + i * 0.1 for i in range(25)],
                "close_adj": [0.0] * 5 + [10.2 + i * 0.1 for i in range(25)],
                "volume": [1000] * 30,
            }
        )
        ind = calc_technical(df)
        self.assertEqual(len(ind), 30)
        self.assertTrue(pd.isna(ind.iloc[0]["ma5"]))
        self.assertTrue(pd.notna(ind.iloc[-1]["ma20"]))
        for col in (
            "vol_ratio_20",
            "boll_width_20",
            "consol_range_20",
            "high_20d_prev",
            "pocket_pivot",
            "upper_shadow_ratio",
            "bias_ma20",
        ):
            self.assertIn(col, ind.columns)
            self.assertTrue(pd.notna(ind.iloc[-1][col]), f"{col} should be populated")

    def test_t10_t11_launch_templates_eval(self):
        df = pd.DataFrame(
            [
                {
                    "stock_code": "000001",
                    "total_mv_yi": 180.0,
                    "pe_ttm": 22.0,
                    "close_price": 12.0,
                    "ma20": 11.4,
                    "ma60": 11.0,
                    "amount_yi": 5.0,
                    "consol_range_20": 15.0,
                    "pocket_pivot": 1,
                    "vol_ratio_20": 1.8,
                    "upper_shadow_ratio": 0.15,
                    "bias_ma20": 5.2,
                    "pct_chg_adj": 4.2,
                    "pct_chg_20d": 14.5,
                    "high_20d_prev": 11.8,
                    "rs_20d": 12.0,
                    "vol_ratio": 1.6,
                },
                {
                    "stock_code": "000002",
                    "total_mv_yi": 20.0,
                    "pe_ttm": -5.0,
                    "close_price": 9.0,
                    "ma20": 10.0,
                    "ma60": 10.5,
                    "amount_yi": 1.0,
                    "consol_range_20": 35.0,
                    "pocket_pivot": 0,
                    "vol_ratio_20": 0.9,
                    "upper_shadow_ratio": 0.6,
                    "bias_ma20": -10.0,
                    "pct_chg_adj": -1.5,
                    "pct_chg_20d": -4.0,
                    "high_20d_prev": 11.0,
                    "rs_20d": -5.0,
                    "vol_ratio": 0.8,
                },
            ]
        )
        m10 = eval_tree(TEMPLATES["T10"]["condition"], df)
        self.assertEqual(m10.tolist(), [True, False])
        m11 = eval_tree(TEMPLATES["T11"]["condition"], df)
        self.assertEqual(m11.tolist(), [True, False])


if __name__ == "__main__":
    unittest.main()
