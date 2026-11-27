# -*- coding: utf-8 -*-
"""启动信号池（个股 T10/T11 + ETF Tier1/Tier2）持续每日跟踪、买入级别评定与自动移出引擎。"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from .. import db
from . import cross_section
from .engine import apply_filters, eval_tree
from .templates import TEMPLATES


def ensure_pool_table() -> None:
    """幂等创建启动信号池跟踪表。"""
    from sqlalchemy import text

    with db.engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS fact_launch_pool (
                    pool_id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    asset_type VARCHAR(8) NOT NULL COMMENT 'STOCK 或 ETF',
                    code VARCHAR(16) NOT NULL COMMENT '股票或ETF代码',
                    name VARCHAR(64) NOT NULL COMMENT '名称',
                    status VARCHAR(12) NOT NULL DEFAULT 'ACTIVE' COMMENT 'ACTIVE=池内跟踪 EXITED=已移出',
                    entry_date DATE NOT NULL COMMENT '入池日期',
                    entry_price DECIMAL(12,4) NOT NULL COMMENT '入池当日收盘价',
                    entry_reason VARCHAR(255) NOT NULL DEFAULT '' COMMENT '入池理由',
                    source_tag VARCHAR(32) NOT NULL DEFAULT '' COMMENT '信号来源(T10/T11/T10+T11/Tier1/Tier2)',
                    breakout_ref DECIMAL(12,4) DEFAULT NULL COMMENT '突破参考位(前20日高点)',
                    stop_loss_ref DECIMAL(12,4) DEFAULT NULL COMMENT '动态防守止损位',
                    last_track_date DATE NOT NULL COMMENT '最近跟踪日期',
                    last_price DECIMAL(12,4) NOT NULL COMMENT '最新收盘价',
                    last_pct_chg DECIMAL(8,3) DEFAULT NULL COMMENT '最新日涨跌幅(%)',
                    cum_pct DECIMAL(8,3) NOT NULL DEFAULT 0 COMMENT '入池以来累计涨跌幅(%)',
                    max_price DECIMAL(12,4) DEFAULT NULL COMMENT '入池以来最高价',
                    drawdown_from_peak DECIMAL(8,3) DEFAULT 0 COMMENT '自入池高点回撤(%)',
                    days_tracked INT NOT NULL DEFAULT 1 COMMENT '持续跟踪交易日数',
                    ma5 DECIMAL(12,4) DEFAULT NULL,
                    ma20 DECIMAL(12,4) DEFAULT NULL,
                    ma60 DECIMAL(12,4) DEFAULT NULL,
                    bias_ma20 DECIMAL(8,3) DEFAULT NULL COMMENT '20日乖离率(%)',
                    vol_ratio DECIMAL(8,3) DEFAULT NULL COMMENT '5日量比',
                    vol_ratio_20 DECIMAL(8,3) DEFAULT NULL COMMENT '20日量比',
                    amount_yi DECIMAL(10,3) DEFAULT NULL COMMENT '成交额(亿元)',
                    buy_level VARCHAR(24) NOT NULL DEFAULT 'C-持有观察' COMMENT '买入级别',
                    buy_score INT NOT NULL DEFAULT 60 COMMENT '综合评分(0-100)',
                    track_note VARCHAR(255) NOT NULL DEFAULT '' COMMENT '当日跟踪点评',
                    exit_date DATE DEFAULT NULL COMMENT '移出日期',
                    exit_reason VARCHAR(255) DEFAULT NULL COMMENT '移出理由',
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    UNIQUE KEY uk_pool_asset_entry (asset_type, code, entry_date),
                    KEY idx_pool_type_status (asset_type, status, buy_score),
                    KEY idx_pool_exit_date (asset_type, status, exit_date)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='个股与ETF启动信号池持续跟踪表'
                """
            )
        )


def _sf(v: Any, default: Optional[float] = None) -> Optional[float]:
    if v is None:
        return default
    try:
        if pd.isna(v):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _r(v: Optional[float], ndigits: int = 2) -> Optional[float]:
    return None if v is None else round(float(v), ndigits)


