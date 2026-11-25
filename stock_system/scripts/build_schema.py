# -*- coding: utf-8 -*-
"""生成并执行 MySQL 建表脚本（对应需求文档 4.2 数据表清单）。

用法：
    python scripts/build_schema.py            # 建表（IF NOT EXISTS）
    python scripts/build_schema.py --sql-only # 只生成 sql/01_schema.sql，不执行
"""
import os
import sys

import pymysql

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SQL_PATH = os.path.join(BASE_DIR, "sql", "01_schema.sql")

DB = {
    "host": "127.0.0.1",
    "port": 3306,
    "user": "stock_app",
    "password": "StockSys_2026",
    "database": "stock_system",
    "charset": "utf8mb4",
}

TAIL = "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='%s'"


def year_partitions(start=2005, end=2027):
    """按年 RANGE 分区子句。分区键 trade_date 已包含在主键中。"""
    parts = []
    for y in range(start, end + 1):
        parts.append(f"  PARTITION p{y} VALUES LESS THAN (TO_DAYS('{y + 1}-01-01'))")
    parts.append("  PARTITION pmax VALUES LESS THAN MAXVALUE")
    return "PARTITION BY RANGE (TO_DAYS(trade_date)) (\n" + ",\n".join(parts) + "\n)"


DDL = []

# ---------- 维度表 ----------
DDL.append(f"""
CREATE TABLE IF NOT EXISTS dim_stock (
  stock_code   VARCHAR(10)  NOT NULL COMMENT '证券代码',
  stock_name   VARCHAR(32)  NOT NULL COMMENT '证券简称',
  market       VARCHAR(8)   NOT NULL COMMENT '市场 SH/SZ/BJ',
  board        VARCHAR(16)  DEFAULT NULL COMMENT '板块 主板/创业板/科创板/北交所',
  list_date    DATE         DEFAULT NULL COMMENT '上市日期',
  delist_date  DATE         DEFAULT NULL COMMENT '退市日期',
  list_status  VARCHAR(8)   NOT NULL DEFAULT 'L' COMMENT 'L上市 D退市 P暂停',
  is_st        TINYINT(1)   NOT NULL DEFAULT 0 COMMENT '是否ST',
  total_share  DECIMAL(20,2) DEFAULT NULL COMMENT '总股本(股)',
  float_share  DECIMAL(20,2) DEFAULT NULL COMMENT '流通股本(股)',
  industry_code VARCHAR(16) DEFAULT NULL COMMENT '所属行业代码',
  updated_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (stock_code),
  KEY idx_board (board),
  KEY idx_status (list_status)
) {TAIL % '股票基础信息'}
""")

DDL.append(f"""
CREATE TABLE IF NOT EXISTS dim_stock_name_history (
  stock_code  VARCHAR(10) NOT NULL COMMENT '证券代码',
  change_date DATE        NOT NULL COMMENT '变更生效日',
  old_name    VARCHAR(32) NOT NULL COMMENT '原简称',
  new_name    VARCHAR(32) NOT NULL COMMENT '新简称',
  PRIMARY KEY (stock_code, change_date, old_name)
) {TAIL % '证券简称变更历史'}
""")

DDL.append(f"""
CREATE TABLE IF NOT EXISTS dim_trade_calendar (
  cal_date        DATE       NOT NULL COMMENT '日期',
  is_open         TINYINT(1) NOT NULL DEFAULT 1 COMMENT '是否交易日',
  prev_trade_date DATE       DEFAULT NULL COMMENT '上一交易日',
  next_trade_date DATE       DEFAULT NULL COMMENT '下一交易日',
  PRIMARY KEY (cal_date),
  KEY idx_open (is_open)
) {TAIL % '交易日历'}
""")

