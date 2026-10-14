#!/bin/bash
# 一条龙循环：**注册全流程**（run.py，内部含 注册→激活→登录→建Key）
#           + **捡漏**（run_downstream，补建注册成功但没拿到 key 的账号）。
# 用法：  ./loop_run.sh            # 前台
#         setsid nohup ./loop_run.sh >/dev/null 2>&1 &   # 后台常驻
#
# 🔴 2026-10-14 整合：以前是 regloop（注册）和 harvest（登录建 key）两个
#    supervisor 程序并存，而 harvest 每批前会 `stop regloop` 独占 Camoufox ——
#    目标耗尽后它被 supervisor 无限重启，就把 regloop 反复杀掉（实测 04:46
#    停到 12:46，8 小时零注册）。其实 `run.py` 的 `run_batch` **本来就包含**
#    `stage_login_key`（登录+建 key），harvest 只需作为**捡漏**串行跑在后面。
#    两者串行执行，不抢浏览器，也不需要互相停对方。
#
# 🔴 2026-10-13：项目从 /root/intern-register 迁到持久卷
#    /app/working/intern-register —— /root 会被平台 overlay 重置（10-12 丢过一次），
#    持久卷不会。这里用**直接持久路径**，不经 /root 软链，保证 /root 重置后仍能跑。
cd /app/working/intern-register || exit 1
mkdir -p logs
LOG=/app/working/intern-register/logs/loop.log

# camoufox 装到持久卷（默认 ~/.cache/camoufox 在 /root，会被重置）
export XDG_CACHE_HOME=/app/working/intern-register/.cache
export DISPLAY=:1

n=0
while true; do
  n=$((n + 1))
  echo "########## ROUND $n  $(date '+%F %T') ##########" >>"$LOG" 2>&1

  # ① 注册主链路 —— run.py 内含 注册→激活→登录→建 Key 完整流程
  # 🔴 2026-10-13：轮内最坏耗时 ≈ 3 次 × (浏览器 60~90s + 限流退避 45~60s)
  #    ≈ 450s+，600s 的 timeout 会在**浏览器还开着**时把 run.py 整个杀掉
  #    → camoufox 进程泄漏 → 内存耗尽 → 之后所有浏览器 "Failed to launch"
  #    / "Target closed"（实测：内存 3766M 只剩 251M，残留 9 个浏览器进程）。
  #    放宽到 1800s，并在每轮结束后清理残留浏览器。
  echo "[round $n] ① 注册（全流程）" >>"$LOG" 2>&1
  timeout 1800 .venv/bin/python -u run.py \
      --count "${IR_ROUND_COUNT:-1}" --workers 1 >>"$LOG" 2>&1
  rc=$?
  echo "[round $n] ① exit=$rc  $(date '+%F %T')" >>"$LOG" 2>&1

  # 清理残留浏览器（本脚本自己的 cmdline 不含关键字，pkill 不会自杀）
  PAT="camou""fox"; pkill -9 -f "$PAT" 2>/dev/null
  sleep 3

  # ② 捡漏：上面注册成功、但建 Key 没拿到的账号（--keyless-only 天然跳过
  #    已拿到 key 的）。串行跑在 ① 之后，不与它抢 Camoufox。
  echo "[round $n] ② 捡漏建 Key" >>"$LOG" 2>&1
  timeout 900 .venv/bin/python -u tools/run_downstream.py \
      --keyless-only --registered-only --rekey \
      --limit "${IR_HARVEST_LIMIT:-4}" --workers 1 >>"$LOG" 2>&1
  rc2=$?
  PAT="camou""fox"; pkill -9 -f "$PAT" 2>/dev/null
  echo "[round $n] ② exit=$rc2  $(date '+%F %T')" >>"$LOG" 2>&1

  # 轮间隔：默认 60s（注册已不再被 429 卡死，不需要 300s 那么保守）。
  # 可用 IR_LOOP_SLEEP 覆盖。
  sleep "${IR_LOOP_SLEEP:-60}"
done
