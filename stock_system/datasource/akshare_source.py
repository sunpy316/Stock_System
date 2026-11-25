# -*- coding: utf-8 -*-
"""akshare 数据源实现：限频、重试退避、列名标准化。"""
import random
import threading
import time
from datetime import datetime, timedelta

import akshare as ak
import pandas as pd

from .. import config
from .base import BaseDataSource

# 东方财富 push2 限流异常。akshare 1.20 起才有这个类，旧版本降级为 None。
# 语义：它表示"IP 被临时限流"，重试只会延长封禁，必须立即上抛。
try:  # pragma: no cover - 取决于安装的 akshare 版本
    from akshare.utils.http import Push2RateLimitError
except Exception:  # noqa: BLE001
    Push2RateLimitError = None


def _call_with_timeout(call, fn, timeout=20):
    """给单次外部调用加超时保护。超时/异常返回 None，不阻塞主流程。

    北交所站点（www.bse.cn）偶发长时间不响应，不能让它拖住整个列表同步。
    """
    from concurrent.futures import ThreadPoolExecutor

    ex = ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(call, fn)
    try:
        return fut.result(timeout=timeout)
    except Exception:  # noqa: BLE001
        return None
    finally:
        ex.shutdown(wait=False)

QUOTE_COLUMNS = {
    "日期": "trade_date",
    "股票代码": "stock_code",
    "开盘": "open_price",
    "收盘": "close_price",
    "最高": "high_price",
    "最低": "low_price",
    "成交量": "volume",
    "成交额": "amount",
    "振幅": "amplitude",
    "涨跌幅": "pct_chg",
    "涨跌额": "chg",
    "换手率": "turnover_rate",
}

def _tx_symbol(code: str) -> str:
    """6 位代码转腾讯格式：sh600000 / sz000001 / bj920021。

    注意北交所新代码段 920xxx 以 9 开头，不能按"9 开头=沪市"处理（900xxx 才是沪 B）。
    """
    c = str(code).zfill(6)
    if c[:2] == "92":          # 北交所新代码段
        return "bj" + c
    if c[0] in "48":           # 北交所 43/83/87/88 等旧代码段
        return "bj" + c
    if c[0] in "69":           # 沪市 A/B
        return "sh" + c
    return "sz" + c


QUOTE_FIELDS = [
    "trade_date",
    "stock_code",
    "open_price",
    "high_price",
    "low_price",
    "close_price",
    "volume",
    "amount",
    "amplitude",
    "pct_chg",
    "chg",
    "turnover_rate",
]


