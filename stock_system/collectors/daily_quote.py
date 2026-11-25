# -*- coding: utf-8 -*-
"""日线行情采集：不复权落地 + 复权序列落地 + 复权系数 + 涨跌停判定。

复权口径说明（对应需求文档 3.4 决策二与 FR-IND-04）：
- 原始价写 fact_daily_quote，供市值与涨跌停价计算；
- 复权价写 fact_daily_quote_adj，供均线/涨幅/突破类指标计算。

为什么用前复权（qfq）而不是后复权（hfq）：
    东财返回的 hfq 序列并非"原始价 × 固定累积因子"，其单位因子逐日漂移，
    导致复权价的日涨跌幅与原始价对不上（实测 0.87% vs 0.66%），长周期会累积失真。
    而 qfq 序列满足 qfq_t = raw_t × (F_t / F_T)，等价于以上市首日为基准、归一化到
    最新日的后复权序列，日收益率与真实收益率一致。因此复权价取 qfq，
    adj_factor = qfq / raw 即当日复权系数（最新交易日为 1）。

限制：qfq 会随新的除权事件整体平移，除权后需对该股全历史重算复权序列
（M2 实现除权检测与自动重算；本期先保证单次同步内的序列自洽）。
"""
import math
from datetime import date, datetime, timedelta

from .. import config, db
from ..datasource import AkshareSource
from ..datasource.akshare_source import Push2RateLimitError


def _f(v):
    """转 float，NaN/异常返回 None。"""
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) or math.isinf(x) else x


def _limit_status(row, limit_pct):
    pct = _f(row["pct_chg"])
    high, low, close = _f(row["high_price"]), _f(row["low_price"]), _f(row["close_price"])
    if pct is None:
        return "NORMAL"
    tol = config.LIMIT_TOLERANCE

    # 新股上市初期、退市整理期等实际涨跌幅可超过常规限制，按无限制处理
    if abs(pct) > limit_pct + 1.0:
        return "NORMAL"

    one_word = high is not None and low is not None and close is not None and high == low
    if pct >= limit_pct - tol:
        return "ONE_WORD_UP" if one_word else "UP"
    if pct <= -(limit_pct - tol):
        return "ONE_WORD_DOWN" if one_word else "DOWN"
    return "NORMAL"


def _read_progress(path, key, total):
    """读取断点。key = "起始日~结束日"，换了区间即视为全新一轮。"""
    if not path:
        return 0
    try:
        with open(path, encoding="utf-8") as f:
            k, v = f.read().strip().split("|", 1)
        if k != key:
            return 0
        n = int(v)
        return n if 0 <= n < total else 0
    except Exception:  # noqa: BLE001
        return 0


def _write_progress(path, key, done):
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"{key}|{done}")
    except Exception:  # noqa: BLE001
        pass


