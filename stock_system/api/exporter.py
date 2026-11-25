# -*- coding: utf-8 -*-
"""筛选结果导出 Excel：结果清单 + 命中明细 + 条件快照。"""
import io
import json

from openpyxl import Workbook
from openpyxl.chart import BarChart, PieChart, Reference
from openpyxl.chart.label import DataLabelList
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .. import db

HEADER_FILL = PatternFill("solid", fgColor="1565C0")
HEADER_FONT = Font(color="FFFFFF", bold=True, size=10, name="微软雅黑")
BODY_FONT = Font(size=10, name="微软雅黑")
TITLE_FONT = Font(size=13, bold=True, name="微软雅黑")
THIN = Side(style="thin", color="D5DCE6")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
UP_FONT = Font(size=10, name="微软雅黑", color="D32F2F")
DOWN_FONT = Font(size=10, name="微软雅黑", color="2E7D32")

OP_TEXT = {
    "gt": ">", "gte": "≥", "lt": "<", "lte": "≤", "eq": "=", "neq": "≠",
    "between": "介于", "not_between": "不在", "in": "属于", "not_in": "不属于",
    "is_true": "为真", "is_false": "为假", "pct_between": "百分位", "top_n": "前N名",
}


def _load(run_id):
    run = db.query_df(
        """
        SELECT r.run_id, r.data_date, r.run_type, r.condition_json, r.filter_json,
               r.candidate_cnt, r.hit_cnt, r.elapsed_ms, r.created_at, s.strategy_name
        FROM fact_screen_run r
        LEFT JOIN dim_strategy s ON s.strategy_id = r.strategy_id
        WHERE r.run_id = :i
        """,
        {"i": run_id},
    )
    if run.empty:
        raise ValueError(f"执行记录 {run_id} 不存在")
    head = run.iloc[0].to_dict()

    rows = db.query_df(
        """
        SELECT res.rank_no, res.stock_code, s.stock_name, s.board, res.score, res.hit_cnt,
               res.hit_detail, q.close_price, q.pct_chg_adj, q.turnover_rate, q.amount,
               ROUND(q.close_price * s.total_share / 100000000, 2) AS total_mv_yi
        FROM fact_screen_result res
        JOIN dim_stock s ON s.stock_code = res.stock_code
        LEFT JOIN fact_daily_quote q
               ON q.stock_code = res.stock_code AND q.trade_date = :d
        WHERE res.run_id = :i
        ORDER BY res.rank_no
        """,
        {"i": run_id, "d": head["data_date"]},
    )
    return head, rows


def _style_header(ws, ncol, row=1):
    for c in range(1, ncol + 1):
        cell = ws.cell(row=row, column=c)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = BORDER


def _set_widths(ws, widths):
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


def _collect_stats(rows):
    """汇总图表所需的四组数据。

    第三张图用市值分档而非「评分前十」：score 是命中集内的百分位，
    按定义近似均匀分布，前十名的柱高几乎一样（99.98 / 99.96 / …），
    画出来没有信息量。市值分档能看出策略偏大盘还是小盘。
    """
    board_order, board_cnt = [], {}
    buckets = ["<-5%", "-5~-2%", "-2~0%", "0~2%", "2~5%", ">5%"]
    bucket_cnt = dict.fromkeys(buckets, 0)
    mv_labels = ["<30亿", "30-100亿", "100-300亿", "300-1000亿", ">1000亿"]
    mv_cnt = dict.fromkeys(mv_labels, 0)
    mv_unknown = 0

    for rec in rows.itertuples(index=False):
        b = rec.board or "未知"
        if b not in board_cnt:
            board_cnt[b] = 0
            board_order.append(b)
        board_cnt[b] += 1

        v = rec.pct_chg_adj
        if v is not None:
            v = float(v)
            if v < -5:
                bucket_cnt["<-5%"] += 1
            elif v < -2:
                bucket_cnt["-5~-2%"] += 1
            elif v < 0:
                bucket_cnt["-2~0%"] += 1
            elif v < 2:
                bucket_cnt["0~2%"] += 1
            elif v < 5:
                bucket_cnt["2~5%"] += 1
            else:
                bucket_cnt[">5%"] += 1

        mv = rec.total_mv_yi
        if mv is None:
            mv_unknown += 1
        else:
            mv = float(mv)
            if mv < 30:
                mv_cnt["<30亿"] += 1
            elif mv < 100:
                mv_cnt["30-100亿"] += 1
            elif mv < 300:
                mv_cnt["100-300亿"] += 1
            elif mv < 1000:
                mv_cnt["300-1000亿"] += 1
            else:
                mv_cnt[">1000亿"] += 1

    if mv_unknown:
        mv_labels = mv_labels + ["未知"]
        mv_cnt["未知"] = mv_unknown

    return board_order, board_cnt, buckets, bucket_cnt, mv_labels, mv_cnt