class AkshareSource(BaseDataSource):
    def __init__(self, interval=None, max_retry=None, provider=None):
        self.interval = config.REQUEST_INTERVAL if interval is None else interval
        self.max_retry = config.MAX_RETRY if max_retry is None else max_retry
        # 行情源：em=东方财富 push2（默认），tx=腾讯财经。
        # 东财按 IP 限流且封禁时间不可控，tx 作为可用替代（前复权价实测与东财逐位一致）。
        self.provider = (provider or config.QUOTE_PROVIDER or "em").lower()
        self._last_call = 0.0
        self._lock = threading.Lock()

    # ---------- 内部：限频与重试 ----------
    def _throttle(self):
        with self._lock:
            gap = time.time() - self._last_call
            wait = self.interval - gap
            if wait > 0:
                # 加 ±30% 抖动：严格等间隔的请求节奏特征太像脚本，容易被判定为爬虫
                wait += random.uniform(-self.interval * 0.3, self.interval * 0.3)
                time.sleep(max(0.0, wait))
            self._last_call = time.time()

    def _call(self, fn, *args, **kwargs):
        last_err = None
        for attempt in range(self.max_retry):
            self._throttle()
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                # 东方财富 push2 限流：akshare 明确要求不重试。
                # 这里重试等于在封禁窗口里持续敲门，会把 5 分钟拖成 40 分钟，
                # 所以必须原样上抛，交由调用方熔断整批。
                if Push2RateLimitError is not None and isinstance(exc, Push2RateLimitError):
                    raise
                last_err = exc
                if attempt < self.max_retry - 1:
                    time.sleep(config.RETRY_BACKOFF * (2**attempt))
        raise RuntimeError(f"{getattr(fn, '__name__', fn)} 调用失败：{last_err}")

    # ---------- 接口实现 ----------
    def get_stock_list(self) -> pd.DataFrame:
        """分市场拉取股票列表。

        不走 stock_info_a_code_name（内部串行访问北交所站点，易超时），
        改为上交所 + 深交所 + 北交所（超时容忍）三路合并。
        沪深两个接口自带上市日期、股本与行业，省去逐只补采。
        """
        frames = []

        # 上交所：主板与科创板需分两次请求（接口 symbol 参数区分）
        for sym in ("主板A股", "科创板"):
            try:
                sh = self._call(ak.stock_info_sh_name_code, symbol=sym)
            except Exception:  # noqa: BLE001
                continue
            if sh is None or sh.empty:
                continue
            frames.append(
                pd.DataFrame(
                    {
                        "stock_code": sh["证券代码"].astype(str).str.zfill(6),
                        "stock_name": sh["证券简称"].astype(str).str.strip(),
                        "list_date": pd.to_datetime(sh["上市日期"], errors="coerce").dt.date,
                        "total_share": None,
                        "float_share": None,
                        "industry_name": None,
                    }
                )
            )

        sz = self._call(ak.stock_info_sz_name_code)
        if sz is not None and not sz.empty:
            frames.append(
                pd.DataFrame(
                    {
                        "stock_code": sz["A股代码"].astype(str).str.zfill(6),
                        "stock_name": sz["A股简称"].astype(str).str.strip(),
                        "list_date": pd.to_datetime(sz["A股上市日期"], errors="coerce").dt.date,
                        # 深交所返回的股本是带千分位逗号的字符串，需清洗后再转数值
                        "total_share": pd.to_numeric(
                            sz["A股总股本"].astype(str).str.replace(",", "", regex=False),
                            errors="coerce",
                        ),
                        "float_share": pd.to_numeric(
                            sz["A股流通股本"].astype(str).str.replace(",", "", regex=False),
                            errors="coerce",
                        ),
                        "industry_name": sz.get("所属行业"),
                    }
                )
            )

        bj = _call_with_timeout(self._call, ak.stock_info_bj_name_code, timeout=25)
        if bj is not None and not bj.empty:
            code_col = "证券代码" if "证券代码" in bj.columns else bj.columns[0]
            name_col = "证券简称" if "证券简称" in bj.columns else bj.columns[1]
            frames.append(
                pd.DataFrame(
                    {
                        "stock_code": bj[code_col].astype(str).str.zfill(6),
                        "stock_name": bj[name_col].astype(str).str.strip(),
                        "list_date": None,
                        "total_share": None,
                        "float_share": None,
                        "industry_name": None,
                    }
                )
            )

        if not frames:
            raise RuntimeError("三个市场的股票列表均获取失败")

        df = pd.concat(frames, ignore_index=True)
        df = df.drop_duplicates(subset=["stock_code"], keep="first")
        return df.sort_values("stock_code").reset_index(drop=True)

    def get_trade_calendar(self) -> pd.DataFrame:
        df = self._call(ak.tool_trade_date_hist_sina)
        df = df.rename(columns={"trade_date": "trade_date"})
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        return df[["trade_date"]].drop_duplicates().sort_values("trade_date")

    def get_daily_quote(self, code: str, start: str, end: str, adjust: str = "") -> pd.DataFrame:
        if self.provider == "tx":
            return self._get_daily_quote_tx(code, start, end, adjust)
        df = self._call(
            ak.stock_zh_a_hist,
            symbol=code,
            period="daily",
            start_date=start,
            end_date=end,
            adjust=adjust,
        )
        if df is None or df.empty:
            return pd.DataFrame(columns=QUOTE_FIELDS)

        df = df.rename(columns=QUOTE_COLUMNS)
        for col in QUOTE_FIELDS:
            if col not in df.columns:
                df[col] = None

        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        df["stock_code"] = df["stock_code"].astype(str).str.zfill(6)

        # 前收盘价：优先用 收盘 - 涨跌额；缺涨跌额时按涨跌幅反推
        close = pd.to_numeric(df["close_price"], errors="coerce")
        chg = pd.to_numeric(df["chg"], errors="coerce")
        pct = pd.to_numeric(df["pct_chg"], errors="coerce")
        pre_close = close - chg
        fallback = close / (1 + pct / 100.0)
        df["pre_close"] = pre_close.where(pre_close.notna(), fallback)

        for col in ["open_price", "high_price", "low_price", "close_price", "pre_close",
                    "amount", "amplitude", "pct_chg", "chg", "turnover_rate"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce").astype("Int64")

        return df[QUOTE_FIELDS + ["pre_close"]].sort_values("trade_date").reset_index(drop=True)


    def _get_daily_quote_tx(self, code: str, start: str, end: str, adjust: str = "") -> pd.DataFrame:
        """腾讯财经日线。腾讯不返回前收/涨跌幅等派生字段，这里自行补齐。

        - 多取 12 天，用 shift(1) 得到 pre_close（区间首日也能算）
        - pct_chg / chg / amplitude 由价格推导，公式与东财口径一致（已验证逐位相同）
        - turnover 为小数（0.0054），乘 100 得百分比
        """
        d0 = datetime.strptime(start, "%Y%m%d") - timedelta(days=12)
        df = self._call(
            ak.stock_zh_a_hist_tx,
            symbol=_tx_symbol(code),
            start_date=d0.strftime("%Y%m%d"),
            end_date=end,
            adjust=("qfq" if adjust == "qfq" else ""),
        )
        if df is None or df.empty:
            return pd.DataFrame(columns=QUOTE_FIELDS)

        df = df.copy()
        df["trade_date"] = pd.to_datetime(df["date"]).dt.date
        df = df.sort_values("trade_date").reset_index(drop=True)

        close = pd.to_numeric(df["close"], errors="coerce")
        raw_vol = pd.to_numeric(df["volume"], errors="coerce")
        raw_amt = pd.to_numeric(df["amount"], errors="coerce")
        # 腾讯接口 volume 单位通常为「股」，而东财为「手」（1手=100股）；统一折算为手
        is_shares = (raw_vol > 0) & (raw_amt > 0) & ((raw_amt / raw_vol) < (close * 10))
        norm_vol = raw_vol.where(~is_shares, (raw_vol / 100).round()).astype("Int64")
        out = pd.DataFrame({
            "trade_date": df["trade_date"],
            "stock_code": str(code).zfill(6),
            "open_price": pd.to_numeric(df["open"], errors="coerce"),
            "high_price": pd.to_numeric(df["high"], errors="coerce"),
            "low_price": pd.to_numeric(df["low"], errors="coerce"),
            "close_price": close,
            "pre_close": close.shift(1),
            "volume": norm_vol,
            "amount": raw_amt,
            "turnover_rate": (pd.to_numeric(df["turnover"], errors="coerce") * 100).round(3),
        })
        out["pct_chg"] = ((out["close_price"] / out["pre_close"] - 1) * 100).round(3)
        out["chg"] = (out["close_price"] - out["pre_close"]).round(3)
        out["amplitude"] = (
            (out["high_price"] - out["low_price"]) / out["pre_close"] * 100
        ).round(3)

        s_date = datetime.strptime(start, "%Y%m%d").date()
        e_date = datetime.strptime(end, "%Y%m%d").date()
        out = out[
            (out["trade_date"] >= s_date)
            & (out["trade_date"] <= e_date)
            & out["pre_close"].notna()
        ]
        return out[QUOTE_FIELDS + ["pre_close"]].reset_index(drop=True)

    def get_financial(self, code: str) -> pd.DataFrame:
        df = self._call(ak.stock_financial_abstract, symbol=code)
        if df is None or df.empty:
            return pd.DataFrame()
        return df

    def get_stock_info(self, code: str) -> dict:
        df = self._call(ak.stock_individual_info_em, symbol=code)
        if df is None or df.empty:
            return {}
        return dict(zip(df["item"].astype(str), df["value"]))

    def get_spot_by_market(self, market: str) -> pd.DataFrame:
        """按市场取实时快照（含总市值/流通市值），用于反推股本。

        沪深交易所的列表接口只有深市带股本，沪市与北交所只能用市值反推：
            总股本 = 总市值 / 最新价
        """
        fn = {
            "sh": getattr(ak, "stock_sh_a_spot_em", None),
            "sz": getattr(ak, "stock_sz_a_spot_em", None),
            "bj": getattr(ak, "stock_bj_a_spot_em", None),
        }.get(market)
        if fn is None:
            raise RuntimeError(f"akshare 不提供 {market} 市场快照接口")
        df = self._call(fn)
        if df is None or df.empty:
            return pd.DataFrame(columns=["stock_code", "price", "total_mv", "float_mv"])
        out = pd.DataFrame(
            {
                "stock_code": df["代码"].astype(str).str.zfill(6),
                "price": pd.to_numeric(df["最新价"], errors="coerce"),
                "total_mv": pd.to_numeric(df["总市值"], errors="coerce"),
                "float_mv": pd.to_numeric(df["流通市值"], errors="coerce"),
            }
        )
        return out.dropna(subset=["price"]).query("price > 0").reset_index(drop=True)
