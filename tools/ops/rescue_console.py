#!/usr/bin/env python3
"""经平台 console 通道向容器内 agent 下 shell 命令。

用途：CF 隧道离线（1033/530）导致 SSH 进不去时，这是唯一旁路 ——
平台 console_base_url 由平台直连容器，不经过 cloudflared 隧道。

用法：
    python3 rescue_console.py <label> <appId> "<给容器 agent 的消息>"
"""
import json
import sys
import time
import urllib.error
import urllib.request

CRED = "/opt/qwenpaw-guard/credentials.json"
BASE_API = "https://platform.agentscope.io"
UA = {"User-Agent": "Mozilla/5.0"}

label, appid, msg = sys.argv[1], sys.argv[2], sys.argv[3]

c = json.load(open(CRED, encoding="utf-8"))
acc = [a for a in c["accounts"] if a.get("label") == label]
if not acc:
    print("ERR: credentials.json 里没有 label=%s 的账号" % label)
    print("    现有 label: %s" % ", ".join(a.get("label", "?") for a in c["accounts"]))
    sys.exit(1)
tok = acc[0]["accessToken"]
H = dict(UA, Authorization="Bearer " + tok, Accept="application/json")


def get(url, headers=None):
    return json.loads(urllib.request.urlopen(
        urllib.request.Request(url, headers=headers or H), timeout=40).read())


# 1) 领 console_base_url + console_token（单次签发）
d = (get(BASE_API + "/api/v1/qwenpaw/" + appid).get("data") or {})
base = (d.get("console_base_url") or "").rstrip("/")
ctok = d.get("console_token")
if not (base and ctok):
    print("ERR: 拿不到 console_base_url/console_token ->", json.dumps(d)[:300])
    sys.exit(1)

# 2) 发给容器内 agent
body = json.dumps({
    "input": [{"role": "user", "content": [{"type": "text", "text": msg}]}],
    "session_id": sys.argv[4] if len(sys.argv) > 4 else "rescue-%d" % int(time.time()),
    "stream": True,
}).encode()
req = urllib.request.Request(
    base + "/api/console/chat", data=body, method="POST",
    headers={"Cookie": "qwenpaw_console_token=" + ctok,
             "Content-Type": "application/json",
             "Accept": "text/event-stream",
             "User-Agent": UA["User-Agent"]})
try:
    raw = urllib.request.urlopen(req, timeout=300).read().decode("utf-8", "ignore")
except urllib.error.HTTPError as e:
    print("HTTPError", e.code, e.read().decode("utf-8", "ignore")[:400])
    sys.exit(1)

texts = []
for line in raw.splitlines():
    if not line.startswith("data: "):
        continue
    try:
        j = json.loads(line[6:])
    except Exception:
        continue
    if j.get("type") == "text" and j.get("text"):
        texts.append(j["text"])
    elif j.get("type") == "error":
        texts.append("[ERROR] " + str(j.get("error") or j)[:300])
out = "".join(texts)
# 容器 agent 会把思考过程一起当 text 吐出来，用标记把真正的 shell 回显切出来
BEGIN, END = "@@@BEGIN@@@", "@@@END@@@"
# agent 常把标记原文复述进思考过程，取「最长」的那一段才是真正的 shell 回显
best = ""
pos = 0
while True:
    i = out.find(BEGIN, pos)
    if i < 0:
        break
    tail = out[i + len(BEGIN):]
    seg = tail[:tail.index(END)] if END in tail else tail
    if len(seg) > len(best):
        best = seg
    pos = i + len(BEGIN)
if best:
    out = best
print("=== 容器回显 (%d 字符) ===" % len(out))
print(out[:8000] if out else "(空/未含标记，以下为原始尾部)\n" + "".join(texts)[-3000:])
