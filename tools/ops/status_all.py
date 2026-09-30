"""一键查况：双端 key 数 + 最近产线动作。随时可跑。"""
import glob
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SSH = ["ssh", "-o", "UserKnownHostsFile=.workbuddy-ai/ssh/known_hosts",
       "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "qwenpaw-tom"]


def local_status():
    keys = set()
    for f in glob.glob(str(ROOT / "ledger" / "runs" / "*" / "results-*.json")):
        try:
            for r in json.load(open(f, encoding="utf-8")):
                k = r.get("api_key") or ""
                if k.startswith("sk-"):
                    keys.add(k)
        except Exception:
            pass
    print(f"[本机] key 累计: {len(keys)}")
    lf = ROOT / ".workbuddy-ai" / "loop_local.log"
    if lf.exists():
        lines = lf.read_text(encoding="utf-8").splitlines()
        print("[本机] 最近动作:")
        for ln in lines[-5:]:
            print("   ", ln)


def server_status():
    cmd = ("tail -6 loop.log 2>/dev/null; "
           ".venv/bin/python -c \"import json,glob;s=set()\n"
           "for f in glob.glob('ledger/runs/*/results-*.json'):\n"
           " try:\n"
           "  for r in json.load(open(f)):\n"
           "   k=r.get('api_key') or ''\n"
           "   if k.startswith('sk-'): s.add(k)\n"
           " except Exception: pass\n"
           "print('SERVER_KEYS:', len(s))\" 2>/dev/null")
    try:
        out = subprocess.run(SSH + ["cd /root/intern-register && " + cmd],
                             capture_output=True, text=True, timeout=45,
                             encoding="utf-8", errors="replace")
        print("[服务器]")
        print(out.stdout.strip() or "(无输出)")
    except Exception as ex:  # noqa: BLE001
        print(f"[服务器] 连接失败: {type(ex).__name__}")


if __name__ == "__main__":
    local_status()
    server_status()
