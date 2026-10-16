#!/bin/bash
# 实时监控服务器产线：**单次 ssh** 取全部信息，每 180s 记一行。
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
LOG=logs/monitor.log
NOISE='permanently|Warning: Permanently|REMOTE HOST|WARNING:|fingerprint|Offending|Add correct|IT IS POSSIBLE|Someone could|Keyboard-interactive|UpdateHostkeys|@@@'

REMOTE='cd /app/working/intern-register && echo "reg=$(grep -ac "registered: uid=" logs/loop.log)"; echo "st=$(supervisorctl status regloop 2>/dev/null | grep -oE "RUNNING|STOPPED|FATAL|BACKOFF")"; echo "done=$(grep -a "DONE: " logs/loop.log | tail -1 | grep -oE "[0-9]+/[0-9]+ succeeded")"'

echo "===== 监控启动 $(date '+%F %T') =====" >>"$LOG"
while true; do
  out=""
  for i in 1 2 3; do
    out=$(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=25 -o ServerAliveInterval=10 \
          -o ServerAliveCountMax=2 qwenpaw-tom "$REMOTE" 2>&1 | grep -avE "$NOISE")
    if [ -n "$out" ] && ! echo "$out" | grep -qE "UNKNOWN port|Broken pipe|Connection reset|timed out|not responding"; then
      break
    fi
    out=""
    sleep 6
  done
  if [ -z "$out" ]; then
    echo "$(date '+%F %T') | SSH_FAIL" >>"$LOG"
  else
    line=$(echo "$out" | tr '\n' ' ')
    echo "$(date '+%F %T') | $line" >>"$LOG"
  fi
  # ── 顺带同步本地台账（sync_daemon 作为独立进程会被会话清理杀掉，
  #    并进这个常驻 bash 循环最可靠）──
  {
    echo "----- sync $(date '+%F %T') -----"
    bash tools/sync_now.sh 2>&1 | tail -4
  } >>logs/sync_daemon.log
  sleep 180
done
