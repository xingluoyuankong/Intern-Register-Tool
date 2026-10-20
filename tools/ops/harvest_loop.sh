#!/usr/bin/env bash
# 收割「已注册未领 key」账号：分批跑下游，**每批结束立即写回台账**，可中断续跑。
#
# 为什么分批（而不是一条命令 --limit 0 跑完）
# ------------------------------------------------
# run_downstream.py 只在**全部跑完**后才 merge + 落盘。服务器是 2 核 / 3.7G，
# Camoufox 重且偶发 TargetClosedError —— 一条命令跑 82 个账号中途崩，
# 前面几十个已登录/已建 key 的结果**全部丢失**（它们只活在内存里）。
# 分批（每批 8 个）后，崩溃最多丢当前批。且下一批的 --keyless-only 会
# 自动跳过上一批已拿到 key 的账号 —— 天然续跑，无需游标。
#
# 用法：
#   BATCH=8 WORKERS=2 bash tools/ops/harvest_loop.sh
set -u
cd "$(dirname "$0")/../.." || exit 1
PY=.venv/bin/python
BATCH="${BATCH:-8}"
WORKERS="${WORKERS:-2}"

key_count() {
  PYTHONPATH=/app/working/intern-register $PY - <<'PYEOF'
from src import ledger
recs = ledger.load_existing(ledger.ledger_path())
def has(d, k): return bool((d.get(k) or "").strip())
print(sum(1 for d in recs if has(d, "api_key")))
PYEOF
}

# 「已注册(sso_uid) 但没有 api_key」的待收数量 —— 决定要不要抢 regloop 的资源
keyless_count() {
  PYTHONPATH=/app/working/intern-register $PY - <<'PYEOF'
from src import ledger
recs = ledger.load_existing(ledger.ledger_path())
def has(d, k): return bool((d.get(k) or "").strip())
print(sum(1 for d in recs
          if has(d, "sso_uid") and not has(d, "api_key")
          and has(d, "email") and has(d, "password")))
PYEOF
}

echo "===== 收割开始 $(date -u +%F' '%T) UTC  BATCH=$BATCH WORKERS=$WORKERS ====="
noprog=0
i=0
while true; do
  i=$((i + 1))

  # ── 先查有没有货，没有就**安静等**，绝不碰 regloop ──────────────
  # 🔴 2026-10-14 致命 bug：目标耗尽后本脚本 break 退出 → supervisord
  #    `autorestart=true` 立刻重启 → 重启第一件事就是下面的 `stop regloop`
  #    → regloop 被反复杀掉（实测从 04:46 停到 12:46，8 小时零新账号）。
  #    收割没有货时，唯一的增长点是**注册**，这时候杀 regloop 等于自杀。
  avail=$(keyless_count)
  if [ "${avail:-0}" -le 0 ]; then
    echo "[idle] $(date -u +%T) 无可收割账号（已注册未领key=0）→ 等 300s，不动 regloop"
    # 顺便确认 regloop 在跑（生产新账号是唯一增长点）
    if ! supervisorctl status regloop 2>/dev/null | grep -q RUNNING; then
      echo "[idle] regloop 没在跑 → 拉起（新账号只能靠它）"
      supervisorctl start regloop >/dev/null 2>&1
    fi
    noprog=0
    sleep 300
    continue
  fi

  # ── 有货才停 regloop（收割需独占 Camoufox，避免抢内存）──────────
  if supervisorctl status regloop 2>/dev/null | grep -q RUNNING; then
    echo "[guard] 有 $avail 个待收 → regloop 让位"
    supervisorctl stop regloop >/dev/null 2>&1
    sleep 3
  fi
  pkill -9 -f 'run\.py --count' >/dev/null 2>&1
  pkill -9 -x camoufox-bin >/dev/null 2>&1
  sleep 2
  before=$(key_count)
  echo "----- 批次 $i  $(date -u +%T)  已有 key=$before  待收=$avail -----"
  # 🔴 `tee` 而不是 `out=$(...)`：`$(...)` 会把整批输出攒到批末才落日志，
  #    批内进度完全看不到（排查时是盲区）。tee 边跑边写，同时留一份给下面解析。
  tmpf=$(mktemp)
  $PY -u tools/run_downstream.py --keyless-only --registered-only --rekey \
        --limit "$BATCH" --workers "$WORKERS" 2>&1 | tee "$tmpf"
  out=$(cat "$tmpf"); rm -f "$tmpf"
  after=$(key_count)
  echo "----- 批次 $i 结束  现有 key=$after (+$((after - before))) -----"
  if echo "$out" | grep -q '没有可用的'; then
    echo "===== 目标耗尽，收割完成  $(date -u +%F' '%T) UTC ====="
    break
  fi
  if [ "$after" -le "$before" ]; then
    noprog=$((noprog + 1))
    echo "!! 本批零新增（连续 $noprog 批）"
    if [ "$noprog" -ge 3 ]; then
      echo "===== 连续 3 批零新增，中止（疑似代理/登录全挂）  $(date -u +%T) ====="
      break
    fi
  else
    noprog=0
  fi
  sleep 2
done