def check_exit_condition(
    asset_type: str,
    close_price: float,
    ma5: Optional[float],
    ma20: Optional[float],
    stop_loss_ref: Optional[float],
    cum_pct: float,
    drawdown_from_peak: float,
    days_tracked: int,
) -> Optional[str]:
    """判定池内标的是否触发移出条件；返回移出理由或 None（继续保留）。"""
    if ma20 is not None and ma20 > 0 and close_price < ma20 * 0.99:
        return f"有效跌破MA20趋势线（现价{close_price:.2f} < MA20 {ma20:.2f}）"

    if stop_loss_ref is not None and stop_loss_ref > 0 and close_price < stop_loss_ref * 0.99:
        return f"跌破动态防守位（现价{close_price:.2f} < 防守位{stop_loss_ref:.2f}）"

    dd_limit = 8.0 if asset_type == "ETF" else 12.0
    if drawdown_from_peak >= dd_limit:
        return f"自入池高点回撤{drawdown_from_peak:.1f}%（超{dd_limit:.0f}%阈值），动能衰竭"

    if days_tracked >= 15 and cum_pct < 0:
        return f"已跟踪{days_tracked}个交易日且累计收益{cum_pct:+.1f}%，趋势未走出"

    if ma5 is not None and ma5 > 0 and close_price < ma5 and cum_pct <= -5.0:
        return f"跌破MA5短线均线且累计回撤{cum_pct:.1f}%"

    return None


def rate_buy_level(
    asset_type: str,
    source_tag: str,
    close_price: float,
    pct_chg: float,
    ma5: Optional[float],
    ma20: Optional[float],
    ma60: Optional[float],
    bias_ma20: Optional[float],
    vol_ratio: Optional[float],
    vol_ratio_20: Optional[float],
    cum_pct: float,
    drawdown_from_peak: float,
    days_tracked: int,
    is_new_entry: bool,
    retriggered: bool,
) -> Tuple[str, int, str]:
    """根据当前技术形态与跟踪表现评定买入级别、评分与当日点评。"""
    bias = bias_ma20 if bias_ma20 is not None else 0.0
    vr5 = vol_ratio if vol_ratio is not None else 1.0
    vr20 = vol_ratio_20 if vol_ratio_20 is not None else 1.0
    abv20 = ma20 is not None and close_price > ma20
    abv60 = ma60 is not None and close_price > ma60
    ma_bull = (ma5 is not None and ma20 is not None and ma5 >= ma20)

    high_bias_thr = 8.0 if asset_type == "ETF" else 12.0
    if bias > high_bias_thr:
        return (
            "D-高乖离等待",
            48,
            f"MA20乖离{bias:.1f}%偏高（>{high_bias_thr:.0f}%），严禁追高，宜等回踩MA20附近",
        )

    score = 60
    if abv20 and abv60:
        score += 12
    elif abv20:
        score += 4
    if ma_bull:
        score += 5
    if source_tag in ("T10+T11", "Tier1"):
        score += 14
    elif source_tag in ("T10", "T11"):
        score += 8
    elif source_tag == "Tier2":
        score += 3

    if is_new_entry:
        score += 8
    elif retriggered:
        score += 10

    if vr5 >= 1.5 and pct_chg > 0:
        score += 6
    elif vr5 >= 1.2 and pct_chg > 0:
        score += 3

    if drawdown_from_peak <= 2.5:
        score += 4
    elif drawdown_from_peak >= 6.0:
        score -= 6

    score = max(35, min(98, int(round(score))))

    # 跟踪期内健康缩量回踩 MA20 附近 → B-回踩低吸窗口
    pullback_bias_cap = 3.5 if asset_type == "ETF" else 4.5
    if (
        not is_new_entry
        and not retriggered
        and abv20
        and 0.0 <= bias <= pullback_bias_cap
        and (vr5 <= 1.05 or (-3.0 <= pct_chg <= 1.2))
    ):
        note = (
            f"跟踪第{days_tracked}日缩量回踩MA20附近（乖离{bias:.1f}%，量比{vr5:.2f}x），"
            f"累计{cum_pct:+.1f}%，低吸观察窗口"
        )
        return "B-回踩低吸", max(68, min(78, score)), note

    s_bias_cap = 6.0 if asset_type == "ETF" else 9.5
    if score >= 85 and abv20 and abv60 and bias <= s_bias_cap and drawdown_from_peak <= 3.5:
        if is_new_entry:
            note = f"{source_tag}当日放量启动，站稳MA20/60，量比{vr5:.2f}x，乖离{bias:.1f}%健康"
        elif retriggered:
            note = f"跟踪第{days_tracked}日再次触发启动信号，趋势加速，累计{cum_pct:+.1f}%"
        else:
            note = f"跟踪第{days_tracked}日主升延续，均线多头、乖离{bias:.1f}%健康，累计{cum_pct:+.1f}%"
        return "S-强烈关注", score, note

    if score >= 72 and abv20:
        if is_new_entry:
            note = f"{source_tag}新入池启动，量比{vr5:.2f}x/20日量比{vr20:.2f}x，乖离{bias:.1f}%"
        elif retriggered:
            note = f"跟踪第{days_tracked}日二次放量确认，累计{cum_pct:+.1f}%，可重点跟踪"
        else:
            note = f"跟踪第{days_tracked}日趋势良好，站稳MA20（乖离{bias:.1f}%），累计{cum_pct:+.1f}%"
        return "A-重点关注", score, note

    if abv20 and 0.0 <= bias <= pullback_bias_cap and pct_chg <= 1.5:
        note = f"贴近MA20整理（乖离{bias:.1f}%），未破防守位，适合回踩低吸观察"
        return "B-回踩低吸", max(64, score), note

    note = (
        f"跟踪第{days_tracked}日仍在MA20上方（乖离{bias:.1f}%，累计{cum_pct:+.1f}%），"
        + ("尚未站上MA60，右侧观察" if not abv60 else "震荡整理中，继续持有观察")
    )
    return "C-持有观察", min(score, 66), note


