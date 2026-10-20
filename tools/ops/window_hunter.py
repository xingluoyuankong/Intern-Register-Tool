#!/usr/bin/env python3
"""窗口猎手（2026-10-18 20:0x）——T001 风控时代的 key 产能引擎。

背景（当天实测）：
  - 登录验证码 T001 全天高压（直连/代理两路都拒），但**间歇开窗**：
    13:01 开过几分钟（捡漏连出 3 key）；昨天 23:14~04:00 整夜开着（25+ key）。
  - 注册不受影响 → regloop 持续攒号，keyless 存量 200+。

策略：与其在 regloop 里按轮被动探一次，不如专职进程全天候狩猎——
  ① 探针：run_downstream --limit 1（直连登录，每次全新浏览器=全新指纹）
  ② 探针过 = 窗口开 → 立刻 BLAST：--limit（默认 25）连续收割存量，
     直到出现登录失败（窗口关）或存量清空
  ③ 探针败 = 退避 10 分钟再探（探针成本 ~2min，每小时 ~5 发，
     足以咬住 10 分钟以上的窗口）

安全护栏：
  - 每次子进程都有 timeout -k（run_downstream 挂了也拖不死猎手）
  - 单实例标记文件防 supervisor 重启叠加
  - 全部日志追加 logs/window_hunter.log

用法：由 ir-supervisord 常驻（program:window-hunter）；手动：
    DISPLAY=:1 .venv/bin/python -u tools/ops/window_hunter.py
"""
import os
import random
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
LOG = os.path.join(ROOT, "logs", "window_hunter.log")
ENV = {**os.environ,
       "DISPLAY": os.environ.get("DISPLAY", ":1"),
       "XDG_CACHE_HOME": os.path.join(ROOT, ".cache")}
for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    ENV.pop(k, None)          # 🔴 直连登录：代理变量会污染 discovery/requests

# 🔴 2026-10-18 20:5x：600→300s。实测窗口比预想短（13:01 那次只开了几分钟
#    就出 3 个 key）；10 分钟退避 = 每小时才 5 发探针，短窗口容易整段错过。
#    5 分钟退避 = 每小时 ~10 发，探针本身 100s 成本可承受。
# 🔴 2026-10-19 300→210：实测窗口可以只开 2-3 分钟（10-18 的 13:01 短窗），
#    300s 探针间隔可能整窗错过。降均值提密度；抓到窗后仍由爆发模式收割。
# 🔴 2026-10-20 210→420 CPU 降压：每次探针都是完整 Camoufox 登录会话
#    （~250s），210s 退避=浏览器近乎连续运转=持续 CPU/内存高载。
#    420s 把浏览器占空比压到 ~37%；窗口捕获延迟中位数 +3.5 分钟，
#    爆发收割不受影响。要更密设 HUNTER_BACKOFF 环境变量。
BACKOFF_CLOSED = float(os.getenv("HUNTER_BACKOFF", "420"))
BLAST_LIMIT = int(os.getenv("HUNTER_BLAST", "25"))           # 开窗一次收割量
PROBE_TIMEOUT = int(os.getenv("HUNTER_PROBE_TIMEOUT", "420"))
BLAST_TIMEOUT = int(os.getenv("HUNTER_BLAST_TIMEOUT", "2400"))
COOLDOWN_AFTER_BLAST = 120.0