def _add_charts(wb, rows):
    """第四个工作表：板块分布 / 涨跌幅分布 / 评分前十。

    用 openpyxl 原生图表而非图片——接收方可以在 Excel 里继续改样式、换数据源。
    图表引用同表底部的数据区（第 46 行起），避免依赖隐藏单元格
    （Excel 默认不绘制隐藏区，隐藏后图会变空白）。
    """
    if rows.empty:
        return

    ws = wb.create_sheet("图表")
    board_order, board_cnt, buckets, bucket_cnt, mv_labels, mv_cnt = _collect_stats(rows)

    DATA_ROW = 46
    # ---- 底表：板块分布 ----
    ws.cell(row=DATA_ROW, column=1, value="板块")
    ws.cell(row=DATA_ROW, column=2, value="数量")
    for i, b in enumerate(board_order, start=1):
        ws.cell(row=DATA_ROW + i, column=1, value=b)
        ws.cell(row=DATA_ROW + i, column=2, value=board_cnt[b])
    board_end = DATA_ROW + len(board_order)

    # ---- 底表：涨跌幅分档 ----
    ws.cell(row=DATA_ROW, column=4, value="区间")
    ws.cell(row=DATA_ROW, column=5, value="数量")
    for i, k in enumerate(buckets, start=1):
        ws.cell(row=DATA_ROW + i, column=4, value=k)
        ws.cell(row=DATA_ROW + i, column=5, value=bucket_cnt[k])
    bucket_end = DATA_ROW + len(buckets)

    # ---- 底表：市值分档 ----
    ws.cell(row=DATA_ROW, column=7, value="市值档")
    ws.cell(row=DATA_ROW, column=8, value="数量")
    for i, k in enumerate(mv_labels, start=1):
        ws.cell(row=DATA_ROW + i, column=7, value=k)
        ws.cell(row=DATA_ROW + i, column=8, value=mv_cnt[k])
    mv_end = DATA_ROW + len(mv_labels)

    ws.cell(row=DATA_ROW - 2, column=1,
            value="以下为图表数据源，供上方图表引用，可自行调整").font = Font(
        size=9, name="微软雅黑", color="9CA3AF")

    # ---- 图 1：板块分布（饼图） ----
    pie = PieChart()
    pie.title = "命中标的板块分布"
    pie.height, pie.width = 8.2, 11
    data = Reference(ws, min_col=2, min_row=DATA_ROW, max_row=board_end)
    cats = Reference(ws, min_col=1, min_row=DATA_ROW + 1, max_row=board_end)
    pie.add_data(data, titles_from_data=True)
    pie.set_categories(cats)
    pie.dataLabels = DataLabelList()
    pie.dataLabels.showPercent = True
    pie.dataLabels.showVal = False
    ws.add_chart(pie, "A1")

    # ---- 图 2：涨跌幅分布（柱状图） ----
    bar = BarChart()
    bar.type = "col"
    bar.title = "命中标的涨跌幅分布"
    bar.y_axis.title = "只数"
    bar.height, bar.width = 8.2, 11
    data = Reference(ws, min_col=5, min_row=DATA_ROW, max_row=bucket_end)
    cats = Reference(ws, min_col=4, min_row=DATA_ROW + 1, max_row=bucket_end)
    bar.add_data(data, titles_from_data=True)
    bar.set_categories(cats)
    bar.legend = None
    bar.dataLabels = DataLabelList()
    bar.dataLabels.showVal = True
    ws.add_chart(bar, "A18")

    # ---- 图 3：市值分档（柱状图） ----
    mv_chart = BarChart()
    mv_chart.type = "col"
    mv_chart.title = "命中标的市值分档"
    mv_chart.y_axis.title = "只数"
    mv_chart.height, mv_chart.width = 8.2, 11
    data = Reference(ws, min_col=8, min_row=DATA_ROW, max_row=mv_end)
    cats = Reference(ws, min_col=7, min_row=DATA_ROW + 1, max_row=mv_end)
    mv_chart.add_data(data, titles_from_data=True)
    mv_chart.set_categories(cats)
    mv_chart.legend = None
    mv_chart.dataLabels = DataLabelList()
    mv_chart.dataLabels.showVal = True
    ws.add_chart(mv_chart, "H1")

    _set_widths(ws, [14, 9, 3, 12, 9, 3, 14, 9])


