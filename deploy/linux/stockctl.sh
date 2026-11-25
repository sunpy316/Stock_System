#!/usr/bin/env bash
# A股智能选股系统 运维脚本（Linux 传统部署）
# 封装 启动 / 停止 / 状态 / 重启，兼容 systemd 与无 systemd 环境（容器、精简系统）。
#
# 用法：
#   ./stockctl.sh start     启动后端 + nginx
#   ./stockctl.sh stop      停止
#   ./stockctl.sh restart   重启
#   ./stockctl.sh status    查看状态
#
# 说明：若系统无 systemd，后端以 nohup 方式后台运行，PID 写入 /run/stock-system.pid。

APP_DIR="/opt/stocksystem"
PY="$APP_DIR/venv/bin/python"
PID_FILE="/run/stock-system.pid"
HOST="127.0.0.1"
PORT="8800"
PROC_PATTERN="uvicorn stock_system.api.main:app"

use_systemd() {
    command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet systemd 2>/dev/null
}

start() {
    if use_systemd; then
        systemctl start stock-system || true
        systemctl start nginx || true
    else
        if pgrep -f "$PROC_PATTERN" >/dev/null 2>&1; then
            echo "后端已在运行 (PID $(pgrep -f "$PROC_PATTERN" | head -1))"
        else
            nohup "$PY" -m uvicorn stock_system.api.main:app --host "$HOST" --port "$PORT" \
                >/var/log/stock-system.log 2>&1 &
            echo $! > "$PID_FILE"
            echo "后端已启动，PID=$(cat "$PID_FILE")，日志：/var/log/stock-system.log"
        fi
        if command -v nginx >/dev/null 2>&1; then
            if nginx -t >/dev/null 2>&1; then
                if pgrep -x nginx >/dev/null 2>&1; then nginx -s reload; else nginx; fi
            else
                echo "[警告] nginx 配置测试未通过，未启动/重载 nginx，请检查配置"
            fi
        fi
    fi
    echo "完成。浏览器访问 http://<本机IP>/"
}

stop() {
    if use_systemd; then
        systemctl stop stock-system || true
        systemctl stop nginx || true
    else
        if [ -f "$PID_FILE" ]; then
            kill "$(cat "$PID_FILE")" 2>/dev/null || true
            rm -f "$PID_FILE"
        fi
        pkill -f "$PROC_PATTERN" 2>/dev/null || true
        pkill -x nginx 2>/dev/null || true
    fi
    echo "已停止"
}

status() {
    if use_systemd; then
        systemctl status stock-system --no-pager 2>/dev/null || echo "后端未运行"
        systemctl status nginx --no-pager 2>/dev/null || echo "nginx 未运行"
    else
        if pgrep -af "$PROC_PATTERN" >/dev/null 2>&1; then
            echo "后端运行中："; pgrep -af "$PROC_PATTERN"
        else
            echo "后端未运行"
        fi
        if pgrep -x nginx >/dev/null 2>&1; then echo "nginx 运行中"; else echo "nginx 未运行"; fi
    fi
}

case "${1:-}" in
    start)   start ;;
    stop)    stop ;;
    restart) stop; sleep 1; start ;;
    status)  status ;;
    *) echo "用法: $0 {start|stop|status|restart}"; exit 1 ;;
esac