def _build_stock_entry_reason(row: pd.Series, hits: List[str]) -> str:
    tag = "+".join(hits)
    parts = []
    if "T10" in hits:
        parts.append("缩量蓄势口袋支点")
    if "T11" in hits:
        parts.append("放量突破前20日平台")
    metrics = []
    amt = _sf(row.get("amount_yi"))
    vr5 = _sf(row.get("vol_ratio"))
    vr20 = _sf(row.get("vol_ratio_20"))
    cr20 = _sf(row.get("consol_range_20"))
    pct = _sf(row.get("pct_chg_adj"))
    if pct is not None:
        metrics.append(f"涨{pct:+.1f}%")
    if vr5 is not None:
        metrics.append(f"5日量比{vr5:.2f}x")
    if vr20 is not None:
        metrics.append(f"20日量比{vr20:.2f}x")
    if cr20 is not None:
        metrics.append(f"20日振幅{cr20:.1f}%")
    if amt is not None:
        metrics.append(f"成交{amt:.1f}亿")
    return f"{tag}({'、'.join(parts)})：{'，'.join(metrics)}"[:250]


def _build_etf_entry_reason(sig: Dict[str, Any]) -> str:
    tier_txt = "Tier1主升突破" if int(sig.get("tier", 2)) == 1 else "Tier2右侧转折"
    sigs = sig.get("signals") or "启动信号"
    metrics = [
        f"涨{float(sig.get('pct_chg') or 0):+.2f}%",
        f"5日量比{float(sig.get('vol_ratio') or 0):.2f}x",
        f"成交{float(sig.get('amt_yi') or 0):.2f}亿",
    ]
    if sig.get("consol_range_20") is not None:
        metrics.append(f"20日振幅{float(sig['consol_range_20']):.1f}%")
    return f"{tier_txt}({sigs})：{'，'.join(metrics)}"[:250]