def export_run(run_id: int):
    """返回 (xlsx 字节流, 文件名)。"""
    head, rows = _load(run_id)
    wb = Workbook()

    # ---------- Sheet1 结果清单 ----------
    ws = wb.active
    ws.title = "筛选结果"
    title = head.get("strategy_name") or f"自定义筛选 #{run_id}"
    ws.cell(row=1, column=1, value=f"{title}　数据日期 {head['data_date']}").font = TITLE_FONT
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=11)
    ws.cell(row=2, column=1,
            value=f"候选 {head['candidate_cnt']} 只　命中 {head['hit_cnt']} 只　"
                  f"耗时 {head['elapsed_ms']} ms　数据口径：前复权").font = Font(
        size=9, name="微软雅黑", color="6B7280")
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=11)

    headers = ["排名", "代码", "简称", "板块", "收盘价", "涨跌幅%", "换手率%",
               "成交额(亿)", "总市值(亿)", "综合评分", "命中条件数"]
    for i, h in enumerate(headers, 1):
        ws.cell(row=3, column=i, value=h)
    _style_header(ws, len(headers), row=3)

    for r, rec in enumerate(rows.itertuples(index=False), start=4):
        vals = [
            int(rec.rank_no) if rec.rank_no else None,
            rec.stock_code, rec.stock_name, rec.board,
            float(rec.close_price) if rec.close_price is not None else None,
            float(rec.pct_chg_adj) if rec.pct_chg_adj is not None else None,
            float(rec.turnover_rate) if rec.turnover_rate is not None else None,
            round(float(rec.amount) / 1e8, 2) if rec.amount is not None else None,
            float(rec.total_mv_yi) if rec.total_mv_yi is not None else None,
            float(rec.score) if rec.score is not None else None,
            int(rec.hit_cnt) if rec.hit_cnt else None,
        ]
        for c, v in enumerate(vals, 1):
            cell = ws.cell(row=r, column=c, value=v)
            cell.font = BODY_FONT
            cell.border = BORDER
            if c in (1, 2, 3, 4):
                cell.alignment = Alignment(horizontal="center")
            elif c in (6, 7):
                cell.number_format = "0.00"
                if v is not None:
                    cell.font = UP_FONT if v > 0 else (DOWN_FONT if v < 0 else BODY_FONT)
            elif c in (5, 8, 9, 10):
                cell.number_format = "0.00"

    ws.freeze_panes = "A4"
    _set_widths(ws, [6, 10, 12, 10, 10, 10, 10, 12, 12, 10, 12])

    # ---------- Sheet2 命中明细 ----------
    ws2 = wb.create_sheet("命中明细")
    h2 = ["排名", "代码", "简称", "条件", "运算符", "阈值", "实际值", "是否通过"]
    for i, h in enumerate(h2, 1):
        ws2.cell(row=1, column=i, value=h)
    _style_header(ws2, len(h2))

    r = 2
    for rec in rows.itertuples(index=False):
        detail = rec.hit_detail
        if isinstance(detail, str):
            try:
                detail = json.loads(detail)
            except json.JSONDecodeError:
                detail = []
        for d in detail or []:
            thr = d.get("threshold")
            if isinstance(thr, list):
                thr = " ~ ".join(str(x) for x in thr)
            ws2.cell(row=r, column=1, value=int(rec.rank_no) if rec.rank_no else None)
            ws2.cell(row=r, column=2, value=rec.stock_code)
            ws2.cell(row=r, column=3, value=rec.stock_name)
            ws2.cell(row=r, column=4, value=d.get("field"))
            ws2.cell(row=r, column=5, value=OP_TEXT.get(d.get("op"), d.get("op")))
            ws2.cell(row=r, column=6, value=thr)
            ws2.cell(row=r, column=7, value=d.get("value"))
            ws2.cell(row=r, column=8, value="通过" if d.get("pass") else "未通过")
            for c in range(1, 9):
                ws2.cell(row=r, column=c).font = BODY_FONT
                ws2.cell(row=r, column=c).border = BORDER
            r += 1

    ws2.freeze_panes = "A2"
    _set_widths(ws2, [6, 10, 12, 16, 9, 14, 12, 10])

    # ---------- Sheet3 条件快照 ----------
    ws3 = wb.create_sheet("条件快照")
    ws3.cell(row=1, column=1, value="筛选条件（JSON）").font = TITLE_FONT
    cond = head.get("condition_json")
    if isinstance(cond, str):
        try:
            cond = json.loads(cond)
        except json.JSONDecodeError:
            pass
    text = json.dumps(cond, ensure_ascii=False, indent=2)
    for i, line in enumerate(text.split("\n"), start=3):
        ws3.cell(row=i, column=1, value=line).font = Font(size=9, name="Consolas")
    ws3.column_dimensions["A"].width = 96

    # ---------- Sheet4 图表 ----------
    _add_charts(wb, rows)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"筛选结果_{head['data_date']}_run{run_id}.xlsx"
    return buf.getvalue(), filename
