# -*- coding: utf-8 -*-
"""本地（Windows）收割循环：分批跑 run_downstream，直到「已注册未领 key」清空。

为什么单独写一个（不复用 tools/ops/harvest_loop.sh）
--------------------------------------------------
服务器版 harvest_loop.sh 依赖 `supervisorctl` / `pkill -x`（Windows 没有），
且要处理「容器每 ~12 分钟重启」——本地都没有这些，直接循环即可。

为什么**分批**：run_downstream 只在整批跑完才 merge+落盘。一批太大，中途
崩/超时就整批白跑。分批（默认 6 个）后最多丢当前批，下一批自动续跑
（`--keyless-only` 天然跳过已拿到 key 的账号）。

与本地注册循环**并存**是安全的：run_downstream 已改为合并前重读台账
（见其 main() 的 `_base` 注释），且 ledger.save_snapshot 自带防缩水护栏。

用法：
    .venv\\Scripts\\python.exe -u tools/ops/harvest_local.py
    BATCH=4 WORKERS=1 ...（环境变量覆盖）
"""
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
LOG = os.path.join(ROOT, "logs", "harvest_local.log")
BATCH = int(os.getenv("BATCH", "6"))
WORKERS = int(os.getenv("WORKERS", "1"))

sys.path.insert(0, ROOT)


def key_count() -> int:
    from src import ledger
    recs = ledger.load_existing(ledger.ledger_path())
    return sum(1 for d in recs if (d.get("api_key") or "").strip())


def main() -> int:
    noprog = 0
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8", errors="replace") as log:
        def w(s: str) -> None:
            print(s, flush=True)
            log.write(s + "\n")
            log.flush()

        w(f"===== 本地收割开始 {time.strftime('%F %T')} "
          f"BATCH={BATCH} WORKERS={WORKERS} =====")
        while True:
            before = key_count()
            w(f"----- 已有 key={before} {time.strftime('%T')} -----")
            p = subprocess.run(
                [PY, "-u", "tools/run_downstream.py",
                 "--keyless-only", "--registered-only", "--rekey",
                 "--limit", str(BATCH), "--workers", str(WORKERS)],
                cwd=ROOT, capture_output=True, text=True,
                encoding="utf-8", errors="replace")
            out = (p.stdout or "") + (p.stderr or "")
            w(out)
            after = key_count()
            w(f"----- 结束 现有 key={after} (+{after - before}) -----")
            if "没有可用的" in out:
                w("===== 目标耗尽，本地收割完成 =====")
                return 0
            if after <= before:
                noprog += 1
                w(f"!! 本批零新增（连续 {noprog}）")
                if noprog >= 3:
                    w("===== 连续 3 批零新增，中止（疑似登录全挂）=====")
                    return 0
            else:
                noprog = 0
            time.sleep(2)


if __name__ == "__main__":
    # 🔴 兜底：把**任何**异常（含启动期）写进日志。上一版把 stderr 丢进
    #    /dev/null，进程悄悄死掉后完全无从查起 —— 这个坑必须堵上。
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:                       # noqa: BLE001
        import traceback
        tb = traceback.format_exc()
        try:
            with open(LOG, "a", encoding="utf-8", errors="replace") as f:
                f.write(f"!!!!! 收割异常退出 {time.strftime('%F %T')} !!!!!\n{tb}\n")
        except Exception:
            pass
        print(tb, flush=True)
        raise
