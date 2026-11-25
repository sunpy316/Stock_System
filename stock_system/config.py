# -*- coding: utf-8 -*-
"""全局配置。生产环境请改为读取环境变量或 .env。"""
import os

DB_HOST = os.getenv("STOCK_DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("STOCK_DB_PORT", "3306"))
DB_USER = os.getenv("STOCK_DB_USER", "stock_app")
# 用 `or` 而非 getenv 的默认值：环境变量被设成空串时，getenv 会返回 ""，
# 导致应用拿空密码去连库（MySQL 报 using password: NO）。空值一律回退默认。
DB_PASSWORD = os.getenv("STOCK_DB_PASSWORD") or "StockSys_2026"
DB_NAME = os.getenv("STOCK_DB_NAME", "stock_system")

DB_URL = (
    f"mysql+pymysql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    "?charset=utf8mb4"
)

# 采集参数
# 0.3s ≈ 200 次/分钟，实测会触发东方财富 push2 的 IP 限流（对方经验安全区在几十次/分）。
# 1.2s ≈ 50 次/分钟；全市场一轮（5577 只 × 2 次）约 3.7 小时。
REQUEST_INTERVAL = float(os.getenv("STOCK_REQ_INTERVAL", "1.2"))  # 单请求最小间隔（秒）
# 行情源：em=东方财富 push2（默认），tx=腾讯财经。
# 东财按 IP 限流、封禁时长不可控；腾讯前复权价实测与东财逐位一致，可作替代。
QUOTE_PROVIDER = os.getenv("STOCK_QUOTE_PROVIDER", "em")
MAX_RETRY = int(os.getenv("STOCK_MAX_RETRY", "3"))
# 连续失败多少只判定为"数据源整体不可用"（非东财专属限流，如腾讯/网络层拒绝），触发熔断
MAX_CONSEC_FAIL = int(os.getenv("STOCK_MAX_CONSEC_FAIL", "20"))
RETRY_BACKOFF = 1.0  # 退避基数：1s、2s、4s
BATCH_SIZE = int(os.getenv("STOCK_BATCH_SIZE", "2000"))

# 当日行情生成时间闸：A 股 15:00 收盘，数据源通常 15:10 之后才有当日 K 线。
# 早于这个时间点强制同步"当日"只会白跑几千次请求，还会撞限流。
DAILY_SYNC_READY_HOUR = int(os.getenv("STOCK_DAILY_READY_HOUR", "15"))
DAILY_SYNC_READY_MINUTE = int(os.getenv("STOCK_DAILY_READY_MINUTE", "10"))

# 数据保留与历史窗口
# 日线默认起始日：库里没有数据且未指定 start 时用这个。
# 设为 1990 是为了拿到完整 A 股历史（上交所 1990-12-19 开市）。
HISTORY_START = os.getenv("STOCK_HISTORY_START", "19900101")
INDICATOR_WINDOW_YEARS = 3

# 各板块涨跌幅限制（百分比），用于涨跌停判定；随交易所规则调整
LIMIT_RULES = {
    "主板": 10.0,
    "创业板": 20.0,
    "科创板": 20.0,
    "北交所": 30.0,
}
ST_LIMIT = 5.0
LIMIT_TOLERANCE = 0.5

# 安全与跨域配置（公网部署建议设置 STOCK_AUTH_ENABLED=true 并修改密码/密钥）
AUTH_ENABLED = os.getenv("STOCK_AUTH_ENABLED", "false").lower() in ("1", "true", "yes")
AUTH_USERNAME = os.getenv("STOCK_AUTH_USERNAME", "admin")
AUTH_PASSWORD = os.getenv("STOCK_AUTH_PASSWORD") or "StockSys_Admin_2026"
AUTH_SECRET = os.getenv("STOCK_AUTH_SECRET") or "change_this_hmac_secret_in_prod"
AUTH_EXPIRE_SECONDS = int(os.getenv("STOCK_AUTH_EXPIRE_SECONDS", "86400"))
CORS_ORIGINS = [
    o.strip()
    for o in os.getenv("STOCK_CORS_ORIGINS", "*").split(",")
    if o.strip()
]
