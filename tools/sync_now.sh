#!/bin/bash
# 服务器台账 → 本地 同步（导出全量 + 合并 + 导 key 清单）
# 用法: bash tools/sync_now.sh    （sync_daemon.py 每 180s 调一次）
#
# 🔴 2026-10-14：原来拉 `ledger/latest.json` 是错的 —— 那是"本批结果"文件
#    （见 src/ledger.py 说明），可能只有 1 条。正解：让服务器先把全量合并
#    台账导出到 `ledger/export_all.json`，再拉那个。
cd "$(dirname "$0")/.." || exit 1
TMP="$(cygpath -u "$TEMP")/srv_export_all.json"

SSHX="./tools/sshx.sh"
REMOTE="cd /app/working/intern-register && .venv/bin/python tools/ops/export_ledger.py"
# 🔴 2026-10-16：scp 也要显式 ProxyCommand（裸 ssh config 的 ProxyCommand
#    大量 "Connection closed by UNKNOWN port 65535"，见 tools/sshx.sh）。
SCP_OPTS=(-o StrictHostKeyChecking=no -o ConnectTimeout=30
          -o "ProxyCommand=cloudflared access ssh --hostname qwenpaw-tom.xzxyuan.ccwu.cc --log-level info")

# 1) 让服务器导出全量
exported=0
for i in 1 2 3; do
  if "$SSHX" "$REMOTE" 6 2>&1 | grep -q "EXPORT_OK"; then exported=1; break; fi
  sleep 3
done
if [ "$exported" -ne 1 ]; then
  echo "SYNC_FAIL 服务器导出 export_all.json 失败"
  exit 1
fi

# 2) 拉取导出文件
ok=0
for i in 1 2 3 4 5; do
  scp "${SCP_OPTS[@]}" \
      qwenpaw-tom:/app/working/intern-register/ledger/export_all.json "$TMP" \
      2>&1 | grep -aviE "permanently|Warning"
  if [ "${PIPESTATUS[0]}" -eq 0 ]; then ok=1; break; fi
  sleep 4
done
if [ "$ok" -ne 1 ]; then
  echo "SYNC_FAIL scp 5 次重试后仍失败"
  exit 1
fi

# 3) 合并进本地台账 + 导 key 清单
exec .venv/Scripts/python.exe tools/sync_ledger_from_server.py "$(cygpath -w "$TMP")"
