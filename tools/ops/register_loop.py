"""常驻批量循环：跑完一轮歇 5 分钟；本轮 0 成功（全线撞顶）则歇 1 小时。

为什么不用 shell while：要读 run.py 的中文统计行做决策 + 统一日志，
python 循环更好维护。配额保护在 run.py 内部（按出口 IP 分桶 40/24h），
撞顶轮会自动跳过注册，所以这个循环不会击穿配额 —— 只会在恢复后自动续跑。
"""
import re
import subprocess
import time

COUNT = ["--count", "8", "--workers", "2"]
IDLE_SLEEP = 300        # 正常轮间隔 5 分钟
TOP_SLEEP = 3600        # 全线撞顶 → 1 小时后再试


def one_round() -> tuple[int, str]:
    r = subprocess.run([".venv/bin/python", "-u", "run.py", *COUNT],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=3600)
    out = (r.stdout or "") + (r.stderr or "")
    m = re.search(r"本次成功 (\d+) 个", out)
    return (int(m.group(1)) if m else -1), out


def main():
    print("[loop] started", flush=True)     # 启动即落一行 —— 死没死一眼可辨
    while True:
        t0 = time.strftime("%m-%d %H:%M:%S")
        try:
            ok, out = one_round()
        except subprocess.TimeoutExpired:
            print(f"[{t0}] 轮次超时（>1h），跳过本轮", flush=True)
            time.sleep(IDLE_SLEEP)
            continue
        print(f"[{t0}] 本轮成功 {ok} 个", flush=True)
        # 尾部关键行进日志（DONE 行 + 失败清单首行），方便不翻大日志
        for ln in out.splitlines():
            if ln.startswith(("DONE:", "失败 ")) or "API KEY =" in ln:
                print("  " + ln, flush=True)
        time.sleep(TOP_SLEEP if ok == 0 else IDLE_SLEEP)


if __name__ == "__main__":
    main()
