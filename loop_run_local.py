# -*- coding: utf-8 -*-
"""本地不间断注册循环（Windows / Camoufox 引擎）。

与服务器 loop_run.sh 同构：一轮一个账号，失败退避，永续循环。
单实例锁：同一个时刻只允许一个循环在跑（msvcrt 文件锁）。
日志：logs/loop_local.log（与服务器 loop.log 格式一致，方便看板/巡检复用）。
"""
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(ROOT, "logs", "loop_local.log")
LOCK = os.path.join(ROOT, "logs", "loop_local.lock")

os.makedirs(os.path.dirname(LOG), exist_ok=True)

# ── 单实例锁 ──
import msvcrt
_lockf = open(LOCK, "a+")
try:
    msvcrt.locking(_lockf.fileno(), msvcrt.LK_NBLCK, 1)
except OSError:
    print("另一个本地注册循环已在运行，退出。", flush=True)
    sys.exit(0)


def log(msg: str) -> None:
    line = f"[{time.strftime('%F %T')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8", errors="replace") as f:
        f.write(line + "\n")


def main() -> None:
    env = dict(os.environ)
    env["IR_BROWSER_ENGINE"] = "camoufox"       # 🔴 用户指定：本机也用 Camoufox
    env.setdefault("MOZ_DISABLE_GPU_SANDBOX", "1")
    env.setdefault("MOZ_DISABLE_CONTENT_SANDBOX", "1")
    py = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
    n = 0
    log("=== local loop start (camoufox) ===")
    while True:
        n += 1
        log(f"########## ROUND {n} start ##########")
        t0 = time.time()
        rc = -1
        outf = None
        try:
            # 🔴 每轮新开文件句柄并显式 close：共享/长持句柄在 Windows 上会
            #    让 TimeoutExpired 的杀进程路径卡住（实测 2026-10-04 round456
            #    卡满 900s 后把整个父循环带死，进程直接消失）。
            outf = open(LOG, "a", encoding="utf-8", errors="replace")
            r = subprocess.run(
                [py, "-u", "run.py", "--count", "1", "--workers", "1"],
                cwd=ROOT, env=env,
                stdout=outf, stderr=subprocess.STDOUT, timeout=900)
            rc = r.returncode
        except subprocess.TimeoutExpired:
            rc = -1
            log("round timeout (900s)")
        except Exception as ex:                      # noqa: BLE001
            # 🔴 任何意外（权限/磁盘/杀进程异常）都不许带走父循环 ——
            #    这是 7×24 循环，死一次就停一整夜。
            rc = -2
            log(f"round crashed: {type(ex).__name__}: {str(ex)[:120]}")
        finally:
            try:
                if outf is not None:
                    outf.close()
            except Exception:
                pass
        log(f"[round {n}] exit={rc} elapsed={time.time()-t0:.0f}s")
        # 失败退避：连败越久退越长，别空转烧出口信誉
        if rc == 0:
            time.sleep(5)
        else:
            time.sleep(min(30 * max(n % 5, 1), 120))


if __name__ == "__main__":
    main()
