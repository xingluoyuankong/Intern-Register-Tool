#!/bin/bash
# 容器重建后恢复 Intern-Register 业务进程（regloop / 看板 / irguard）。
#
# 🔴 背景：QwenPaw 容器的 /etc 在 overlay 上，**容器重建即重置**；
#    平台自带 supervisord 的配置又由 /entrypoint.sh 从模板重新生成 ——
#    我们加的业务程序会随容器重建消失（2026-10-16 实测：regloop/看板全停，
#    隧道还在但 8000 没人听，面板 404）。
#    本脚本从 NAS 上的权威副本恢复，并起**独立 supervisord 实例**。
#
# 用法（服务器上）： bash /app/working/intern-register/deploy/supervisor/restore.sh
set -e
SRC=/app/working/intern-register/deploy/supervisor
CONF=/etc/supervisor/conf.d
SOCK=/var/run/supervisor-ir.sock

echo "[1/4] 拷贝配置到 /etc"
mkdir -p "$CONF"
cp -f "$SRC/ir-services.conf" "$CONF/ir-services.conf"
cp -f "$SRC/supervisord-ir.conf" /etc/supervisord-ir.conf

echo "[2/4] 停掉旧实例（若有）"
if [ -S "$SOCK" ]; then
  supervisorctl -c /etc/supervisord-ir.conf stop all >/dev/null 2>&1 || true
  supervisorctl -c /etc/supervisord-ir.conf shutdown >/dev/null 2>&1 || true
fi
pkill -f "supervisord -c /etc/supervisord-ir.conf" 2>/dev/null || true
sleep 2

echo "[3/4] 起独立 supervisord"
/usr/bin/supervisord -c /etc/supervisord-ir.conf
sleep 8

echo "[4/4] 状态"
supervisorctl -c /etc/supervisord-ir.conf status

echo
echo "看板:  curl -s http://127.0.0.1:8000/healthz   （期望 ok）"
echo "注册:  tail -f /app/working/intern-register/logs/loop.log"
