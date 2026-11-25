#!/bin/bash
# 服务器端诊断脚本：由 诊断服务器.bat 通过 ssh "bash -s" 送入执行，只读，不改任何数据
APP=/opt/stocksystem
PY=$APP/venv/bin/python

hr() { echo; echo "===== $* ====="; }

hr "0 基本信息"
date "+%F %T %Z"
hostname
uptime
uname -r

hr "1 内存"
free -m

hr "2 磁盘"
df -h / /opt 2>/dev/null | awk 'NR==1||!seen[$0]++'

hr "3 服务状态"
systemctl is-active stock-system 2>&1
systemctl status stock-system --no-pager -l 2>&1 | sed -n '1,22p'

hr "4 代码版本探测"
echo "[runner.py 是否含新版分批取数]"
grep -c "_iter_groups" $APP/stock_system/indicators/runner.py 2>&1
echo "[runner.py chunksize/batch]"
grep -n "chunksize=\|batch=" $APP/stock_system/indicators/runner.py 2>&1 | head -5
echo "[calc.py 是否含 inf 兜底]"
grep -n "isinf\|np.inf" $APP/stock_system/indicators/calc.py 2>&1 | head -5
echo "[indicators 目录文件时间]"
ls -l --time-style=long-iso $APP/stock_system/indicators/ 2>&1 | tail -8

hr "5 错误日志过滤"
journalctl -u stock-system -n 500 --no-pager 2>&1 \
  | grep -iE "error|exception|traceback|inf can not|killed|memory|fail" \
  | tail -40

hr "6 日志尾部 60 行"
journalctl -u stock-system -n 60 --no-pager 2>&1

hr "7 OOM 检查"
dmesg -T 2>/dev/null | grep -iE "killed process|out of memory|oom-kill" | tail -10 \
  || echo "(无 dmesg 权限或无记录)"

hr "8 最近 12 条任务日志"
ENVF=/etc/stock-system.env
if [ -f "$ENVF" ]; then set -a; . "$ENVF"; set +a; fi
MYSQL_PWD="${STOCK_DB_PASSWORD}" mysql \
  -u"${STOCK_DB_USER:-root}" \
  -h"${STOCK_DB_HOST:-127.0.0.1}" \
  -P"${STOCK_DB_PORT:-3306}" \
  "${STOCK_DB_NAME:-stock_system}" --table -e "
SELECT task_id, task_name, start_time,
       TIMESTAMPDIFF(SECOND, start_time, IFNULL(end_time,NOW())) AS sec,
       status, total_cnt, success_cnt, fail_cnt,
       LEFT(REPLACE(IFNULL(error_msg,''),'\n',' '),150) AS err
FROM sys_task_log
ORDER BY task_id DESC LIMIT 12;" 2>&1

hr "9 数据量核对"
MYSQL_PWD="${STOCK_DB_PASSWORD}" mysql \
  -u"${STOCK_DB_USER:-root}" \
  -h"${STOCK_DB_HOST:-127.0.0.1}" \
  -P"${STOCK_DB_PORT:-3306}" \
  "${STOCK_DB_NAME:-stock_system}" --table -e "
SELECT 'fact_daily_quote' t, COUNT(*) rows_cnt FROM fact_daily_quote
UNION ALL SELECT 'fact_indicator_daily', COUNT(*) FROM fact_indicator_daily
UNION ALL SELECT 'adj_close<=0', COUNT(*) FROM fact_daily_quote_adj WHERE close_adj<=0
UNION ALL SELECT 'ind_ma20_null', COUNT(*) FROM fact_indicator_daily WHERE ma20 IS NULL;" 2>&1

hr "10 MySQL 错误日志"
for f in /var/log/mysql/error.log /var/log/mysql/mysql-error.log /var/log/mysqld.log; do
  if [ -f "$f" ]; then
    echo "-- $f --"
    tail -20 "$f" 2>&1
    break
  fi
done

hr "DONE"
echo "诊断采集结束"