DDL.append(f"""
CREATE TABLE IF NOT EXISTS dim_industry (
  industry_code VARCHAR(16) NOT NULL COMMENT '行业代码',
  industry_name VARCHAR(64) NOT NULL COMMENT '行业名称',
  level         TINYINT     NOT NULL DEFAULT 1 COMMENT '层级 1/2',
  parent_code   VARCHAR(16) DEFAULT NULL COMMENT '上级行业代码',
  src           VARCHAR(16) NOT NULL DEFAULT 'sw' COMMENT '来源',
  PRIMARY KEY (industry_code),
  KEY idx_parent (parent_code)
) {TAIL % '行业分类'}
""")

DDL.append(f"""
CREATE TABLE IF NOT EXISTS dim_concept (
  concept_code VARCHAR(24) NOT NULL COMMENT '概念板块代码',
  concept_name VARCHAR(64) NOT NULL COMMENT '概念板块名称',
  src          VARCHAR(16) NOT NULL DEFAULT 'em' COMMENT '来源',
  updated_at   DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (concept_code),
  KEY idx_name (concept_name)
) {TAIL % '概念板块'}
""")

DDL.append(f"""
CREATE TABLE IF NOT EXISTS dim_index (
  index_code VARCHAR(16) NOT NULL COMMENT '指数代码',
  index_name VARCHAR(64) NOT NULL COMMENT '指数名称',
  base_date  DATE        DEFAULT NULL COMMENT '基日',
  base_point DECIMAL(16,4) DEFAULT NULL COMMENT '基点',
  PRIMARY KEY (index_code)
) {TAIL % '指数清单'}
""")

# ---------- 事实表 ----------
DDL.append(f"""
CREATE TABLE IF NOT EXISTS fact_daily_quote (
  stock_code    VARCHAR(10)  NOT NULL COMMENT '证券代码',
  trade_date    DATE         NOT NULL COMMENT '交易日',
  open_price    DECIMAL(12,3) DEFAULT NULL COMMENT '开盘价(不复权)',
  high_price    DECIMAL(12,3) DEFAULT NULL COMMENT '最高价',
  low_price     DECIMAL(12,3) DEFAULT NULL COMMENT '最低价',
  close_price   DECIMAL(12,3) DEFAULT NULL COMMENT '收盘价',
  pre_close     DECIMAL(12,3) DEFAULT NULL COMMENT '前收盘价(除权日为除权参考价)',
  volume        BIGINT       DEFAULT NULL COMMENT '成交量(手)',
  amount        DECIMAL(20,2) DEFAULT NULL COMMENT '成交额(元)',
  amplitude     DECIMAL(8,3) DEFAULT NULL COMMENT '振幅(%)',
  pct_chg       DECIMAL(8,3) DEFAULT NULL COMMENT '涨跌幅(%%，原始价口径)',
  pct_chg_adj   DECIMAL(8,3) DEFAULT NULL COMMENT '复权涨跌幅(%%，除权日亦正确)',
  chg           DECIMAL(12,3) DEFAULT NULL COMMENT '涨跌额',
  turnover_rate DECIMAL(8,3) DEFAULT NULL COMMENT '换手率(%)',
  adj_factor    DECIMAL(16,8) DEFAULT NULL COMMENT '复权因子',
  is_suspended  TINYINT(1)   NOT NULL DEFAULT 0 COMMENT '是否停牌',
  limit_status  VARCHAR(16)  NOT NULL DEFAULT 'NORMAL' COMMENT 'UP/DOWN/ONE_WORD_UP/ONE_WORD_DOWN/NORMAL',
  src_updated_at DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (stock_code, trade_date),
  KEY idx_date_code (trade_date, stock_code)
) {TAIL % '日线行情(不复权)'}
{year_partitions()}
""")

