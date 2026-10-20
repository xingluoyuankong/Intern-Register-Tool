"""汇总两端台账全部 API key → keys_all（含控制台 URL）。"""
import glob
import json
import subprocess
import sys
import time
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# key 管理控制台：OpenXLab 账号中心 → API Key 页
URLS = """
【OpenXLab / 书生 控制台】
- 账号登录:        https://sso.openxlab.org.cn
- API Key 管理页:  https://openxlab.org.cn/gpt/user-center  (登录后「API密钥」)
- 书生·浦语对话:   https://chat.intern-ai.org.cn

【API 调用端点（本项目实测可用）】
- Chat (OpenAI 兼容): https://discovery-api.intern-ai.org.cn/v1/chat/completions
- 模型列表:           https://discovery.intern-ai.org.cn
- 可用模型:           intern-s2 等 (intern-s1 不在免费列表, model_not_available)
- 认证头:             Authorization: Bearer <API_KEY>

【服务器台账】ssh qwenpaw-tom → /root/intern-register/ledger/
【本机台账】    E:/API获取工具/Intern-Register/ledger/
"""


def collect_local() -> OrderedDict:
    seen = OrderedDict()
    for f in glob.glob(str(ROOT / "ledger" / "runs" / "*" / "results-*.json")):
        try:
            for r in json.load(open(f, encoding="utf-8")):
                k = r.get("api_key") or ""
                if k.startswith("sk-") and k not in seen:
                    seen[k] = r
        except Exception:
            pass
    return seen


def collect_server() -> OrderedDict:
    cmd = (
        ".venv/bin/python -c \"import json,glob;seen={}\n"
        "for f in glob.glob('ledger/runs/*/results-*.json'):\n"
        " try:\n"
        "  for r in json.load(open(f)):\n"
        "   k=r.get('api_key') or ''\n"
        "   if k.startswith('sk-') and k not in seen: seen[k]=r\n"
        " except Exception: pass\n"
        "print(json.dumps(list(seen.values())))\""
    )
    try:
        out = subprocess.run(
            ["ssh", "-o", "UserKnownHostsFile=.workbuddy-ai/ssh/known_hosts",
             "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "qwenpaw-tom",
             f"cd /root/intern-register && {cmd}"],
            capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace")
        rows = json.loads(out.stdout.strip().splitlines()[-1])
        return OrderedDict((r["api_key"], r) for r in rows
                           if r.get("api_key"))
    except Exception as ex:  # noqa: BLE001
        print(f"[warn] server collect failed: {ex}", file=sys.stderr)
        return {}


def main():
    local = collect_local()
    server = collect_server()
    merged = OrderedDict()
    for k, r in list(server.items()) + list(local.items()):
        merged.setdefault(k, r)

    lines = []
    for i, (k, r) in enumerate(merged.items(), 1):
        lines.append(f"{i:02d}  {k}")
        lines.append(f"    email: {r.get('email', '')}")
        lines.append(f"    uid:   {r.get('sso_uid', '')}")
    out_txt = "\n".join(lines)

    Path("keys_all.txt").write_text(
        f"共 {len(merged)} 个 key（{time.strftime('%m-%d %H:%M')} 汇总）\n"
        f"{URLS}\n{out_txt}\n", encoding="utf-8")
    Path("keys_bare.txt").write_text(
        "\n".join(merged.keys()) + "\n", encoding="utf-8")
    print(f"merged {len(merged)} keys (local {len(local)} + server "
          f"{len(server)}) -> keys_all.txt / keys_bare.txt")
    print(out_txt[:600])




if __name__ == "__main__":
    main()