def _upsert_pool_updates(
    asset_type: str,
    trade_date: str,
    new_signals: Dict[str, Dict[str, Any]],
    latest_snaps: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """统一执行：老成员每日跟踪+评级/移出，新信号入池。"""
    from sqlalchemy import text

    ensure_pool_table()
    active_df = db.query_df(
        "SELECT * FROM fact_launch_pool WHERE asset_type = :t AND status = 'ACTIVE'",
        {"t": asset_type},
    )

    active_codes = set()
    updated_cnt = 0
    exited_cnt = 0
    added_cnt = 0

    with db.engine.begin() as conn:
        # 1) 更新已有 ACTIVE 标的
        for _, m in active_df.iterrows():
            code = str(m["code"])
            active_codes.add(code)
            snap = latest_snaps.get(code)
            if not snap:
                continue

            close_p = float(snap["close_price"])
            pct_chg = float(snap.get("pct_chg") or 0.0)
            ma5 = _sf(snap.get("ma5"))
            ma20 = _sf(snap.get("ma20"))
            ma60 = _sf(snap.get("ma60"))
            bias = _sf(snap.get("bias_ma20"))
            if bias is None and ma20 and ma20 > 0:
                bias = (close_p / ma20 - 1.0) * 100.0
            vr5 = _sf(snap.get("vol_ratio"))
            vr20 = _sf(snap.get("vol_ratio_20"))
            amt_yi = _sf(snap.get("amount_yi"))

            entry_p = float(m["entry_price"])
            cum_pct = ((close_p / entry_p) - 1.0) * 100.0 if entry_p > 0 else 0.0
            prev_max = _sf(m.get("max_price"), entry_p) or entry_p
            max_p = max(prev_max, close_p)
            dd_peak = ((max_p - close_p) / max_p * 100.0) if max_p > 0 else 0.0

            last_dt = str(m["last_track_date"])[:10]
            days = int(m["days_tracked"] or 1)
            if trade_date > last_dt:
                days += 1

            old_stop = _sf(m.get("stop_loss_ref"))
            # 移出判定使用原防守位与当前 MA20
            exit_reason = check_exit_condition(
                asset_type=asset_type,
                close_price=close_p,
                ma5=ma5,
                ma20=ma20,
                stop_loss_ref=old_stop,
                cum_pct=cum_pct,
                drawdown_from_peak=dd_peak,
                days_tracked=days,
            )

            # 动态上移防守位（只升不降，保护利润）
            new_stop = old_stop
            if ma20 is not None and ma20 > 0:
                new_stop = max(old_stop, ma20) if old_stop else ma20

            retriggered = code in new_signals
            source_tag = str(m["source_tag"] or "")
            if retriggered and new_signals[code].get("source_tag"):
                new_src = new_signals[code]["source_tag"]
                if new_src in ("T10+T11", "Tier1"):
                    source_tag = new_src

            if exit_reason:
                conn.execute(
                    text(
                        """
                        UPDATE fact_launch_pool
                        SET status='EXITED', last_track_date=:td, last_price=:lp, last_pct_chg=:lpc,
                            cum_pct=:cp, max_price=:mp, drawdown_from_peak=:dd, days_tracked=:dt,
                            ma5=:ma5, ma20=:ma20, ma60=:ma60, bias_ma20=:bias,
                            vol_ratio=:vr5, vol_ratio_20=:vr20, amount_yi=:amt,
                            exit_date=:td, exit_reason=:er, track_note=:er
                        WHERE pool_id = :pid
                        """
                    ),
                    {
                        "td": trade_date, "lp": _r(close_p, 4), "lpc": _r(pct_chg, 3),
                        "cp": _r(cum_pct, 3), "mp": _r(max_p, 4), "dd": _r(dd_peak, 3),
                        "dt": days, "ma5": _r(ma5, 4), "ma20": _r(ma20, 4), "ma60": _r(ma60, 4),
                        "bias": _r(bias, 3), "vr5": _r(vr5, 3), "vr20": _r(vr20, 3),
                        "amt": _r(amt_yi, 3), "er": exit_reason[:250], "pid": int(m["pool_id"]),
                    },
                )
                exited_cnt += 1
            else:
                buy_level, buy_score, track_note = rate_buy_level(
                    asset_type=asset_type,
                    source_tag=source_tag,
                    close_price=close_p,
                    pct_chg=pct_chg,
                    ma5=ma5,
                    ma20=ma20,
                    ma60=ma60,
                    bias_ma20=bias,
                    vol_ratio=vr5,
                    vol_ratio_20=vr20,
                    cum_pct=cum_pct,
                    drawdown_from_peak=dd_peak,
                    days_tracked=days,
                    is_new_entry=(str(m["entry_date"])[:10] == trade_date),
                    retriggered=retriggered,
                )
                conn.execute(
                    text(
                        """
                        UPDATE fact_launch_pool
                        SET source_tag=:src, stop_loss_ref=:sl, last_track_date=:td,
                            last_price=:lp, last_pct_chg=:lpc, cum_pct=:cp, max_price=:mp,
                            drawdown_from_peak=:dd, days_tracked=:dt,
                            ma5=:ma5, ma20=:ma20, ma60=:ma60, bias_ma20=:bias,
                            vol_ratio=:vr5, vol_ratio_20=:vr20, amount_yi=:amt,
                            buy_level=:bl, buy_score=:bs, track_note=:tn
                        WHERE pool_id = :pid
                        """
                    ),
                    {
                        "src": source_tag, "sl": _r(new_stop, 4), "td": trade_date,
                        "lp": _r(close_p, 4), "lpc": _r(pct_chg, 3), "cp": _r(cum_pct, 3),
                        "mp": _r(max_p, 4), "dd": _r(dd_peak, 3), "dt": days,
                        "ma5": _r(ma5, 4), "ma20": _r(ma20, 4), "ma60": _r(ma60, 4),
                        "bias": _r(bias, 3), "vr5": _r(vr5, 3), "vr20": _r(vr20, 3),
                        "amt": _r(amt_yi, 3), "bl": buy_level, "bs": buy_score,
                        "tn": track_note[:250], "pid": int(m["pool_id"]),
                    },
                )
                updated_cnt += 1

        # 2) 新符合条件的标的加入池子
        for code, sig in new_signals.items():
            if code in active_codes:
                continue
            close_p = float(sig["close_price"])
            pct_chg = float(sig.get("pct_chg") or 0.0)
            ma5 = _sf(sig.get("ma5"))
            ma20 = _sf(sig.get("ma20"))
            ma60 = _sf(sig.get("ma60"))
            bias = _sf(sig.get("bias_ma20"))
            if bias is None and ma20 and ma20 > 0:
                bias = (close_p / ma20 - 1.0) * 100.0
            vr5 = _sf(sig.get("vol_ratio"))
            vr20 = _sf(sig.get("vol_ratio_20"))
            amt_yi = _sf(sig.get("amount_yi"))
            brk_ref = _sf(sig.get("breakout_ref"))
            sl_ref = _sf(sig.get("stop_loss_ref"), ma20)
            src_tag = str(sig.get("source_tag") or "")

            buy_level, buy_score, track_note = rate_buy_level(
                asset_type=asset_type,
                source_tag=src_tag,
                close_price=close_p,
                pct_chg=pct_chg,
                ma5=ma5,
                ma20=ma20,
                ma60=ma60,
                bias_ma20=bias,
                vol_ratio=vr5,
                vol_ratio_20=vr20,
                cum_pct=0.0,
                drawdown_from_peak=0.0,
                days_tracked=1,
                is_new_entry=True,
                retriggered=False,
            )

            conn.execute(
                text(
                    """
                    INSERT INTO fact_launch_pool (
                        asset_type, code, name, status, entry_date, entry_price,
                        entry_reason, source_tag, breakout_ref, stop_loss_ref,
                        last_track_date, last_price, last_pct_chg, cum_pct,
                        max_price, drawdown_from_peak, days_tracked,
                        ma5, ma20, ma60, bias_ma20, vol_ratio, vol_ratio_20, amount_yi,
                        buy_level, buy_score, track_note
                    ) VALUES (
                        :at, :code, :name, 'ACTIVE', :td, :ep,
                        :er, :src, :brk, :sl,
                        :td, :ep, :lpc, 0,
                        :ep, 0, 1,
                        :ma5, :ma20, :ma60, :bias, :vr5, :vr20, :amt,
                        :bl, :bs, :tn
                    )
                    ON DUPLICATE KEY UPDATE
                        status='ACTIVE', name=VALUES(name), entry_reason=VALUES(entry_reason),
                        source_tag=VALUES(source_tag), breakout_ref=VALUES(breakout_ref),
                        stop_loss_ref=VALUES(stop_loss_ref), last_track_date=VALUES(last_track_date),
                        last_price=VALUES(last_price), last_pct_chg=VALUES(last_pct_chg),
                        ma5=VALUES(ma5), ma20=VALUES(ma20), ma60=VALUES(ma60),
                        bias_ma20=VALUES(bias_ma20), vol_ratio=VALUES(vol_ratio),
                        vol_ratio_20=VALUES(vol_ratio_20), amount_yi=VALUES(amount_yi),
                        buy_level=VALUES(buy_level), buy_score=VALUES(buy_score),
                        track_note=VALUES(track_note), exit_date=NULL, exit_reason=NULL
                    """
                ),
                {
                    "at": asset_type, "code": code, "name": str(sig["name"])[:64],
                    "td": trade_date, "ep": _r(close_p, 4),
                    "er": str(sig["entry_reason"])[:250], "src": src_tag,
                    "brk": _r(brk_ref, 4), "sl": _r(sl_ref, 4),
                    "lpc": _r(pct_chg, 3), "ma5": _r(ma5, 4), "ma20": _r(ma20, 4),
                    "ma60": _r(ma60, 4), "bias": _r(bias, 3), "vr5": _r(vr5, 3),
                    "vr20": _r(vr20, 3), "amt": _r(amt_yi, 3),
                    "bl": buy_level, "bs": buy_score, "tn": track_note[:250],
                },
            )
            added_cnt += 1

    return {
        "asset_type": asset_type,
        "trade_date": trade_date,
        "added": added_cnt,
        "updated": updated_cnt,
        "exited": exited_cnt,
        "signals_today": len(new_signals),
    }


def sync_stock_launch_pool(trade_date: Optional[str] = None) -> Dict[str, Any]:
    """同步个股 T10/T11 启动信号池：新符合入池 + 池内持续跟踪评级 + 破位自动移出。"""
    ensure_pool_table()
    d = trade_date or db.scalar("SELECT MAX(trade_date) FROM fact_indicator_daily")
    if not d:
        return {"asset_type": "STOCK", "status": "empty", "message": "暂无行情指标数据"}
    td = str(d)[:10]

    raw_df = cross_section.build(d)
    if raw_df.empty:
        return {"asset_type": "STOCK", "trade_date": td, "status": "empty"}

    # 全市场最新快照（用于跟踪池内老标的，即使当日成交额缩量也能持续跟踪）
    latest_snaps: Dict[str, Dict[str, Any]] = {}
    for _, r in raw_df.iterrows():
        c = str(r["stock_code"])
        cp = _sf(r.get("close_price"))
        if cp is None or cp <= 0:
            continue
        latest_snaps[c] = {
            "close_price": cp,
            "pct_chg": _sf(r.get("pct_chg_adj"), _sf(r.get("pct_chg"), 0.0)),
            "ma5": _sf(r.get("ma5")),
            "ma20": _sf(r.get("ma20")),
            "ma60": _sf(r.get("ma60")),
            "bias_ma20": _sf(r.get("bias_ma20")),
            "vol_ratio": _sf(r.get("vol_ratio")),
            "vol_ratio_20": _sf(r.get("vol_ratio_20")),
            "amount_yi": _sf(r.get("amount_yi")),
        }

    # 基础过滤后的候选集运行 T10 & T11
    cand = apply_filters(raw_df, {})
    m10 = eval_tree(TEMPLATES["T10"]["condition"], cand) if not cand.empty else pd.Series(dtype=bool)
    m11 = eval_tree(TEMPLATES["T11"]["condition"], cand) if not cand.empty else pd.Series(dtype=bool)

    new_signals: Dict[str, Dict[str, Any]] = {}
    if not cand.empty:
        hit_mask = m10 | m11
        for idx, r in cand[hit_mask].iterrows():
            code = str(r["stock_code"])
            hits = []
            if bool(m10.loc[idx]):
                hits.append("T10")
            if bool(m11.loc[idx]):
                hits.append("T11")
            new_signals[code] = {
                "code": code,
                "name": str(r.get("stock_name") or code),
                "source_tag": "+".join(hits),
                "entry_reason": _build_stock_entry_reason(r, hits),
                "close_price": float(r["close_price"]),
                "pct_chg": _sf(r.get("pct_chg_adj"), _sf(r.get("pct_chg"), 0.0)),
                "ma5": _sf(r.get("ma5")),
                "ma20": _sf(r.get("ma20")),
                "ma60": _sf(r.get("ma60")),
                "bias_ma20": _sf(r.get("bias_ma20")),
                "vol_ratio": _sf(r.get("vol_ratio")),
                "vol_ratio_20": _sf(r.get("vol_ratio_20")),
                "amount_yi": _sf(r.get("amount_yi")),
                "breakout_ref": _sf(r.get("high_20d_prev")),
                "stop_loss_ref": _sf(r.get("ma20")),
            }

    return _upsert_pool_updates("STOCK", td, new_signals, latest_snaps)


def _etf_secid(code: str) -> str:
    return f"1.{code}" if str(code).startswith(("5", "6")) else f"0.{code}"


def _snap_from_kline(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not rows or len(rows) < 25:
        return None
    last = rows[-1]
    closes = [r["close"] for r in rows]
    vols = [r["vol"] for r in rows]
    ma5 = sum(closes[-5:]) / 5.0
    ma20 = sum(closes[-20:]) / 20.0
    ma60 = sum(closes[-60:]) / 60.0 if len(closes) >= 60 else None
    vol_ma5 = sum(vols[-6:-1]) / 5.0 if len(vols) >= 6 else 0.0
    vol_ma20 = sum(vols[-21:-1]) / 20.0 if len(vols) >= 21 else 0.0
    return {
        "trade_date": last["date"],
        "close_price": last["close"],
        "pct_chg": last["pct"],
        "ma5": ma5,
        "ma20": ma20,
        "ma60": ma60,
        "bias_ma20": (last["close"] / ma20 - 1.0) * 100.0 if ma20 > 0 else 0.0,
        "vol_ratio": last["vol"] / vol_ma5 if vol_ma5 > 0 else 1.0,
        "vol_ratio_20": last["vol"] / vol_ma20 if vol_ma20 > 0 else 1.0,
        "amount_yi": last["amt"] / 1e8,
    }


def sync_etf_launch_pool(
    trade_date: Optional[str] = None,
    scanned_hits: Optional[List[Dict[str, Any]]] = None,
    kline_cache: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """同步 ETF 启动信号池：新触发 ETF 入池 + 已有池内 ETF 持续拉 K 线跟踪与移出。"""
    from .etf_launch import _fetch_etf_kline, ensure_etf_table

    ensure_pool_table()
    ensure_etf_table()

    if scanned_hits is None:
        td = trade_date or db.scalar("SELECT MAX(trade_date) FROM fact_etf_launch_signal")
        if not td:
            return {"asset_type": "ETF", "status": "empty", "message": "尚无 ETF 扫描记录"}
        td = str(td)[:10]
        df = db.query_df(
            "SELECT * FROM fact_etf_launch_signal WHERE trade_date = :d", {"d": td}
        )
        scanned_hits = df.to_dict(orient="records") if not df.empty else []
    else:
        td = str(trade_date)[:10] if trade_date else str(date.today())

    kline_cache = kline_cache or {}
    latest_snaps: Dict[str, Dict[str, Any]] = {}
    new_signals: Dict[str, Dict[str, Any]] = {}

    for h in scanned_hits:
        code = str(h["etf_code"])
        snap = _snap_from_kline(kline_cache[code]) if code in kline_cache else None
        ma5 = snap["ma5"] if snap else None
        ma20 = _sf(h.get("ma20"), snap["ma20"] if snap else None)
        ma60 = _sf(h.get("ma60"), snap["ma60"] if snap else None)
        cp = float(h["close_price"])
        latest_snaps[code] = {
            "close_price": cp,
            "pct_chg": _sf(h.get("pct_chg"), 0.0),
            "ma5": ma5,
            "ma20": ma20,
            "ma60": ma60,
            "bias_ma20": _sf(h.get("bias_ma20")),
            "vol_ratio": _sf(h.get("vol_ratio")),
            "vol_ratio_20": _sf(h.get("vol_ratio_20")),
            "amount_yi": _sf(h.get("amt_yi")),
        }
        new_signals[code] = {
            "code": code,
            "name": str(h["etf_name"]),
            "source_tag": f"Tier{int(h.get('tier', 2))}",
            "entry_reason": _build_etf_entry_reason(h),
            "close_price": cp,
            "pct_chg": _sf(h.get("pct_chg"), 0.0),
            "ma5": ma5,
            "ma20": ma20,
            "ma60": ma60,
            "bias_ma20": _sf(h.get("bias_ma20")),
            "vol_ratio": _sf(h.get("vol_ratio")),
            "vol_ratio_20": _sf(h.get("vol_ratio_20")),
            "amount_yi": _sf(h.get("amt_yi")),
            "breakout_ref": _sf(h.get("breakout_ref")),
            "stop_loss_ref": _sf(h.get("stop_loss_ref"), ma20),
        }

    # 为池内已有但不在今日扫描命中的 ACTIVE ETF 补充最新 K 线，确保不漏跟踪
    active_df = db.query_df(
        "SELECT code FROM fact_launch_pool WHERE asset_type = 'ETF' AND status = 'ACTIVE'"
    )
    need_fetch = []
    for _, r in active_df.iterrows():
        c = str(r["code"])
        if c in latest_snaps:
            continue
        if c in kline_cache:
            sn = _snap_from_kline(kline_cache[c])
            if sn:
                latest_snaps[c] = sn
        else:
            need_fetch.append(c)

    if need_fetch:
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(_fetch_etf_kline, _etf_secid(c), 100): c for c in need_fetch}
            for fut in as_completed(futs):
                c = futs[fut]
                try:
                    rows = fut.result()
                    sn = _snap_from_kline(rows) if rows else None
                    if sn:
                        latest_snaps[c] = sn
                except Exception:  # noqa: BLE001
                    pass

    return _upsert_pool_updates("ETF", td, new_signals, latest_snaps)


def query_launch_pool(
    asset_type: str = "STOCK",
    status: str = "ACTIVE",
    buy_level: Optional[str] = None,
    limit: int = 200,
) -> Dict[str, Any]:
    """查询启动信号池（支持按资产类型、状态、买入级别筛选及统计摘要）。"""
    ensure_pool_table()
    at = asset_type.upper()
    st = status.upper()

    summary_df = db.query_df(
        """
        SELECT status, buy_level, entry_date, last_track_date
        FROM fact_launch_pool
        WHERE asset_type = :at
        """,
        {"at": at},
    )
    latest_date = None
    active_cnt = exited_cnt = new_today_cnt = sa_cnt = b_cnt = 0
    if not summary_df.empty:
        dates = summary_df["last_track_date"].dropna().astype(str).tolist()
        if dates:
            latest_date = max(dates)[:10]
        for _, r in summary_df.iterrows():
            if r["status"] == "ACTIVE":
                active_cnt += 1
                if latest_date and str(r["entry_date"])[:10] == latest_date:
                    new_today_cnt += 1
                bl = str(r["buy_level"] or "")
                if bl.startswith(("S-", "A-")):
                    sa_cnt += 1
                elif bl.startswith("B-"):
                    b_cnt += 1
            else:
                exited_cnt += 1

    where = ["asset_type = :at"]
    params: Dict[str, Any] = {"at": at, "lim": limit}
    if st in ("ACTIVE", "EXITED"):
        where.append("status = :st")
        params["st"] = st
    if buy_level:
        where.append("buy_level LIKE :bl")
        params["bl"] = f"{buy_level}%"

    order = (
        "buy_score DESC, cum_pct DESC, entry_date DESC"
        if st != "EXITED"
        else "exit_date DESC, updated_at DESC"
    )
    df = db.query_df(
        f"""
        SELECT pool_id, asset_type, code, name, status,
               CAST(entry_date AS CHAR) AS entry_date, entry_price, entry_reason,
               source_tag, breakout_ref, stop_loss_ref,
               CAST(last_track_date AS CHAR) AS last_track_date,
               last_price, last_pct_chg, cum_pct, max_price, drawdown_from_peak,
               days_tracked, ma5, ma20, ma60, bias_ma20, vol_ratio, vol_ratio_20,
               amount_yi, buy_level, buy_score, track_note,
               CAST(exit_date AS CHAR) AS exit_date, exit_reason
        FROM fact_launch_pool
        WHERE {' AND '.join(where)}
        ORDER BY {order}
        LIMIT :lim
        """,
        params,
    )

    items = []
    for _, r in df.iterrows():
        rec = {}
        for k, v in r.items():
            if pd.isna(v):
                rec[k] = None
            elif hasattr(v, "item"):
                rec[k] = v.item()
            else:
                rec[k] = v
        rec["is_new_today"] = bool(latest_date and rec.get("entry_date") == latest_date and rec.get("status") == "ACTIVE")
        items.append(rec)

    return {
        "asset_type": at,
        "status": st,
        "latest_date": latest_date,
        "summary": {
            "active": active_cnt,
            "new_today": new_today_cnt,
            "sa_focus": sa_cnt,
            "b_pullback": b_cnt,
            "exited": exited_cnt,
        },
        "items": items,
    }
