"""双端产线大脑（本机常驻）：本机注册 + SSH 触发服务器注册。

🔴 为什么服务器不挂常驻脚本：QwenPaw 平台会清理 SSH 会话残留进程
（setsid/nohup 都挡不住，实测三次被静默杀）。容器里也没有 cron。
所以架构改为：本机是常驻大脑，服务器只是无状态执行器 ——
每轮通过 SSH 进去跑一轮（screen→run），跑完 SSH 退出，无可杀进程。

每轮顺序（约 8-10 分钟）：
  1. 服务器：screen_slots（POST 级筛）→ 有 CLEAR 就 run（workers 2，稳）
  2. 本机：screen_slots → 有 CLEAR 就 run
"""
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOG = ROOT / ".workbuddy-ai" / "loop_all.log"
SSH = ["ssh", "-o", "UserKnownHostsFile=.workbuddy-ai/ssh/known_hosts",
       "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
       "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=2",
       "qwenpaw-tom"]


def log(*a):
    line = f"[{time.strftime('%H:%M:%S')}] " + " ".join(str(x) for x in a)
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def sh(args, timeout=600) -> str:
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                       encoding="utf-8", errors="replace", cwd=str(ROOT))
    return (r.stdout or "") + (r.stderr or "")


def sh_ssh(remote_cmd: str, timeout=600) -> str:
    """SSH 执行远端命令（带心跳 + 强杀，防止隧道断连后 subprocess 假死）。

    🔴 实测坑：QwenPaw 隧道会静默断连，此时 `subprocess.run(timeout=)`
    **不会**超时 —— 它卡在等 stdout 管道关闭上（ssh 子进程不会自己退出），
    整个大脑就挂死（22:54 起停摆 47 分钟就是这个原因）。
    解法：Popen + communicate(timeout) + 超时后 taskkill /T 杀进程树，
    并用 ServerAliveInterval 让 ssh 尽早发现对端死亡。
    """
    args = SSH + [remote_cmd]
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, encoding="utf-8", errors="replace",
                         cwd=str(ROOT))
    try:
        out, _ = p.communicate(timeout=timeout)
        return out or ""
    except subprocess.TimeoutExpired:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                           capture_output=True, timeout=30)
        except Exception:  # noqa: BLE001
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass
        return "TIMEOUT"


def count_keys(where: str, remote=False) -> int:
    py = ("import json,glob;s=set()\n"
          "for f in glob.glob('ledger/runs/*/results-*.json'):\n"
          " try:\n"
          "  for r in json.load(open(f)):\n"
          "   k=r.get('api_key') or ''\n"
          "   if k.startswith('sk-'): s.add(k)\n"
          " except Exception: pass\n"
          "print(len(s))")
    try:
        if remote:
            out = sh_ssh(
                f"cd /root/intern-register && .venv/bin/python -c \"{py}\"",
                timeout=60)
        else:
            out = sh([sys.executable, "-c", py], timeout=60)
        for ln in out.splitlines():
            ln = ln.strip()
            if ln.isdigit():
                return int(ln)
    except Exception:
        pass
    return -1


def srv_workers() -> int:
    """自适应线程数：内存够且上轮正常就 +1，异常就 -1。状态落盘。"""
    f = ROOT / ".workbuddy-ai" / "srv_workers.txt"
    try:
        return max(2, min(6, int(f.read_text().strip())))
    except Exception:  # noqa: BLE001
        f.write_text("2")
        return 2


def set_srv_workers(n: int) -> None:
    n = max(2, min(6, n))
    (ROOT / ".workbuddy-ai" / "srv_workers.txt").write_text(str(n))


