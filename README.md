# A股智能选股系统 · 数据底座（M1）

对应《A股智能选股系统需求说明书》V1.2 的 M1 交付内容：数据模型、采集适配层、调度任务、质量校验。

## 一、目录结构

```
stock_system/
├── config.py                   # 数据库与采集参数
├── db.py                       # 连接池、批量 upsert、任务日志
├── datasource/
│   ├── base.py                 # 数据源抽象接口（换 Tushare 只需新增实现）
│   └── akshare_source.py       # akshare 实现：限频、重试退避、字段标准化
├── collectors/
│   ├── stock_list.py           # 股票列表（沪主板/科创板/深主板/创业板/北交所）
│   ├── calendar.py             # 交易日历
│   ├── daily_quote.py          # 日线（不复权 + 复权 + 涨跌停判定）
│   ├── financial.py            # 财务摘要（宽表转长表 + 公告日推定）
│   └── stock_info.py           # 个股概况补采
├── quality/checks.py           # DQ-01 ~ DQ-07 校验
├── scripts/
│   ├── build_schema.py         # 生成并执行 22 张表
│   ├── sync.py                 # 采集任务 CLI
│   └── probe_akshare.py        # 接口连通性探测
├── sql/01_schema.sql           # 建表脚本（自动生成）
└── logs/
```

## 二、环境

- Python 3.13 + pandas 3.1、akshare 1.20.22、PyMySQL 2.2.8、SQLAlchemy 2.1.6
- MySQL 8.0.29，库 `stock_system`

## 三、使用

```bash
# 建表（22 张，幂等）
python stock_system/scripts/build_schema.py

# 股票列表 + 交易日历
python -m stock_system.scripts.sync --task base

# 日线（首次全量约 60 分钟；增量自动从最大交易日续跑）
python -m stock_system.scripts.sync --task quote

# 财务（首次全量约 100 分钟）
python -m stock_system.scripts.sync --task financial

# 质量校验
python -m stock_system.scripts.sync --task check --date 2026-11-02
```

调试参数：`--limit 30` 限制股票数，`--codes 600519,000001` 指定标的，`--start 20240101` 指定起始日。

## 四、已验证结果（2026-11-02）

| 项目   | 结果                                                 |
| ---- | -------------------------------------------------- |
| 表结构  | 22 张，其中 4 张事实表按年 RANGE 分区（2005—2027 + pmax）        |
| 股票列表 | 5577 只：主板 3194、创业板 1411、科创板 618、北交所 354            |
| 交易日历 | 8797 个交易日（1990-12-19 起）                            |
| 日线   | 30 只 × 2024-01-02 至 2026-11-02，20513 行，两表各 20513 行 |
| 财务   | 30 只 × 3560 行，最早报告期 1989-12-31                     |
| 数据质量 | DQ-01/02/03/06 均为 0 异常；重复执行幂等                      |

## 五、三个必须知道的数据口径问题

### 1. 复权用前复权序列，不用后复权

东财返回的 hfq 序列不是"原始价 × 固定因子"，其单位因子逐日漂移，日涨跌幅与原始价对不上（实测 0.87% vs 0.66%），长周期累积失真。qfq 序列满足 `qfq_t = raw_t × (F_t / F_T)`，等价于归一化到最新日的后复权序列，日收益率与真实收益一致。

验证：000034 于 2026-05-19 实施 10 转 4 派 0.73，原始价从 41.57 跌到 30.96（显示 -25.52%），复权后实际涨幅 **+4.45%**，与除权参考价 29.64 的计算结果一致。

**限制**：qfq 会随新的除权整体平移，除权后需对该股全历史重算（M2 实现除权检测）。

### 2. 除权日的原始涨跌幅不可用

`fact_daily_quote.pct_chg` 是东财返回的原始价涨跌幅，除权日会显示成暴跌（如 -25.52%），不是真实亏损。因此表里另存 `pct_chg_adj`（复权涨跌幅，除权日亦正确）。

**筛选引擎的单日涨幅条件必须用 `pct_chg_adj`，不能用 `pct_chg`。**

### 3. 财务数据全部是推定公告日

akshare 财务摘要只返回报告期、不返回公告日，因此按法定披露截止日推定：  
一季报 4/30、半年报 8/31、三季报 10/31、年报次年 4/30。全部标 `ann_date_source='estimated'`。

`as-of` 切片因此是保守的（永远不早于真实公告日），不会产生前视偏差，但会略微滞后。

## 六、已知限制

1. 北交所列表接口（[www.bse.cn）偶发长时间不响应，已加](http://www.bse.cn）偶发长时间不响应，已加) 25 秒超时保护，超时则跳过并保留上次数据。
2. `total_asset`（总资产）无对应指标，暂为空；`deduct_net_profit_yoy` 由系统按同期反算。
3. 极端比率（净利趋零导致的同比天文数字）超过 10 万% 时置空。
4. 停牌股当前不生成行情记录，`fact_event_suspend` 尚未采集。
5. 指数行情、板块成分、预计算指标（`fact_indicator_daily`）留待 M2。
