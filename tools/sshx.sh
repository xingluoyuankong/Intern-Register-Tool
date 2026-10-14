#!/bin/bash
# 带重试的远程命令执行器。吃掉 SSH 抖动（Broken pipe / UNKNOWN port 65535）。
# 用法: ./sshx.sh '远程命令'
# 输出只保留 stdout；失败重试最多 N 次。
CMD="$1"
N="${2:-6}"
GREP_NOISE='permanently|Warning: Permanently|REMOTE HOST|WARNING:|fingerprint|Offending|Add correct|IT IS POSSIBLE|Someone could|Keyboard-interactive|UpdateHostkeys|@@@'
for i in $(seq 1 "$N"); do
  out=$(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 -o ServerAliveInterval=10 \
        -o ServerAliveCountMax=2 qwenpaw-tom "$CMD" 2>&1 | grep -avE "$GREP_NOISE")
  rc=$?
  if [ -n "$out" ] && ! echo "$out" | grep -qE "Connection closed by UNKNOWN|Broken pipe|Connection reset"; then
    echo "$out"
    exit 0
  fi
  sleep 5
done
echo "[SSHX_FAIL] 6 次重试后仍失败: ${CMD:0:80}"
exit 1