DDL.append(f"""
CREATE TABLE IF NOT EXISTS fact_daily_quote_adj (
  stock_code  VARCHAR(10)  NOT NULL COMMENT '证券代码',
  trade_date  DATE         NOT NULL COMMENT '交易日',
  open_adj    DECIMAL(16,4) DEFAULT NULL COMMENT '开盘价(后复权)',
  high_adj    DECIMAL(16,4) DEFAULT NULL COMMENT '最高价(后复权)',
  low_adj     DECIMAL(16,4) DEFAULT NULL COMMENT '最低价(后复权)',
  close_adj   DECIMAL(16,4) DEFAULT NULL COMMENT '收盘价(后复权)',
  pre_close_adj DECIMAL(16,4) DEFAULT NULL COMMENT '前收盘(后复权)',
  volume      BIGINT       DEFAULT NULL COMMENT '成交量(手)',
  amount      DECIMAL(20,2) DEFAULT NULL COMMENT '成交额(元)',
  PRIMARY KEY (stock_code, trade_date),
  KEY idx_date_code (trade_date, stock_code)
) {TAIL % '日线行情(后复权，供指标计算)'}
{year_partitions()}
""")

DDL.append(f"""
CREATE TABLE IF NOT EXISTS fact_financial (
  stock_code   VARCHAR(10) NOT NULL COMMENT '证券代码',
  report_period DATE       NOT NULL COMMENT '报告期',
  ann_date     DATE        NOT NULL COMMENT '公告日',
  ann_date_source VARCHAR(10) NOT NULL DEFAULT 'actual' COMMENT 'actual实际/estimated推定',
  revenue      DECIMAL(20,2) DEFAULT NULL COMMENT '营业收入(元)',
  revenue_yoy  DECIMAL(12,3) DEFAULT NULL COMMENT '营收同比(%)',
  net_profit   DECIMAL(20,2) DEFAULT NULL COMMENT '归母净利润(元)',
  net_profit_yoy DECIMAL(12,3) DEFAULT NULL COMMENT '净利润同比(%)',
  deduct_net_profit DECIMAL(20,2) DEFAULT NULL COMMENT '扣非净利润(元)',
  deduct_net_profit_yoy DECIMAL(12,3) DEFAULT NULL COMMENT '扣非净利同比(%)',
  gross_margin DECIMAL(12,3) DEFAULT NULL COMMENT '毛利率(%)',
  net_margin   DECIMAL(12,3) DEFAULT NULL COMMENT '净利率(%)',
  roe          DECIMAL(12,3) DEFAULT NULL COMMENT '净资产收益率(%)',
  roa          DECIMAL(12,3) DEFAULT NULL COMMENT '总资产收益率(%)',
  debt_ratio   DECIMAL(12,3) DEFAULT NULL COMMENT '资产负债率(%)',
  current_ratio DECIMAL(10,4) DEFAULT NULL COMMENT '流动比率',
  ocf          DECIMAL(20,2) DEFAULT NULL COMMENT '经营现金流净额(元)',
  total_asset  DECIMAL(20,2) DEFAULT NULL COMMENT '总资产(元)',
  total_equity DECIMAL(20,2) DEFAULT NULL COMMENT '归母净资产(元)',
  eps          DECIMAL(10,4) DEFAULT NULL COMMENT '每股收益',
  bps          DECIMAL(10,4) DEFAULT NULL COMMENT '每股净资产',
  ocf_ps       DECIMAL(10,4) DEFAULT NULL COMMENT '每股经营现金流',
  PRIMARY KEY (stock_code, report_period, ann_date),
  KEY idx_ann_date (ann_date),
  KEY idx_period (report_period)
) {TAIL % '财务数据'}
""")

DDL.append(f"""
CREATE TABLE IF NOT EXISTS fact_index_quote (
  index_code VARCHAR(16) NOT NULL COMMENT '指数代码',
  trade_date DATE        NOT NULL COMMENT '交易日',
  open_price DECIMAL(14,4) DEFAULT NULL,
  high_price DECIMAL(14,4) DEFAULT NULL,
  low_price  DECIMAL(14,4) DEFAULT NULL,
  close_price DECIMAL(14,4) DEFAULT NULL,
  pre_close  DECIMAL(14,4) DEFAULT NULL,
  volume     BIGINT      DEFAULT NULL COMMENT '成交量',
  amount     DECIMAL(20,2) DEFAULT NULL COMMENT '成交额',
  pct_chg    DECIMAL(8,3) DEFAULT NULL COMMENT '涨跌幅(%)',
  PRIMARY KEY (index_code, trade_date),
  KEY idx_date (trade_date)
) {TAIL % '指数日线'}
{year_partitions()}
""")

