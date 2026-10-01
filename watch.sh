#!/bin/bash
# 产线 v4：不间断。screen(P级) → run → 立即下一轮 screen，无歇息。
# 服务器最稳线程：内存 3.7G，headless shell ~300MB/worker → workers 上限 3。
# 日志：loop.log 只留一行摘要/轮，run 明细进 run.log（每轮覆盖）。
cd /root/intern-register
STABLE_WORKERS=3
while true; do
  n=$(.venv/bin/python -u tools/ops/screen_slots.py 2>>loop.log | grep "usable" | grep -oE "[0-9]+" | head -1)
  if [ "${n:-0}" -ge 1 ]; then
    w=$n; [ $w -gt $STABLE_WORKERS ] && w=$STABLE_WORKERS
    c=$((n*2)); [ $c -gt 8 ] && c=8
    .venv/bin/python -u run.py --count $c --workers $w > run.log 2>&1
    ok=$(grep "本次成功" run.log | tail -1 | grep -oE "[0-9]+" | head -1)
    keys=$(.venv/bin/python -c "
import json,glob
s=set()
for f in glob.glob('ledger/runs/*/results-*.json'):
 try:
  for r in json.load(open(f)):
   k=r.get('api_key') or ''
   if k.startswith('sk-'): s.add(k)
 except Exception: pass
print(len(s))" 2>/dev/null)
    echo "[$(date '+%m-%d %H:%M:%S')] slots=$n run=$c/$w ok=${ok:-0} total_keys=${keys:-?}" >> loop.log
  else
    echo "[$(date '+%m-%d %H:%M:%S')] slots=0 (全池不可用, 立即重筛)" >> loop.log
    sleep 60
  fi
done
