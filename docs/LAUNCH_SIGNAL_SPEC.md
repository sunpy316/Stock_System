# 新增需求：A股「靠谱启动信号」个股与 ETF 筛选任务接入方案

> 来源对话：[Stock](conversation://4594fc00-8eb1-491f-b5dd-0634c5b7b1b1)（2026-11-27 策略讨论与实时数据验证）
> 目标项目：`D:\Stocks\Antigravity\stocksystem`（后端 FastAPI + MySQL + APScheduler，前端 Web / Android / iOS）

---

## 一、需求目标

将经过实盘数据验证的**「缩量蓄势 + 放量突破 / 口袋支点 + 趋势与防骗线过滤」**右侧启动信号策略正式接入 `Stock_System`，覆盖**个股**与**高流动性 ETF**两类标的，并支持：
1. 在策略模板中直接一键筛选（兼容现有 `TEMPLATES` 与 `APScheduler` 定时跑批）。
2. 新增 ETF 启动信号扫描能力（接口 + 定时任务 + 前端展示）。
3. 附带明确的**关键压力位 / 防守止损位 / 防追高提示**。

---

## 二、模块改造清单（对齐现有代码架构）

### 1. 指标层扩展（`stock_system/indicators/calc.py` 与 `fact_indicator_daily`）
在现有技术指标（`ma5/10/20/60`, `boll_up/mid/low`, `vol_ratio`, `high_60d`, `rs_20d` 等）基础上，补充 6 个核心启动信号指标（均为前复权序列计算）：

| 字段名 | 中文名 | 计算口径 | 用途 |
| :--- | :--- | :--- | :--- |
| `vol_ratio_20` | 20日量比 | `volume / mean(volume[-21:-1])` | 识别相对中期均量的真实放量 |
| `boll_width_20` | 布林带带宽% | `(boll_up - boll_low) / boll_mid * 100`（前1日或当日） | 识别低波动收敛蓄势 |
| `consol_range_20` | 近20日振幅% | `(max(high[-21:-1]) - min(low[-21:-1])) / min(low[-21:-1]) * 100` | 判断平台蓄势是否紧凑 |
| `high_20d_prev` | 前20日最高价 | `max(high[-21:-1])` | 判定收盘是否有效突破 20 日平台 |
| `pocket_pivot` | 口袋支点 | `pct_chg_adj > 0` 且 `volume > max(近10日所有下跌日成交量)`（0/1） | 识别平台内部提前启动的机构买盘 |
| `upper_shadow_ratio` | 上影线占比 | `(high - max(open, close)) / (high - low)` | 过滤 A 股常见的早盘急拉、尾盘回落钓鱼线 |
| `bias_ma20` | 20日乖离率% | `(close / ma20 - 1) * 100` | 防止过度追高（区分稳健启动 vs 高位透支） |

> 同步更新 `stock_system/screener/templates.py` 中的 `INDICATOR_META`，使前端自定义条件选股也可直接选用以上字段。

---

### 2. 个股策略模板新增（`stock_system/screener/templates.py`）
在现有 `T01`–`T09` 后新增两个高胜率右侧启动模板（并可直接在 `dim_strategy` 中配置收盘后定时任务）：

#### `T10`：缩量蓄势 · 口袋支点启动（稳健型，位置克制、盈亏比优）
* **基础风控过滤**：非 ST、非停牌、总市值 ≥ 100 亿（可配 ≥ 50 亿）、当日成交额 ≥ 3 亿（可配 ≥ 1 亿）、`pe_ttm > 0`。
* **核心条件**：
  * `close_price > ma20` 且 `close_price > ma60`（站稳双均线）
  * `pocket_pivot == 1`（触发口袋支点）
  * `vol_ratio >= 1.4` 且 `vol_ratio_20 >= 1.3`（放量确认）
  * `consol_range_20 <= 25`（或 `boll_width_20 <= 18`，蓄势紧凑）
  * `upper_shadow_ratio <= 0.30`（实体饱满，剔除长上影诱多）
  * `bias_ma20 <= 12` 且 `pct_chg_adj` 介于 `3% ~ 11%`（刚启动未过热）

#### `T11`：主线核心 · 放量平台突破（动量趋势型，适合右侧二买/回踩跟踪）
* **基础风控过滤**：非 ST、总市值 ≥ 100 亿、当日成交额 ≥ 5 亿。
* **核心条件**：
  * `close_price > ma20` 且 `close_price > ma60`
  * `close_price > high_20d_prev`（或 `close_price >= high_60d`，有效突破平台高点）
  * `vol_ratio >= 1.5` 且 `vol_ratio_20 >= 1.4`
  * `upper_shadow_ratio <= 0.25`
  * `pct_chg_20d > 10`（强弱领先 / RPS 居前）
* **风控提示标注**：当 `bias_ma20 > 12%` 时，在返回结果中自动打标签 `偏离较大-宜等回踩`，并附带 `突破参考位 (high_20d_prev / high_60d)` 与 `防守止损位 (ma20 / 当日最低价)`。

---

### 3. ETF 启动信号扫描任务（新增轻量模块）
当前系统以个股为主，建议新增独立的 ETF 启动扫描模块（如 `stock_system/screener/etf_launch.py`），避免侵入现有 22 张个股核心表：

* **标的范围**：沪深 A 股权益类 ETF，过滤掉货币、债券、可转债、商品期货及海外 QDII ETF；要求日成交额 ≥ 1.5 亿元。
* **数据源**：沿用东财公开行情/日线接口（`push2.eastmoney.com` / `akshare`），仅拉取流动性达标的 ~180 只 ETF 近 100 个交易日日线，全量扫描耗时约 10–20 秒。
* **分层判定**：
  * **Tier 1（主升突破）**：站稳 MA20 & MA60 + 突破 20/60 日高点或触发口袋支点 + `vol_ratio >= 1.4` + `upper_shadow_ratio <= 0.35`。
  * **Tier 2（右侧转折/口袋支点）**：站上 MA20 + 触发口袋支点或突破 20 日高点 + `vol_ratio >= 1.4` + `upper_shadow_ratio <= 0.35`（标明距 60 日线压力位空间）。
* **落库与调度**：
  * 新增一张轻表 `fact_etf_launch_signal`（或缓存 JSON 按 `trade_date` 存储）。
  * 接入 `api/main.py` 的 `APScheduler`，每个交易日 **18:15**（在个股每日更新 18:00 之后）自动执行一次，同时提供手动触发/查询 API：
    * `GET /api/etf/launch-signals?trade_date=...`
    * `POST /api/etf/launch-signals/run`

---

### 4. 前端与移动端展示（`stock_system/web/index.html`）
1. 选股模板列表自动展示 `T10 缩量蓄势·口袋支点` 与 `T11 放量平台突破`。
2. 增加「ETF 启动」入口/Tab，展示当日命中的 ETF 分层名单、成交额、量比、20日振幅、突破位与防守位（MA20 / 阳线低点），并对高乖离标的展示「宜等回踩」风险标签。
3. 完成后按现有流程同步至云服务器 `47.104.78.144`、GitHub 仓库，并通过 `deploy/windows/sync_mobile.ps1` 更新 Android APK / iOS IPA。

---

## 三、已验证的原型参考
本方案的核心筛选算法已在 2026-11-27 真实收盘数据上验证通过，参考原型脚本位于：
`C:\Users\Sunpy\.gemini\antigravity\brain\4594fc00-8eb1-491f-b5dd-0634c5b7b1b1\scratch\scan_signals.py`