DDL.append(f"""
CREATE TABLE IF NOT EXISTS fact_indicator_daily (
  stock_code VARCHAR(10) NOT NULL COMMENT '证券代码',
  trade_date DATE        NOT NULL COMMENT '交易日',
  ma5  DECIMAL(16,4) DEFAULT NULL,
  ma10 DECIMAL(16,4) DEFAULT NULL,
  ma20 DECIMAL(16,4) DEFAULT NULL,
  ma60 DECIMAL(16,4) DEFAULT NULL,
  ma120 DECIMAL(16,4) DEFAULT NULL,
  ma250 DECIMAL(16,4) DEFAULT NULL,
  macd_dif DECIMAL(12,6) DEFAULT NULL,
  macd_dea DECIMAL(12,6) DEFAULT NULL,
  macd_bar DECIMAL(12,6) DEFAULT NULL,
  kdj_k DECIMAL(10,4) DEFAULT NULL,
  kdj_d DECIMAL(10,4) DEFAULT NULL,
  kdj_j DECIMAL(10,4) DEFAULT NULL,
  rsi6  DECIMAL(10,4) DEFAULT NULL,
  rsi12 DECIMAL(10,4) DEFAULT NULL,
  rsi24 DECIMAL(10,4) DEFAULT NULL,
  boll_up DECIMAL(16,4) DEFAULT NULL,
  boll_mid DECIMAL(16,4) DEFAULT NULL,
  boll_low DECIMAL(16,4) DEFAULT NULL,
  atr14 DECIMAL(12,4) DEFAULT NULL,
  vol_ratio DECIMAL(10,4) DEFAULT NULL COMMENT '量比',
  vol_ma5 BIGINT DEFAULT NULL COMMENT '5日均量(手)',
  pct_chg_5d DECIMAL(8,3) DEFAULT NULL,
  pct_chg_20d DECIMAL(8,3) DEFAULT NULL,
  pct_chg_60d DECIMAL(8,3) DEFAULT NULL,
  high_60d DECIMAL(12,3) DEFAULT NULL COMMENT '60日最高',
  low_60d  DECIMAL(12,3) DEFAULT NULL COMMENT '60日最低',
  rs_20d DECIMAL(8,3) DEFAULT NULL COMMENT '20日相对强度(对沪深300)',
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (stock_code, trade_date),
  KEY idx_date_code (trade_date, stock_code)
) {TAIL % '预计算技术指标'}
{year_partitions()}
""")

# ---------- 关系表 ----------
DDL.append(f"""
CREATE TABLE IF NOT EXISTS rel_stock_industry (
  stock_code    VARCHAR(10) NOT NULL,
  trade_date    DATE        NOT NULL COMMENT '快照日期',
  industry_code VARCHAR(16) NOT NULL,
  PRIMARY KEY (stock_code, trade_date),
  KEY idx_industry (industry_code, trade_date)
) {TAIL % '股票行业归属(按日快照)'}
""")

DDL.append(f"""
CREATE TABLE IF NOT EXISTS rel_stock_concept (
  concept_code VARCHAR(24) NOT NULL,
  stock_code   VARCHAR(10) NOT NULL,
  trade_date   DATE        NOT NULL COMMENT '快照日期',
  in_date      DATE        DEFAULT NULL COMMENT '纳入日期',
  out_date     DATE        DEFAULT NULL COMMENT '剔除日期',
  PRIMARY KEY (concept_code, stock_code, trade_date),
  KEY idx_stock (stock_code, trade_date)
) {TAIL % '概念成分(按日快照)'}
""")

