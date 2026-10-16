#!/bin/bash
# 带重试的远程命令执行器。吃掉 SSH 抖动（Broken pipe / UNKNOWN port 65535）。
# 🔴 2026-10-16：裸 `ssh qwenpaw-tom`（走 ~/.ssh/config 的 ProxyCommand
#    `cloudflared access ssh`，无日志级别参数）实测大量
#    `Connection closed by UNKNOWN port 65535` / `banner exchange` 超时；
#    改成**显式 ProxyCommand + --log-level info** 后一次即通。
#    两者仅差日志级别但成功率天差地别，已固化于此，别再回退。
# 用法: ./sshx.sh '远程命令'
CMD="$1"
N="${2:-8}"
PROXY='cloudflared access ssh --hostname qwenpaw-tom.xzxyuan.ccwu.cc --log-level info'
GREP_NOISE='permanently|Warning: Permanently|REMOTE HOST|WARNING:|fingerprint|Offending|Add correct|IT IS POSSIBLE|Someone could|Keyboard-interactive|UpdateHostkeys|@@@'
for i in $(seq 1 "$N"); do
  out=$(timeout 60 ssh -o StrictHostKeyChecking=no -o ConnectTimeout=20 \
        -o ServerAliveInterval=10 -o ServerAliveCountMax=2 \
        -o ProxyCommand="$PROXY" qwenpaw-tom "$CMD" 2>&1 | grep -avE "$GREP_NOISE")
  if [ -n "$out" ] && ! echo "$out" | grep -qE "Connection closed by UNKNOWN|Broken pipe|Connection reset|banner exchange"; then
    echo "$out"
    exit 0
  fi
  sleep 4
done
echo "[SSHX_FAIL] $N 次重试后仍失败: ${CMD:0:80}"
exit 1
