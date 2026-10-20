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
# 🔴 2026-10-20 全链时间统一为北京时间（用户要求，此后日志/报告一律北京时）
export TZ="Asia/Shanghai"

cd /app/working/intern-register || exit 1
mkdir -p logs
LOG=/app/working/intern-register/logs/loop.log

# ═══ 2026-10-20 CPU/内存降压（容器禁 swap，用户态等效）═══
# ① 降优先级：regloop 全链（python+浏览器）让位于系统的 qwenpaw/控制台，
#    CPU 争抢时聊天响应优先，注册只 slowdown 不饿死。
renice -n 15 -p $$ >/dev/null 2>&1
# ② 内存闸门：可用 <500MB 时不开轮（Camoufox 一轮 ~700MB，硬开会 OOM）。
#    这与 window_hunter 的 900MB 闸门配合，两台浏览器永不同时挤爆内存。
mem_gate() {
  for _i in 1 2 3 4 5 6; do
    AV=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 999)
    [ "$AV" -ge 500 ] && return 0
    echo "[$(date '+%F %T')] ⏳ 内存 ${AV}MB<500MB，60s 后再开轮" >>"$LOG"
    sleep 60
  done
}

# 🔴 2026-10-19：.env 是 python 侧读的，shell 变量必须在这里显式导出，
#    否则 --count 永远拿默认值（今天踩坑：.env 写了 IR_ROUND_COUNT=6
#    但日志仍显示 [1/3]）。
export IR_ROUND_COUNT="${IR_ROUND_COUNT:-6}"
export IR_LOOP_SLEEP="${IR_LOOP_SLEEP:-45}"
# 🔴 2026-10-20 低谷期产能转移：注册被配额墙拦时，捡漏（登录建 key）
#    不吃注册配额 → 加码 4→8 + 窗口 600→900s，backlog（T001 被拒号）
#    在低谷期继续转化为 key。
export IR_HARVEST_LIMIT="${IR_HARVEST_LIMIT:-8}"

# camoufox 装到持久卷（默认 ~/.cache/camoufox 在 /root，会被重置）
export XDG_CACHE_HOME=/app/working/intern-register/.cache
export DISPLAY=:1

n=0
while true; do
  n=$((n + 1))
  echo "########## ROUND $n  $(date '+%F %T') ##########" >>"$LOG" 2>&1

  # 🔴 2026-10-20：开轮前过内存闸门（容器禁 swap 的等效保护）
  mem_gate

  # ① 注册主链路 —— run.py 内含 注册→激活→登录→建 Key 完整流程
  # 🔴 2026-10-13：轮内最坏耗时 ≈ 3 次 × (浏览器 60~90s + 限流退避 45~60s)
  #    ≈ 450s+，600s 的 timeout 会在**浏览器还开着**时把 run.py 整个杀掉
  #    → camoufox 进程泄漏 → 内存耗尽 → 之后所有浏览器 "Failed to launch"
  #    / "Target closed"（实测：内存 3766M 只剩 251M，残留 9 个浏览器进程）。
  #    放宽到 1800s，并在每轮结束后清理残留浏览器。
  # 🔴 2026-10-17 23:1x（北京时间）：每轮 1 → 3（出口 ~19 个、邮箱 4 个
  #    冷门域轮转，配额都够）；超时按 3 个号放宽到 3600s（单号最坏
  #    ~600s：预检+复查+浏览器+收信+重试）。
  echo "[round $n] ① 注册（全流程）" >>"$LOG" 2>&1
  # 🔴 2026-10-18：timeout 必须带 -k —— 实测 python 卡在 ThreadPoolExecutor
  #    join 里会**无视 SIGTERM**，裸 timeout 到点杀不动（18:56 现场：run.py
  #    挂了 4h42m）。-k 30 = TERM 后 30s 补 SIGKILL，物理保证轮次有上限。
  #    总时限 3600→1800：注册 3 号 ~6min + 登录熔断后应 <<30min。
  timeout -k 30 "${IR_ROUND_TIMEOUT:-1800}" .venv/bin/python -u run.py \
      --count "${IR_ROUND_COUNT:-3}" --workers 1 >>"$LOG" 2>&1
  rc=$?
  echo "[round $n] ① exit=$rc  $(date '+%F %T')" >>"$LOG" 2>&1

  # 清理残留浏览器（本脚本自己的 cmdline 不含关键字，pkill 不会自杀）
  PAT="camou""fox"; pkill -9 -f "$PAT" 2>/dev/null
  sleep 3

  # ② 捡漏：上面注册成功、但建 Key 没拿到的账号（--keyless-only 天然跳过
  #    已拿到 key 的）。串行跑在 ① 之后，不与它抢 Camoufox。
  # 🔴 2026-10-18 解冻：激活邮件发信侧被域名拉黑后（temp-mail.io 域烧穿），
  #    注册的号收不到激活邮件但**实测可直接登录**（3/3）—— 捡漏负责救
  #    这些号。老烧穿域（hotmail 等）由 run_downstream 自动跳过，不烧时间。
  # 🔴 2026-10-18：② 同样 -k 兜底 + 登录熔断联动 —— run.py 里出现 T001
  #    （登录验证码被拒）时写 ledger/.login_blocked 标记；存在则本轮
  #    跳过捡漏（登录同样被拒，纯空转），标记即删（下轮重新探测风控）。
  #    ⚠ 必须用绝对路径：本脚本没有 ROOT 变量（第一版踩过）。
  if [ -f /app/working/intern-register/ledger/.login_blocked ]; then
      rm -f /app/working/intern-register/ledger/.login_blocked
      echo "[round $n] ② 捡漏跳过（本轮登录已熔断：T001 验证码拒绝，风控窗口内登录必败） $(date '+%F %T')" >>"$LOG" 2>&1
  elif [ "${IR_HARVEST_ENABLED:-1}" = "1" ]; then
      echo "[round $n] ② 捡漏建 Key" >>"$LOG" 2>&1
      timeout -k 30 900 .venv/bin/python -u tools/run_downstream.py \
          --keyless-only --registered-only --rekey \
          --limit "${IR_HARVEST_LIMIT:-8}" --workers 1 >>"$LOG" 2>&1
      rc2=$?
      PAT="camou""fox"; pkill -9 -f "$PAT" 2>/dev/null
      echo "[round $n] ② exit=$rc2  $(date '+%F %T')" >>"$LOG" 2>&1
  else
      echo "[round $n] ② 捡漏已冻结（IR_HARVEST_ENABLED!=1；老 hotmail 存量暂缓，滑块攻下再捞） $(date '+%F %T')" >>"$LOG" 2>&1
  fi

  # 🔴 2026-10-17 降频 60s → 300s：出口存活率才是当前瓶颈（预检后到浏览器
  #    启动之间出口还会死，浏览器前已加活体复查），高频轮询只会烧浏览器启动
  #    时间换不来产出，还给目标站 WAF 喂更多流量。可用 IR_LOOP_SLEEP 覆盖。
  # 🔴 2026-10-19 邮件窗口风暴模式：本轮只要有一个号激活成功（邮件能到 =
  #    发信窗口开着），下一轮 30s 就发车抢窗口；窗口一关自动回到 300s。
  SLEEP="${IR_LOOP_SLEEP:-300}"
  if tail -n 60 "$LOG" | grep -qE "(^|[^a-z])activated([^a-z]|$)"; then
    SLEEP=30
    echo "[round $n] ⚡ 发信窗口开着 → 风暴模式 30s 发车 $(date '+%F %T')" >>"$LOG" 2>&1
  fi
  sleep "$SLEEP"
done
