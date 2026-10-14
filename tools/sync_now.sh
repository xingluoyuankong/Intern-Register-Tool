#!/bin/bash
# 服务器台账 → 本地 同步（拉 latest.json + 合并 + 导 key 清单）
# 用法: bash tools/sync_now.sh    （sync_daemon.py 每 180s 调一次）
cd "$(dirname "$0")/.." || exit 1
TMP="$(cygpath -u "$TEMP")/srv_latest.json"
ok=0
for i in 1 2 3 4 5; do
  # scp 输出过滤噪音后写日志；管道整体成败看 scp 本身（PIPESTATUS）
  scp -o StrictHostKeyChecking=no -o ConnectTimeout=30 \
      qwenpaw-tom:/app/working/intern-register/ledger/latest.json "$TMP" \
      2>&1 | grep -aviE "permanently|Warning"
  if [ "${PIPESTATUS[0]}" -eq 0 ]; then ok=1; break; fi
  sleep 4
done
if [ "$ok" -ne 1 ]; then
  echo "SYNC_FAIL scp 5 次重试后仍失败"
  exit 1
fi
exec .venv/Scripts/python.exe tools/sync_ledger_from_server.py "$(cygpath -w "$TMP")"