def sync_daily_quote(
    codes=None,
    start=None,
    end=None,
    limit=None,
    source: AkshareSource = None,
    progress_every: int = 50,
    chunk_size: int = 0,
    progress_file: str = None,
):
    """同步日线。codes 为空则同步全市场在市股票。"""
    src = source or AkshareSource()
    end = end or date.today().strftime("%Y%m%d")

    # 当日行情时间闸：收盘（15:00）之前数据源还没有当天 K 线，
    # 此时请求"今天"只会拿到空结果，白白消耗几千次配额并撞限流。
    # 这里把 end 回退到上一个交易日，手动触发和定时任务都受保护。
    ready = datetime.now().replace(
        hour=config.DAILY_SYNC_READY_HOUR,
        minute=config.DAILY_SYNC_READY_MINUTE,
        second=0,
        microsecond=0,
    )
    if datetime.now() < ready and end >= date.today().strftime("%Y%m%d"):
        prev = db.query_df(
            "SELECT MAX(cal_date) AS d FROM dim_trade_calendar WHERE is_open=1 AND cal_date < :t",
            {"t": date.today()},
        )
        if not prev.empty and prev.iloc[0]["d"] is not None:
            end = prev.iloc[0]["d"].strftime("%Y%m%d")

    if start is None:
        last = db.scalar("SELECT MAX(trade_date) FROM fact_daily_quote")
        start = (last + timedelta(days=1)).strftime("%Y%m%d") if last else config.HISTORY_START

    if codes is None:
        meta = db.query_df(
            "SELECT stock_code, board, is_st FROM dim_stock WHERE list_status='L' ORDER BY stock_code"
        )
        if limit:
            meta = meta.head(limit)
        codes = meta["stock_code"].tolist()
        board_map = dict(zip(meta["stock_code"], meta["board"]))
        st_map = dict(zip(meta["stock_code"], meta["is_st"]))
    else:
        codes = list(codes)[: limit or None]
        board_map, st_map = {}, {}

    if start > end:
        return {"start": start, "end": end, "stocks": 0, "quote_rows": 0, "skipped": "无新增交易日"}

    task_id = db.log_task_start(
        "sync_daily_quote", "daily_quote", f"start={start} end={end} n={len(codes)}"
    )
    db.log_task_progress(task_id, total_cnt=len(codes), success_cnt=0, fail_cnt=0)

    ok, fail, total_rows, fail_list = 0, 0, 0, []
    rate_limited = False
    pending_codes = []
    consec_fail = 0

    # ---- 分片与断点续跑 ----
    # 熔断后不用从 000001 重来：每完成一片就把进度落盘，下次直接从断点继续。
    # 进度只在"整片完成"时前进，保证中断时重跑的那一片不会漏股票。
    total_n = len(codes)
    key = f"{start}~{end}"
    skip_n = _read_progress(progress_file, key, total_n)
    if skip_n:
        print(f"[daily_quote] 断点续跑：已完成 {skip_n}/{total_n}，从第 {skip_n + 1} 只继续", flush=True)
        codes = codes[skip_n:]
    span = chunk_size if chunk_size and chunk_size > 0 else max(len(codes), 1)

    for base in range(0, len(codes), span):
        part = codes[base:base + span]
        for j, code in enumerate(part, 1):
            idx = skip_n + base + j
            try:
                raw = src.get_daily_quote(code, start, end, "")
                consec_fail = 0
                if raw.empty:
                    ok += 1
                    continue
                qfq = src.get_daily_quote(code, start, end, "qfq")
                qfq_map = {r["trade_date"]: r for _, r in qfq.iterrows()} if not qfq.empty else {}

                board = board_map.get(code) or _guess_board(code)
                limit_pct = config.ST_LIMIT if st_map.get(code) else config.LIMIT_RULES.get(board, 10.0)

                quote_rows, adj_rows = [], []
                for _, r in raw.iterrows():
                    d = r["trade_date"]
                    close = _f(r["close_price"])
                    q = qfq_map.get(d)
                    q_close = _f(q["close_price"]) if q is not None else None
                    factor = (q_close / close) if (q_close and close) else None

                    quote_rows.append(
                        (
                            code, d,
                            _f(r["open_price"]), _f(r["high_price"]), _f(r["low_price"]), close,
                            _f(r["pre_close"]),
                            int(r["volume"]) if _f(r["volume"]) is not None else None,
                            _f(r["amount"]), _f(r["amplitude"]), _f(r["pct_chg"]),
                            _f(q["pct_chg"]) if q is not None else None,
                            _f(r["chg"]), _f(r["turnover_rate"]), factor, 0, _limit_status(r, limit_pct),
                        )
                    )

                    if q is not None:
                        q_pre = _f(q["pre_close"])
                        adj_rows.append(
                            (
                                code, d,
                                _f(q["open_price"]), _f(q["high_price"]), _f(q["low_price"]),
                                q_close, q_pre,
                                int(r["volume"]) if _f(r["volume"]) is not None else None,
                                _f(r["amount"]),
                            )
                        )

                db.bulk_upsert(
                    "fact_daily_quote",
                    ["stock_code", "trade_date", "open_price", "high_price", "low_price", "close_price",
                     "pre_close", "volume", "amount", "amplitude", "pct_chg", "pct_chg_adj", "chg",
                     "turnover_rate", "adj_factor", "is_suspended", "limit_status"],
                    quote_rows,
                    update_columns=["open_price", "high_price", "low_price", "close_price", "pre_close",
                                    "volume", "amount", "amplitude", "pct_chg", "pct_chg_adj", "chg",
                                    "turnover_rate", "adj_factor", "limit_status"],
                )
                if adj_rows:
                    db.bulk_upsert(
                        "fact_daily_quote_adj",
                        ["stock_code", "trade_date", "open_adj", "high_adj", "low_adj", "close_adj",
                         "pre_close_adj", "volume", "amount"],
                        adj_rows,
                        update_columns=["open_adj", "high_adj", "low_adj", "close_adj", "pre_close_adj",
                                        "volume", "amount"],
                    )
                total_rows += len(quote_rows)
                ok += 1
            except Exception as exc:  # noqa: BLE001
                # IP 被东方财富限流：继续跑只会延长封禁，立即熔断整批。
                # 剩余股票（含当前这只）留给下一轮补，不在这里消耗重试。
                if Push2RateLimitError is not None and isinstance(exc, Push2RateLimitError):
                    rate_limited = True
                    pending_codes = codes[base + j - 1:]
                    print(
                        f"[daily_quote] 触发东财 push2 限流，已熔断："
                        f"成功 {ok} 只，待补 {len(pending_codes)} 只（从 {code} 起）",
                        flush=True,
                    )
                    break
                fail += 1
                consec_fail += 1
                fail_list.append(f"{code}: {exc}")
                # 连续失败过多：大概率是数据源整体拒绝（腾讯/网络层），同样熔断，
                # 避免像"逐只失败到底"那样既没效率又加重对方负担。
                if consec_fail >= config.MAX_CONSEC_FAIL:
                    rate_limited = True
                    pending_codes = codes[base + j - 1:]
                    print(
                        f"[daily_quote] 连续失败 {consec_fail} 只，判定数据源不可用，已熔断："
                        f"成功 {ok} 只，待补 {len(pending_codes)} 只（从 {code} 起）",
                        flush=True,
                    )
                    break
            if idx % progress_every == 0:
                print(f"  进度 {idx}/{total_n}  成功 {ok} 失败 {fail} 行数 {total_rows}", flush=True)
                db.log_task_progress(task_id, success_cnt=idx, fail_cnt=fail)

        if rate_limited:
            break
        # 整片处理完才推进断点，保证中断时重跑的那一片不漏股票
        _write_progress(progress_file, key, skip_n + base + len(part))

    if rate_limited:
        status = "interrupted"
        msg = (
            f"东财 push2 限流自动熔断：成功 {ok} 只，待补 {len(pending_codes)} 只"
            f"（{pending_codes[0]} ~ {pending_codes[-1]}）。请等待 5~40 分钟后重跑，"
            f"期间不要重复调用行情接口。"
        )
        db.log_task_end(task_id, status, total_n, ok, fail, msg)
        return {
            "start": start, "end": end, "stocks": total_n, "done_before": skip_n,
            "ok": ok, "fail": fail, "quote_rows": total_rows,
            "rate_limited": True, "pending": len(pending_codes),
            "pending_from": pending_codes[0], "message": msg,
        }

    status = "success" if fail == 0 else ("partial" if ok else "failed")
    db.log_task_end(task_id, status, total_n, ok, fail, "; ".join(fail_list[:5]))
    return {
        "start": start, "end": end, "stocks": total_n, "done_before": skip_n,
        "ok": ok, "fail": fail, "quote_rows": total_rows,
        "fail_samples": fail_list[:5],
    }


def _guess_board(code: str):
    if code.startswith("68"):
        return "科创板"
    if code.startswith("60"):
        return "主板"
    if code.startswith("30"):
        return "创业板"
    if code.startswith(("00", "001", "002", "003")):
        return "主板"
    if code.startswith(("8", "4", "92")):
        return "北交所"
    return "主板"
