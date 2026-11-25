# -*- coding: utf-8 -*-
"""数据库连接与批量写入工具。"""
from contextlib import contextmanager
from datetime import datetime

import pymysql
from sqlalchemy import create_engine, event, text

from . import config

engine = create_engine(
    config.DB_URL,
    pool_size=10,
    max_overflow=5,
    pool_recycle=3600,
    pool_pre_ping=True,
    future=True,
    # 大批量取数时客户端可能短暂读不动（内存换页），若不给超时，
    # 服务端默认 net_write_timeout=60s 就会先掐断，表现为
    # 2013 Lost connection to MySQL server during query。
    connect_args={"read_timeout": 600, "write_timeout": 600},
)


@event.listens_for(engine, "connect")
def _session_setup(dbapi_conn, _record):
    """每条新连接放大会话级读写超时。

    这两个是会话变量，只能在 SET 里改；不能写成 mysql 客户端命令行的
    --net_write_timeout（会报 unknown variable）。失败不影响主流程。
    """
    try:
        cur = dbapi_conn.cursor()
        cur.execute("SET SESSION net_write_timeout=600")
        cur.execute("SET SESSION net_read_timeout=600")
        cur.close()
    except Exception:  # noqa: BLE001 - 老版本/受限账号忽略即可
        pass



@contextmanager
def raw_conn():
    """取一条原生 pymysql 连接（用于 executemany 批量写入）。"""
    conn = engine.raw_connection()
    try:
        yield conn
    finally:
        conn.close()


def bulk_upsert(table, columns, rows, update_columns=None, coalesce_columns=None, chunk=None):
    """批量写入，主键冲突则更新。

    rows: list[tuple]，顺序与 columns 一致。
    coalesce_columns: 这些列更新时保留已有非空值（新值为 NULL 不覆盖旧值）。
    返回写入行数。
    """
    if not rows:
        return 0
    chunk = chunk or config.BATCH_SIZE
    cols = ",".join(f"`{c}`" for c in columns)
    holders = ",".join(["%s"] * len(columns))
    if update_columns:
        coalesce = set(coalesce_columns or ())
        parts = []
        for c in update_columns:
            if c in coalesce:
                parts.append(f"`{c}`=COALESCE(VALUES(`{c}`), `{c}`)")
            else:
                parts.append(f"`{c}`=VALUES(`{c}`)")
        sql = f"INSERT INTO `{table}` ({cols}) VALUES ({holders}) ON DUPLICATE KEY UPDATE " + ",".join(parts)
    else:
        sql = f"INSERT INTO `{table}` ({cols}) VALUES ({holders}) ON DUPLICATE KEY UPDATE `{columns[0]}`=`{columns[0]}`"

    written = 0
    with raw_conn() as conn:
        cur = conn.cursor()
        try:
            for i in range(0, len(rows), chunk):
                batch = rows[i : i + chunk]
                cur.executemany(sql, batch)
                written += len(batch)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
    return written


def query_df(sql, params=None):
    """执行查询并返回 DataFrame。"""
    import pandas as pd

    with engine.connect() as conn:
        return pd.read_sql(text(sql), conn, params=params or {})


def scalar(sql, params=None, default=None):
    """取单值。"""
    with engine.connect() as conn:
        v = conn.execute(text(sql), params or {}).scalar()
        return default if v is None else v


def log_task_start(task_name, task_type, params=""):
    with engine.begin() as conn:
        res = conn.execute(
            text(
                "INSERT INTO sys_task_log (task_name, task_type, start_time, status, params) "
                "VALUES (:n, :t, :s, 'running', :p)"
            ),
            {"n": task_name, "t": task_type, "s": datetime.now(), "p": params[:500]},
        )
        return res.lastrowid


def log_task_end(task_id, status, total_cnt=0, success_cnt=0, fail_cnt=0, error_msg=None):
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE sys_task_log SET end_time=:e, status=:st, total_cnt=:tc, "
                "success_cnt=:sc, fail_cnt=:fc, error_msg=:err WHERE task_id=:id"
            ),
            {
                "e": datetime.now(),
                "st": status,
                "tc": total_cnt,
                "sc": success_cnt,
                "fc": fail_cnt,
                "err": (error_msg or "")[:1000],
                "id": task_id,
            },
        )


def log_task_progress(task_id, total_cnt=None, success_cnt=None, fail_cnt=None):
    """任务运行中上报进度，供界面显示百分比。

    只更新传入的字段，不动 status 与 end_time，避免被误判为已结束。
    """
    sets, params = [], {"id": task_id}
    if total_cnt is not None:
        sets.append("total_cnt = :tc")
        params["tc"] = int(total_cnt)
    if success_cnt is not None:
        sets.append("success_cnt = :sc")
        params["sc"] = int(success_cnt)
    if fail_cnt is not None:
        sets.append("fail_cnt = :fc")
        params["fc"] = int(fail_cnt)
    if not sets:
        return
    with engine.begin() as conn:
        conn.execute(
            text(f"UPDATE sys_task_log SET {', '.join(sets)} WHERE task_id = :id"), params
        )
