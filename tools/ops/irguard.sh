#!/bin/bash
# irguard —— Intern-Register 进程级保活（supervisord 托管，实例重启也不丢）
# 职责：每 60s 巡检一轮，发现异常自动拉起；只管容器内进程，
#       实例本身被平台休眠/停止由 WorkBuddy 定时任务（平台 API）负责。

ROOT=/root/intern-register
LOG=/root/intern-register/logs/irguard.log
LOOP_LOG=$ROOT/logs/loop.log
STALE_SEC=900          # loop.log 超过 15 分钟没动静视为假死
mkdir -p "$(dirname "$LOG")"

log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

restart_prog() {
    log "ACTION: supervisorctl restart $1"
    supervisorctl restart "$1" >> "$LOG" 2>&1
}

while true; do
    # ── 维护暂停哨兵 ──
    # 存在 $ROOT/.pause_regloop 时，**不**拉 regloop、也**不**做假死重启 ——
    # 收割（run_downstream）等需要独占 Camoufox 的维护窗口用它：本机 2 核/3.7G，
    # regloop 的登录 + 收割的登录同时开浏览器 = 3 个 Camoufox 抢内存 →
    # TargetClosedError（2026-10-05 实测）。删掉哨兵即恢复。
    PAUSED=0
    [ -f "$ROOT/.pause_regloop" ] && PAUSED=1

    # ── 1. regloop：进程必须在 RUNNING ──
    if [ "$PAUSED" -eq 1 ]; then
        :
    else
        st=$(supervisorctl status regloop 2>/dev/null | awk '{print $2}')
        case "$st" in
            RUNNING) : ;;
            ""|FATAL|EXITED|BACKOFF|STOPPED|unknown)
                log "regloop status=$st -> restart"
                supervisorctl update >/dev/null 2>&1     # 配置变更兜底
                restart_prog regloop
                ;;
        esac
    fi

    # ── 2. regloop 假死检测：进程在但 loop.log 长时间无更新 ──
    if [ "$PAUSED" -eq 1 ]; then
        :
    elif [ -f "$LOOP_LOG" ]; then
        now=$(date +%s)
        mtime=$(stat -c %Y "$LOOP_LOG" 2>/dev/null || echo "$now")
        age=$(( now - mtime ))
        if [ "$age" -gt "$STALE_SEC" ]; then
            log "loop.log stale ${age}s (>${STALE_SEC}s) -> restart regloop"
            restart_prog regloop
        fi
    fi

    # ── 3. cloudflared 隧道：挂了会拖死看板域名 ──
    st=$(supervisorctl status cloudflared-tunnel 2>/dev/null | awk '{print $2}')
    if [ "$st" != "RUNNING" ] && [ -n "$st" ]; then
        log "cloudflared-tunnel status=$st -> restart"
        restart_prog cloudflared-tunnel
    fi

    # ── 4. irdash 看板 ──
    st=$(supervisorctl status irdash 2>/dev/null | awk '{print $2}')
    if [ "$st" != "RUNNING" ] && [ -n "$st" ]; then
        log "irdash status=$st -> restart"
        restart_prog irdash
    fi

    # ── 5. 磁盘兜底：清掉没被占用的浏览器临时 profile + 陈旧 ssh socket ──
    #    run.py 正常退出会自己清，但浏览器被 kill / 超时 / TargetClosedError
    #    时 profile 会永远留在 /tmp（实测堆到 282M / 8 个目录）→ 这里兜底。
    for d in /tmp/playwright_firefoxdev_profile-* /tmp/playwright_chromiumdev_profile-* \
             /tmp/camoufox* /tmp/.org.chromium*; do
        [ -d "$d" ] || continue
        fuser "$d" >/dev/null 2>&1 && continue      # 活的浏览器在用，跳过
        find "$d" -maxdepth 0 -mmin +30 -exec rm -rf {} + 2>/dev/null
    done
    find /tmp -maxdepth 1 -name 'ssh-*' -type d -mmin +60 -exec rm -rf {} + 2>/dev/null

    # ── 6. 台账防膨胀：每轮都写全量快照(2.3MB) → 一天几千份/4.9GB（实测）──
    #    合并成一份权威快照再删冗余。脚本内自带"新快照必须覆盖全部带 key
    #    账号"的校验，校验不过就不删，安全。每天最多跑一次（看哨兵文件）。
    STAMP=/root/intern-register/logs/.ledger_consolidated
    TODAY=$(date +%F)
    if [ "$(cat "$STAMP" 2>/dev/null)" != "$TODAY" ]; then
        if /root/intern-register/.venv/bin/python \
             /root/intern-register/tools/ops/consolidate_ledger.py \
             >> /root/intern-register/logs/ledger_consolidate.log 2>&1; then
            echo "$TODAY" > "$STAMP"
            log "台账已合并（防膨胀）"
        fi
    fi

    sleep 60
done