def server_mem_ok() -> tuple[bool, str]:
    out = sh_ssh("free -m | awk 'NR==2{print $7}'", timeout=30)
    try:
        avail = int(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return True, "?"
    return avail > 1100, f"{avail}M"


def server_round():
    workers = srv_workers()
    log("[srv] screen...")
    out = sh_ssh("cd /root/intern-register && timeout 420 "
                    ".venv/bin/python -u tools/ops/screen_slots.py", timeout=450)
    n = 0
    for ln in out.splitlines():
        if "usable" in ln:
            try:
                n = int(ln.split()[1].split("/")[0])
            except (ValueError, IndexError):
                pass
    log(f"[srv] screen -> {n} usable | workers={workers}")
    if n < 1:
        return
    count = min(n * 2, 6)
    # 🔴 必须串行：Resin 多个槽位会共用同一出口 IP（screen 实测
    #    186.167.53.178 同时出现在两个槽位），并发 = 同 IP 同时发多个
    #    注册请求 → WAF 按批量拦（对照：单线程走代理 registered ok，
    #    3 并发全败）。速度靠多轮次补，不靠并发。
    workers = 1
    log(f"[srv] run --count {count} --workers {workers}")
    out = sh_ssh(f"cd /root/intern-register && timeout 800 "
                     f".venv/bin/python -u run.py --count {count} "
                     f"--workers {workers}", timeout=830)
    ok = 0
    crashed = False
    for ln in out.splitlines():
        if "API KEY" in ln:
            log("  [srv] " + ln.strip()[:70])
        if "本次成功" in ln:
            try:
                ok = int(ln.split("本次成功")[1].split("个")[0].strip())
            except (ValueError, IndexError):
                pass
        if "Worker process" in ln and "exited" in ln:
            crashed = True
        if "MemoryError" in ln or "OOM" in ln:
            crashed = True
    log(f"[srv] round w={workers} +{ok} crashed={crashed}")

    # 自适应爬线程：内存充裕且本轮无崩溃 → 下一轮 +1；崩了 → -1
    mem_ok, mem = server_mem_ok()
    if crashed:
        set_srv_workers(workers - 1)
        log(f"[srv] workers {workers}->{workers - 1} (crashed, mem={mem})")
    elif mem_ok and workers < 6:
        set_srv_workers(workers + 1)
        log(f"[srv] workers {workers}->{workers + 1} (mem={mem}) climbing")
    else:
        log(f"[srv] workers stay {workers} (mem={mem})")


def local_round():
    log("[loc] screen...")
    out = sh([sys.executable, "-u", "tools/ops/screen_slots.py"], timeout=450)
    n = 0
    for ln in out.splitlines():
        if "usable" in ln:
            try:
                n = int(ln.split()[1].split("/")[0])
            except (ValueError, IndexError):
                pass
    log(f"[loc] screen -> {n} usable")
    if n < 1:
        return
    count = min(n * 2, 4)
    workers = 1   # 同上：串行避开发同出口并发
    log(f"[loc] run --count {count} --workers {workers}")
    # 🔴 登录建 key 是浏览器 + 验证码，单号 60~120s，批量号数要留足时间：
    #    4 号实测要 14min+（count=4/timeout=900 被截断过一次）。
    out = sh([sys.executable, "-u", "run.py", "--count", str(count),
              "--workers", str(workers)], timeout=1500)
    ok = 0
    for ln in out.splitlines():
        if "API KEY" in ln:
            log("  [loc] " + ln.strip()[:70])
        if "本次成功" in ln:
            try:
                ok = int(ln.split("本次成功")[1].split("个")[0].strip())
            except (ValueError, IndexError):
                pass
    log(f"[loc] round +{ok}")


if __name__ == "__main__":
    # 🔴 低频高效，不瞎撞：每轮先筛（限并发 8）→ 只跑真 CLEAR 槽位 →
    #    跑完歇 COOLDOWN。高频乱撞只会把出口段打进 405（10-01 教训）。
    COOLDOWN = int(os.getenv("IR_LOOP_COOLDOWN", "900"))
    log(f"loop_all started (cooldown={COOLDOWN}s)")
    while True:
        try:
            server_round()
        except Exception as ex:  # noqa: BLE001
            log(f"[srv] EXC {type(ex).__name__}: {str(ex)[:100]}")
        try:
            local_round()
        except Exception as ex:  # noqa: BLE001
            log(f"[loc] EXC {type(ex).__name__}: {str(ex)[:100]}")
        log(f"cycle done | srv_keys={count_keys('srv', remote=True)} "
            f"loc_keys={count_keys('loc')}")
        log(f"cool down {COOLDOWN}s")
        time.sleep(COOLDOWN)
