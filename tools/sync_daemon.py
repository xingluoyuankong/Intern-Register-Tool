# -*- coding: utf-8 -*-
"""本地常驻同步守护：每 N 秒从服务器拉台账 → 合并进本地 → 更新 key 清单。

「实时同步」的落地形态：本地是 Windows 没有公网入口，服务器推不过来；
用本地轮询拉（默认 180s）达到近实时。合并是并集 + 字段级补齐（见
sync_ledger_from_server.py），重复跑安全。

用法：
    .venv\\Scripts\\python.exe -u tools/sync_daemon.py          # 前台
    或交给计划任务 / 手动 nohup。日志: logs/sync_daemon.log
"""
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
SH = os.path.join(ROOT, "tools", "sync_now.sh")
LOG = os.path.join(ROOT, "logs", "sync_daemon.log")
INTERVAL = int(os.getenv("IR_SYNC_INTERVAL", "180"))

sys.path.insert(0, ROOT)


def key_count() -> int:
    try:
        import json
        d = json.load(open(os.path.join(ROOT, "ledger", "latest.json"),
                           encoding="utf-8"))
        return sum(1 for x in d if (x.get("api_key") or "").strip())
    except Exception:
        return -1


def main() -> int:
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8", errors="replace") as log:
        def w(s):
            print(time.strftime("%F %T"), s, flush=True)
            log.write(time.strftime("%F %T") + " " + s + "\n")
            log.flush()

        w(f"===== 同步守护启动 interval={INTERVAL}s =====")
        while True:
            try:
                before = key_count()
                p = subprocess.run(["bash", SH], capture_output=True,
                                   text=True, encoding="utf-8",
                                   errors="replace", timeout=180)
                out = (p.stdout or "").strip().splitlines()
                after = key_count()
                # 有变化才多写两行，没变化只记一行心跳
                if out and ("补上 key" in out[-4] if len(out) >= 4 else False):
                    pass
                tail = " | ".join(out[-3:]) if out else f"rc={p.returncode}"
                if after != before:
                    w(f"SYNC key {before} -> {after}  {tail}")
                else:
                    w(f"tick key={after} {tail[:100]}")
            except Exception as ex:                            # noqa: BLE001
                w(f"ERROR {type(ex).__name__}: {str(ex)[:140]}")
            time.sleep(INTERVAL)


if __name__ == "__main__":
    sys.exit(main() or 0)
