#!/usr/bin/env bash
# A股选股系统 · 服务器端更新脚本
#
# 用法（在服务器上）：
#   sudo bash /opt/stocksystem/deploy/linux/update.sh /tmp/update.tar.gz
#
# 更新包由 Windows 侧打包，内含（相对项目根）：
#   stock_system/   后端 + 前端
#   deploy/         部署脚本与文档（可选）
#
# 流程：备份 -> 解包覆盖 -> 依赖有变才装 -> 重启 -> 健康检查（失败自动回滚）
set -euo pipefail

PKG="${1:?用法: $0 <update.tar.gz>}"
APP_DIR="${APP_DIR:-/opt/stocksystem}"
SERVICE="${SERVICE:-stock-system}"
SVC_USER="${SVC_USER:-stock}"
STAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP="$APP_DIR/.backup_$STAMP"

[[ -f "$PKG" ]] || { echo "[!] 找不到更新包：$PKG"; exit 1; }
[[ -d "$APP_DIR/stock_system" ]] || { echo "[!] 目录不存在：$APP_DIR/stock_system"; exit 1; }

echo "[1/5] 备份当前版本 -> $BACKUP"
mkdir -p "$BACKUP"
cp -a "$APP_DIR/stock_system" "$BACKUP/stock_system"
if [[ -f "$APP_DIR/requirements.txt" ]]; then
    cp -a "$APP_DIR/requirements.txt" "$BACKUP/"
fi
if [[ -d "$APP_DIR/deploy" ]]; then
    cp -a "$APP_DIR/deploy" "$BACKUP/deploy"
fi

echo "[2/5] 解包覆盖"
tar -xzf "$PKG" -C "$APP_DIR"

echo "[3/5] 依赖检查"
REQ="$APP_DIR/requirements.txt"
BK_REQ="$BACKUP/requirements.txt"
if [[ ! -f "$REQ" && -f "$APP_DIR/deploy/linux/requirements.txt" ]]; then
    REQ="$APP_DIR/deploy/linux/requirements.txt"
    BK_REQ="$BACKUP/deploy/linux/requirements.txt"
fi
if [[ -f "$REQ" ]]; then
    if [[ -f "$BK_REQ" ]] && diff -q "$REQ" "$BK_REQ" >/dev/null 2>&1; then
        echo "  requirements 未变化，跳过 pip install"
    else
        echo "  依赖有变化，安装：$REQ"
        "$APP_DIR/venv/bin/pip" install -q -r "$REQ"
    fi
else
    echo "  未找到 requirements.txt，跳过"
fi

echo "[4/5] 权限与重启"
chown -R "$SVC_USER:$SVC_USER" "$APP_DIR" 2>/dev/null || true
systemctl restart "$SERVICE"

# 后端 import pandas/akshare 较慢，冷启动常需 10~30 秒。
# 这里最多等 90 秒（30 次 × 3 秒），避免"还在启动"被误判成失败而回滚。
echo "[5/5] 健康检查（最多等 90 秒）"
ok=0
for i in $(seq 1 30); do
    if curl -fsS --max-time 5 http://127.0.0.1:8800/health >/dev/null 2>&1; then
        ok=1
        echo "    第 $i 次探测成功"
        break
    fi
    sleep 3
done

if [ "$ok" = "1" ]; then
    echo "[+] 更新成功：$(curl -s http://127.0.0.1:8800/health)"
    echo "    备份保留在：$BACKUP"
else
    echo "[!] 等待 90 秒仍无响应，判定失败，开始回滚 ..."
    echo "    服务状态：$(systemctl is-active "$SERVICE" 2>/dev/null || echo unknown)"
    echo "    ---- 最近日志 ----"
    journalctl -u "$SERVICE" -n 20 --no-pager 2>/dev/null || true
    echo "    -----------------"
    systemctl stop "$SERVICE" || true
    rm -rf "$APP_DIR/stock_system"
    cp -a "$BACKUP/stock_system" "$APP_DIR/stock_system"
    systemctl start "$SERVICE" || true
    echo "[!] 已回滚。请查看：journalctl -u $SERVICE -n 50 --no-pager"
    exit 1
fi
