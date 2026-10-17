#!/bin/bash
# 容器重建后恢复 Intern-Register（regloop / 看板 / irguard / 隧道）。
#
# 🔴 背景：QwenPaw 容器的 `/etc` 与 `/root` 在 **overlay** 上，**容器重建即重置**；
#    平台自带 supervisord 的配置又由 /entrypoint.sh 从模板重新生成 ——
#    我们加的业务程序会随容器重建消失。`/root/.cloudflared/config-new.yml`
#    里的 `ir-qwenpaw-tom → :8000` ingress 同理丢失 → 公网面板 404。
#    本脚本从 NAS 上的权威副本恢复，并起**独立 supervisord 实例**。
#
# 用法（服务器上）： bash /app/working/intern-register/deploy/supervisor/restore.sh
set -e
ROOT=/app/working/intern-register
SRC=$ROOT/deploy
CONF=/etc/supervisor/conf.d
SOCK=/var/run/supervisor-ir.sock

echo "[1/5] 恢复 cloudflared 配置到 /root/.cloudflared"
mkdir -p /root/.cloudflared
cp -f "$SRC/cloudflared/config-new.yml" /root/.cloudflared/config-new.yml
# 隧道凭据 json（本地私有，不入库）——只在缺失时从 NAS 副本补
for j in "$SRC"/cloudflared/*.json; do
  [ -f "$j" ] || continue
  [ -f "/root/.cloudflared/$(basename "$j")" ] || cp -f "$j" /root/.cloudflared/
done

echo "[2/5] 拷 supervisor 配置到 /etc"
mkdir -p "$CONF"
cp -f "$SRC/supervisor/ir-services.conf" "$CONF/ir-services.conf"
cp -f "$SRC/supervisor/supervisord-ir.conf" /etc/supervisord-ir.conf

echo "[3/5] 停旧实例 + 清裸 cloudflared（避免旧 ingress 分走流量）"
if [ -S "$SOCK" ]; then
  supervisorctl -c /etc/supervisord-ir.conf stop all >/dev/null 2>&1 || true
  supervisorctl -c /etc/supervisord-ir.conf shutdown >/dev/null 2>&1 || true
fi
pkill -f "supervisord -c /etc/supervisord-ir.conf" 2>/dev/null || true
sleep 1
pkill -f "cloudflared .*tunnel run" 2>/dev/null || true
sleep 2

echo "[4/5] 起独立 supervisord"
/usr/bin/supervisord -c /etc/supervisord-ir.conf
sleep 10

echo "[5/5] 状态"
supervisorctl -c /etc/supervisord-ir.conf status

echo
echo "本地看板:  curl -s http://127.0.0.1:8000/healthz      （期望 ok）"
echo "公网看板:  https://ir-qwenpaw-tom.xzxyuan.ccwu.cc/    （隧道约 30s 内生效）"
echo "注册日志:  tail -f $ROOT/logs/loop.log"
