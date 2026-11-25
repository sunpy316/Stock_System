# -*- coding: utf-8 -*-
"""技术指标计算：全部在复权序列上进行（对应需求文档 FR-IND-04）。

输入为单只股票按交易日升序排列的 DataFrame，至少包含：
    trade_date, open_adj, high_adj, low_adj, close_adj, volume
输出为同索引的指标列，未满窗口的位置为 NaN（不填充，避免"假信号"）。
"""
import numpy as np
import pandas as pd


def _wilder(s: pd.Series, n: int) -> pd.Series:
    """Wilder 平滑：alpha = 1/n，等价于 ewm(alpha=1/n)。"""
    return s.ewm(alpha=1.0 / n, adjust=False).mean()


def calc_technical(df: pd.DataFrame) -> pd.DataFrame:
    """计算全部技术指标。df 需按 trade_date 升序。

    数据源缺陷规避：东财前复权是"减法式"折算，当累计分红送转超过历史价格时，
    早年（以及 2010—2014 的部分高分红股）会返回 **0 或负的前复权价**。
    这类价格参与除法会产生 ±inf（PyMySQL 会拒绝写入），算出的指标本身也没有
    意义。因此只对「开高低收全部为正」的连续区间计算，其余位置输出 NaN。
    """
    close = df["close_adj"].astype(float)
    high = df["high_adj"].astype(float)
    low = df["low_adj"].astype(float)
    vol = df["volume"].astype(float)

    valid = (close > 0) & (high > 0) & (low > 0)
    if not valid.all():
        sub = df.iloc[valid.to_numpy().nonzero()[0]]
        if sub.empty:
            return calc_technical(df.iloc[0:0]).reindex(df.index)
        return calc_technical(sub).reindex(df.index)

    out = pd.DataFrame(index=df.index)

    # ---------- 均线 ----------
    for n in (5, 10, 20, 60, 120, 250):
        out[f"ma{n}"] = close.rolling(n, min_periods=n).mean()

    # ---------- MACD ----------
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    out["macd_dif"] = dif
    out["macd_dea"] = dea
    out["macd_bar"] = 2 * (dif - dea)

    # ---------- KDJ(9,3,3) ----------
    low9 = low.rolling(9, min_periods=9).min()
    high9 = high.rolling(9, min_periods=9).max()
    rng = (high9 - low9).replace(0, np.nan)
    rsv = (close - low9) / rng * 100
    k = rsv.ewm(alpha=1 / 3, adjust=False).mean()
    d = k.ewm(alpha=1 / 3, adjust=False).mean()
    out["kdj_k"] = k
    out["kdj_d"] = d
    out["kdj_j"] = 3 * k - 2 * d

    # ---------- RSI（Wilder） ----------
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    for n in (6, 12, 24):
        avg_gain = _wilder(gain, n)
        avg_loss = _wilder(loss, n)
        rs = avg_gain / avg_loss.replace(0, np.nan)
        out[f"rsi{n}"] = 100 - 100 / (1 + rs)

    # ---------- BOLL(20,2) ----------
    mid = close.rolling(20, min_periods=20).mean()
    std = close.rolling(20, min_periods=20).std(ddof=0)
    out["boll_mid"] = mid
    out["boll_up"] = mid + 2 * std
    out["boll_low"] = mid - 2 * std

    # ---------- ATR(14) ----------
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    out["atr14"] = _wilder(tr, 14)

    # ---------- 量能 ----------
    out["vol_ma5"] = vol.shift(1).rolling(5, min_periods=5).mean()
    out["vol_ratio"] = vol / out["vol_ma5"].replace(0, np.nan)

    # ---------- 多周期涨幅 ----------
    for n in (5, 20, 60):
        out[f"pct_chg_{n}d"] = (close / close.shift(n) - 1) * 100

    # ---------- 区间高低 ----------
    out["high_60d"] = high.rolling(60, min_periods=60).max()
    out["low_60d"] = low.rolling(60, min_periods=60).min()

    # ---------- 相对强度 ----------
    # 需要基准指数行情，指数采集完成前留空，避免用个股自身涨幅顶替
    out["rs_20d"] = np.nan

    # 兜底：任何列出现 ±inf（除数为 0 等极端情况）一律转 NaN，
    # 否则写入 MySQL 时 PyMySQL 会抛 "inf can not be used with MySQL"。
    return out.replace([np.inf, -np.inf], np.nan)


def is_bull_ma(df: pd.DataFrame) -> pd.Series:
    """均线多头排列：MA5 > MA10 > MA20 > MA60 且四条均向上。"""
    ma5, ma10 = df["ma5"], df["ma10"]
    ma20, ma60 = df["ma20"], df["ma60"]
    ordered = (ma5 > ma10) & (ma10 > ma20) & (ma20 > ma60)
    rising = (ma5 > ma5.shift(1)) & (ma20 > ma20.shift(1)) & (ma60 > ma60.shift(1))
    return ordered & rising


def cross_above(fast: pd.Series, slow: pd.Series) -> pd.Series:
    """上穿：今日 fast > slow 且昨日 fast <= slow。"""
    return (fast > slow) & (fast.shift(1) <= slow.shift(1))