DDL.append(f"""
CREATE TABLE IF NOT EXISTS rel_index_member (
  index_code VARCHAR(16) NOT NULL,
  stock_code VARCHAR(10) NOT NULL,
  trade_date DATE        NOT NULL COMMENT '快照日期',
  PRIMARY KEY (index_code, stock_code, trade_date),
  KEY idx_stock (stock_code, trade_date)
) {TAIL % '指数成分(按日快照)'}
""")

# ---------- 事件表 ----------
DDL.append(f"""
CREATE TABLE IF NOT EXISTS fact_event_suspend (
  stock_code  VARCHAR(10) NOT NULL,
  suspend_date DATE       NOT NULL COMMENT '停牌日',
  resume_date DATE        DEFAULT NULL COMMENT '复牌日',
  suspend_type VARCHAR(16) DEFAULT NULL COMMENT '停牌类型',
  reason      VARCHAR(255) DEFAULT NULL,
  PRIMARY KEY (stock_code, suspend_date)
) {TAIL % '停复牌事件'}
""")

DDL.append(f"""
CREATE TABLE IF NOT EXISTS fact_event_limit (
  stock_code  VARCHAR(10) NOT NULL,
  trade_date  DATE        NOT NULL,
  limit_status VARCHAR(10) NOT NULL COMMENT 'UP/DOWN/ONE_WORD_UP/ONE_WORD_DOWN',
  limit_price DECIMAL(12,3) DEFAULT NULL COMMENT '涨跌停价',
  seal_amount DECIMAL(20,2) DEFAULT NULL COMMENT '封单额',
  consecutive_days SMALLINT DEFAULT NULL COMMENT '连板数',
  PRIMARY KEY (stock_code, trade_date),
  KEY idx_date (trade_date)
) {TAIL % '涨跌停事件'}
""")

# ---------- 策略与结果 ----------
DDL.append(f"""
CREATE TABLE IF NOT EXISTS dim_strategy (
  strategy_id  BIGINT       NOT NULL AUTO_INCREMENT,
  strategy_name VARCHAR(64) NOT NULL,
  description  VARCHAR(255) DEFAULT NULL,
  condition_json JSON       NOT NULL COMMENT '条件树快照',
  filter_json  JSON         DEFAULT NULL COMMENT '基础过滤配置',
  sort_config  VARCHAR(255) DEFAULT NULL,
  version      INT          NOT NULL DEFAULT 1,
  schedule_cron VARCHAR(32) DEFAULT NULL COMMENT '定时执行cron',
  created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (strategy_id),
  UNIQUE KEY uk_name (strategy_name)
) {TAIL % '策略定义'}
""")

DDL.append(f"""
CREATE TABLE IF NOT EXISTS fact_screen_run (
  run_id       BIGINT       NOT NULL AUTO_INCREMENT,
  strategy_id  BIGINT       DEFAULT NULL,
  run_type     VARCHAR(16)  NOT NULL DEFAULT 'manual' COMMENT 'manual/schedule',
  data_date    DATE         NOT NULL COMMENT '数据截止日',
  condition_json JSON       NOT NULL COMMENT '条件树快照',
  filter_json  JSON         DEFAULT NULL COMMENT '基础过滤配置',
  candidate_cnt INT         DEFAULT NULL COMMENT '候选集数量',
  hit_cnt      INT          DEFAULT NULL COMMENT '命中数量',
  elapsed_ms   INT          DEFAULT NULL,
  created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (run_id),
  KEY idx_date (data_date),
  KEY idx_strategy (strategy_id)
) {TAIL % '筛选执行记录'}
""")

