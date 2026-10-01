#!/bin/bash
cd /root/intern-register
while true; do
  now=$(date +%s)
  last=0
  [ -f .lastrun ] && last=$(cat .lastrun)
  if [ $((now - last)) -ge 300 ] && ! pgrep -f 'python -u run.py' >/dev/null 2>&1; then
    echo $now > .lastrun
    echo "[$(date '+%m-%d %H:%M:%S')] kick a round" >> loop.log
    .venv/bin/python -u run.py --count 8 --workers 2 >> loop.log 2>&1
    echo "[$(date '+%m-%d %H:%M:%S')] round done" >> loop.log
  fi
  sleep 30
done