def log(msg: str) -> None:
    line = f"{time.strftime('%F %T')}  {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _browser_busy() -> bool:
    """regloop 是否正在跑浏览器（run.py 存活 + 有 camoufox 进程）。

    🔴 2026-10-18 OOM 根治：容器只有 4G，`qwenpaw` 已占 600M+。
    regloop 每轮开 1 个 Camoufox（3 个标签 = 3 个浏览器实例，实测峰值
    900M~1.2G），猎手再开一个 → 顶爆内核 OOM，随机杀内容进程 →
    `TargetClosedError`（探针和注册都会"随机崩"）。
    猎手是**低优先级后台任务**，必须让路：regloop 在跑就等。
    """
    try:
        r = subprocess.run(["pgrep", "-f", "u run\\.py"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return True
        r = subprocess.run(["pgrep", "-c", "-f", "camoufox"],
                           capture_output=True, text=True, timeout=5)
        return (r.stdout or "0").strip() not in ("", "0")
    except Exception:
        return False


def _wait_browser_free(max_wait: float = 900.0) -> bool:
    """等 regloop 让出浏览器；最多等 max_wait 秒。"""
    t0 = time.time()
    while time.time() - t0 < max_wait:
        if not _browser_busy():
            return True
        time.sleep(20)
    return False


def run_harvest(limit: int, timeout: int) -> tuple[bool, str]:
    """跑一次收割。返回 (这次有没有任何登录成功, 尾部输出)。"""
    cmd = ["timeout", "-k", "30", str(timeout),
           os.path.join(ROOT, ".venv", "bin", "python"), "-u",
           os.path.join(ROOT, "tools", "run_downstream.py"),
           "--keyless-only", "--registered-only", "--rekey",
           "--limit", str(limit), "--workers", "1"]
    p = subprocess.run(cmd, cwd=ROOT, env=ENV, capture_output=True,
                       text=True, timeout=timeout + 60)
    out = (p.stdout or "") + (p.stderr or "")
    # 🔴 2026-10-18：只留 tail 6 行会把 traceback 关键行吃掉（排查时盲人摸象）。
    #    保留**全部错误信息**（Traceback 及后 12 行），成功信息才留 tail。
    if "Traceback" in out:
        i = out.find("Traceback")
        tail = out[i:i + 900].replace("\n", " | ")
    else:
        tail = "\n".join(out.strip().splitlines()[-6:])
    # 判据：输出里有 ✓ 登录 = 有成功（run_downstream 逐账号打 ✓ 登录）
    return ("✓ 登录" in out, tail)


def _mem_ok(min_avail_mb: int = 900) -> bool:
    """🔴 2026-10-19 OOM 保险：可用内存 <900MB 时不碰浏览器。

    10-18 17:41 regloop + window-hunter 同时开 Camoufox，3.7G 容器
    双双被内核 OOM-Killed → supervisor 重试到 segfault 整线躺平 17h。
    浏览器整链要 ~700MB，低于此阈值再开就是自杀。
    """
    try:
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024 >= min_avail_mb
    except OSError:
        return True
    return True


def main() -> int:
    # 🔴 2026-10-20：降 CPU 优先级（容器禁 swap 的用户态等效之一）——
    #    猎手浏览器让位于 qwenpaw/控制台，争抢时只 slowdown 不饿死。
    try:
        os.nice(10)
    except OSError:
        pass
    lockf = "/tmp/.window_hunter.lock"
    if os.path.exists(lockf):
        # supervisor 重启叠加保护：清 5 分钟以上的陈锁
        try:
            if time.time() - os.path.getmtime(lockf) > 300:
                os.remove(lockf)
            else:
                log("另一个猎手实例在跑，退出")
                return 0
        except OSError:
            pass
    try:
        open(lockf, "w").write(str(os.getpid()))
    except OSError:
        pass
    try:
        log("🏹 窗口猎手启动：退避 %.0fs / 爆发 %d 个" % (BACKOFF_CLOSED, BLAST_LIMIT))
        # 猎手起步先歇 5 分钟：别和 regloop 刚重启的注册挤浏览器内存
        time.sleep(300)
        while True:
            # 🔴 2026-10-18：等 regloop 的浏览器退场再探 —— 避免 4G 容器里
            #    两家浏览器同时存在被内核 OOM 杀掉（见 _browser_busy 说明）。
            if not _wait_browser_free():
                log("⏳ regloop 长期占用浏览器，跳过本轮探针")
                time.sleep(120)
                continue
            # 🔴 2026-10-19：内存闸门 —— 可用内存不足 900MB 就等 2 分钟再探，
            #    宁可漏一次窗口也不能把 regloop 挤死（10-18 教训）。
            if not _mem_ok():
                log("⏳ 内存不足 900MB，2 分钟后再探")
                time.sleep(120)
                continue
            t0 = time.time()
            ok, tail = run_harvest(1, PROBE_TIMEOUT)
            if ok:
                log("🟢 探针过（%.0fs）—— 开窗！爆发收割 %d 个…" %
                    (time.time() - t0, BLAST_LIMIT))
                blasts = 0
                while True:
                    if not _wait_browser_free():
                        log("⏳ regloop 占用浏览器，暂停爆发")
                        break
                    ok2, tail2 = run_harvest(BLAST_LIMIT, BLAST_TIMEOUT)
                    blasts += 1
                    if ok2:
                        log(f"🟢 爆发 #{blasts} 有产出，继续\n  {tail2[:180]}")
                        continue
                    log(f"🔴 爆发 #{blasts} 无登录成功（窗口关）\n  {tail2[:180]}")
                    break
                time.sleep(COOLDOWN_AFTER_BLAST)
            else:
                # 🔴 2026-10-18 防风控统计：固定 300s 周期探针 = 可被服务端
                #    时间序列直接聚类的机器人节拍。改为 4~7 分钟**随机退避**
                #    （均值仍 ~5.5min，不损失抓窗能力，但周期不可预测）。
                backoff = BACKOFF_CLOSED + random.uniform(-60, 120)
                log("🔴 探针败（%.0fs）%.1f 分钟后再探\n  %s" %
                    (time.time() - t0, backoff // 60, tail.replace("\n", " | ")[:150]))
                time.sleep(max(60, backoff))
    finally:
        try:
            os.remove(lockf)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
