# -*- coding: utf-8 -*-
"""把 harvest_local.py 以**分离进程**方式启动（Windows）。

为什么不能直接 `nohup ... &`（2026-10-06 实测教训）
------------------------------------------------
在 Git Bash 里 `nohup python harvest_local.py &` 起的进程，会随**发起它的那个
shell 会话结束**被带走 —— 上一轮 23:41 起的收割进程 4 小时后查证已消失，
日志只剩启动行（且 stderr 被丢进 /dev/null，连崩溃原因都没留下）。

正解：用 `DETACHED_PROCESS` + `CREATE_NEW_PROCESS_GROUP` 起子进程，
它没有控制台、不在父进程组里，父进程退出不影响它。

用法：
    .venv\\Scripts\\python.exe tools\\ops\\start_harvest_local.py
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOG = os.path.join(ROOT, "logs", "harvest_local.log")

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

if __name__ == "__main__":
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    # stdout 走 DEVNULL：harvest_local.py 自己写 logs/harvest_local.log（含异常兜底），
    # 这里再重定向同一文件会让每行写两遍。
    p = subprocess.Popen(
        [sys.executable, "-u", os.path.join(ROOT, "tools", "ops", "harvest_local.py")],
        cwd=ROOT, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        close_fds=True)
    print(f"launched harvest_local pid={p.pid}")
