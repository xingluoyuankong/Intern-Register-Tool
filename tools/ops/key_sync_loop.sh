#!/bin/bash
# 每小时把台账 key 同步到 GitHub 私库（supervisor 程序 key-sync 的宿主循环）。
# 用法：由 ir-supervisord 常驻拉起；手动：bash tools/ops/key_sync_loop.sh
ROOT=/app/working/intern-register
cd "$ROOT" || exit 1
mkdir -p logs
while true; do
  .venv/bin/python -u tools/ops/key_sync.py >> logs/key_sync.log 2>&1
  sleep "${KEY_SYNC_INTERVAL:-3600}"
done