DDL.append(f"""
CREATE TABLE IF NOT EXISTS fact_screen_result (
  run_id      BIGINT      NOT NULL,
  stock_code  VARCHAR(10) NOT NULL,
  score       DECIMAL(8,2) DEFAULT NULL COMMENT '综合评分',
  hit_cnt     SMALLINT    DEFAULT NULL COMMENT '命中条件数',
  hit_detail  JSON        DEFAULT NULL COMMENT '逐条条件实际值与判定',
  rank_no     INT         DEFAULT NULL,
  PRIMARY KEY (run_id, stock_code),
  KEY idx_stock (stock_code)
) {TAIL % '筛选命中明细'}
""")

DDL.append(f"""
CREATE TABLE IF NOT EXISTS dim_watchlist (
  stock_code VARCHAR(10) NOT NULL,
  group_name VARCHAR(32) NOT NULL DEFAULT '默认',
  note       VARCHAR(255) DEFAULT NULL,
  added_at   DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (stock_code, group_name)
) {TAIL % '自选股'}
""")

# ---------- 系统表 ----------
DDL.append(f"""
CREATE TABLE IF NOT EXISTS sys_task_log (
  task_id    BIGINT       NOT NULL AUTO_INCREMENT,
  task_name  VARCHAR(64)  NOT NULL,
  task_type  VARCHAR(32)  NOT NULL COMMENT 'stock_list/calendar/daily_quote/financial/...',
  start_time DATETIME     NOT NULL,
  end_time   DATETIME     DEFAULT NULL,
  status     VARCHAR(16)  NOT NULL DEFAULT 'running' COMMENT 'running/success/failed/partial',
  total_cnt  INT          DEFAULT NULL,
  success_cnt INT         DEFAULT NULL,
  fail_cnt   INT          DEFAULT NULL,
  error_msg  VARCHAR(1000) DEFAULT NULL,
  params     VARCHAR(500) DEFAULT NULL,
  PRIMARY KEY (task_id),
  KEY idx_name_time (task_name, start_time),
  KEY idx_status (status)
) {TAIL % '任务日志'}
""")

DDL.append(f"""
CREATE TABLE IF NOT EXISTS sys_data_quality (
  issue_id   BIGINT      NOT NULL AUTO_INCREMENT,
  rule_code  VARCHAR(16) NOT NULL COMMENT 'DQ-01..DQ-07',
  stock_code VARCHAR(10) DEFAULT NULL,
  trade_date DATE        DEFAULT NULL,
  detail     VARCHAR(500) DEFAULT NULL,
  batch_tag  VARCHAR(64) DEFAULT NULL,
  created_at DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (issue_id),
  KEY idx_rule (rule_code, created_at),
  KEY idx_stock (stock_code, trade_date)
) {TAIL % '数据质量异常'}
""")


def main():
    sql_only = "--sql-only" in sys.argv
    body = ";\n".join(s.strip() for s in DDL) + ";\n"
    header = (
        "-- A股智能选股系统 表结构（自动生成，勿手改）\n"
        f"-- 共 {len(DDL)} 张表；事实表按年 RANGE 分区（2005—2027 + pmax）\n"
        "SET NAMES utf8mb4;\n\n"
    )
    os.makedirs(os.path.dirname(SQL_PATH), exist_ok=True)
    with open(SQL_PATH, "w", encoding="utf-8") as f:
        f.write(header + body)
    print(f"已生成 {SQL_PATH}（{len(DDL)} 张表）")

    if sql_only:
        return

    conn = pymysql.connect(**DB, autocommit=True)
    created, existed = 0, 0
    try:
        with conn.cursor() as cur:
            for stmt in DDL:
                table = stmt.split("CREATE TABLE IF NOT EXISTS")[1].split("(")[0].strip()
                cur.execute("SHOW TABLES LIKE %s", (table,))
                if cur.fetchone():
                    existed += 1
                else:
                    created += 1
                cur.execute(stmt)
            cur.execute("SHOW TABLES")
            tables = sorted(r[0] for r in cur.fetchall())
        print(f"建表完成：新建 {created} 张，已存在 {existed} 张，当前共 {len(tables)} 张")
        print(" | ".join(tables))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
