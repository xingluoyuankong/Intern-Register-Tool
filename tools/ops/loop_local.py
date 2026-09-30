"""本机产线循环：每轮 screen_slots → 立即跑小批量 → 歇 8 分钟。

screen 与 run 之间的时间差越短，出口失效率越低（实测 3 分钟内 3/3 全成，
隔 10 分钟就 0/6 —— Resin 出口质量瞬变）。所以用最小批量 + 高频循环。
"""
import subprocess
import sys
import time
from pathlib import Path

LOG = Path(__file__).resolve().parents[2] / ".workbuddy-ai" / "loop_local.log"
ROOT = Path(__file__).resolve().parents[2]


def log(*a):
    line = f"[{time.strftime('%H:%M:%S')}] " + " ".join(str(x) for x in a)
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main():
    log("local loop started")
    while True:
        # 1) 筛
        r = subprocess.run(
            [sys.executable, "-u", "tools/ops/screen_slots.py"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=420, cwd=str(ROOT))
        out = (r.stdout or "") + (r.stderr or "")
        n = 0
        for ln in out.splitlines():
            if "usable" in ln:
                try:
                    n = int(ln.split()[1].split("/")[0])
                except (ValueError, IndexError):
                    pass
        log(f"screen -> {n} usable")
        if n < 1:
            log("no usable slots, sleep 10min")
            time.sleep(600)
            continue

        # 2) 立即跑（窗口内）
        count = min(n * 2, 6)
        workers = min(n, 3)
        log(f"run --count {count} --workers {workers}")
        try:
            r2 = subprocess.run(
                [sys.executable, "-u", "run.py",
                 "--count", str(count), "--workers", str(workers)],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=900, cwd=str(ROOT))
            o2 = (r2.stdout or "") + (r2.stderr or "")
            ok = 0
            for ln in o2.splitlines():
                if "本次成功" in ln:
                    try:
                        ok = int(ln.split("本次成功")[1].split("个")[0].strip())
                    except (ValueError, IndexError):
                        pass
                if "API KEY" in ln:
                    log("  " + ln.strip()[:80])
            log(f"round done, {ok} succeeded")
        except subprocess.TimeoutExpired:
            log("round timeout")
        time.sleep(480)     # 歇 8 分钟


if __name__ == "__main__":
    main()
