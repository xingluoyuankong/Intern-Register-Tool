#!/usr/bin/env python3
"""Intern-Register 看板 —— 单文件、零依赖（纯标准库）。

数据源：/root/intern-register/ledger/runs/*/*.json（按 email 去重、后写覆盖）
      + /root/intern-register/logs/loop.log（循环日志）
      + supervisorctl status regloop（循环进程状态）

🔴 面板会显示 api_key / 密码，走网页表单登录（POST /login 下发 HttpOnly 会话 Cookie，
   有效期 7 天；/logout 登出）。账号密码从环境变量 IR_DASH_USER / IR_DASH_PASS 读
   （默认 admin/irpanel）。不再用浏览器 Basic Auth 弹窗。
端口 8000 —— 对应 Cloudflare Tunnel ingress 的 ir-qwenpaw-tom.xzxyuan.ccwu.cc。
"""
import glob
import html
import json
import os
import re
import secrets
import subprocess
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = "/root/intern-register"
LOG = os.path.join(ROOT, "logs", "loop.log")
USER = os.getenv("IR_DASH_USER", "admin")
PASS = os.getenv("IR_DASH_PASS", "irpanel")
TZ8 = timezone(timedelta(hours=8))          # 面板按北京时间显示
PORT = int(os.getenv("IR_DASH_PORT", "8000"))

# ────────────────────────── 会话（网页表单登录） ──────────────────────────
# token → 过期时间戳；持久化到磁盘（实例/进程重启不丢登录态）。
_SESSIONS = {}
SESSION_TTL = 7 * 24 * 3600                 # 7 天
COOKIE_NAME = "ir_sess"
SESSIONS_FILE = os.path.join(ROOT, "ledger", ".dash_sessions.json")


def _save_sessions():
    try:
        os.makedirs(os.path.dirname(SESSIONS_FILE), exist_ok=True)
        json.dump(_SESSIONS, open(SESSIONS_FILE, "w", encoding="utf-8"))
    except Exception:
        pass


def _load_sessions():
    try:
        d = json.load(open(SESSIONS_FILE, encoding="utf-8"))
        if isinstance(d, dict):
            now = time.time()
            _SESSIONS.update({k: float(v) for k, v in d.items() if float(v) > now})
    except Exception:
        pass


def _new_session() -> str:
    tok = secrets.token_urlsafe(32)
    _SESSIONS[tok] = time.time() + SESSION_TTL
    # 顺手清过期
    now = time.time()
    for k in [k for k, v in _SESSIONS.items() if v < now]:
        _SESSIONS.pop(k, None)
    _save_sessions()
    return tok


def _check_session(tok: str) -> bool:
    exp = _SESSIONS.get(tok or "")
    return bool(exp and exp > time.time())


# ────────────────────────── 数据聚合 ──────────────────────────
def load_records():
    """扫全部 runs 快照，按 email 去重（文件名时间序，后写覆盖先写）。"""
    recs = {}
    for f in sorted(glob.glob(os.path.join(ROOT, "ledger", "runs", "*", "*.json"))):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        for r in (d if isinstance(d, list) else [d]):
            if not isinstance(r, dict):
                continue
            em = r.get("email")
            if not em:
                continue
            old = recs.get(em)
            # 后写的快照更新；且"有 key 的"优先于"没 key 的"
            if old is None or f >= old["_src"]:
                recs[em] = dict(r, _src=f)
    return list(recs.values())


def summarize():
    recs = load_records()
    now8 = datetime.now(TZ8)
    today = now8.strftime("%Y-%m-%d")

    ok_all = [r for r in recs if r.get("api_key")]
    fail_all = [r for r in recs if r.get("status") == "failed"]

    # 🔴 统一取"key 时刻"：优先 **key_built_at**（收割/建 key 的时间，UTC），
    #    回退 created_at（注册时间）。没有 key_built_at 时，收割今天建出的
    #    key 会按老注册日统计 → 面板"今日 Key"永远 0（2026-10-14 实测）。
    #    注意：key_built_at / created_at 都是 **UTC**，与"北京时间今日"比较
    #    前必须 +8 —— 否则北京时间 00:00-08:00 建的 key 会被算到"昨天"。
    def key_time8(r) -> str:
        raw = str(r.get("key_built_at") or r.get("created_at") or "")
        try:
            c = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
            return c.replace(tzinfo=timezone.utc).astimezone(TZ8).strftime(
                "%Y-%m-%d %H:%M:%S")
        except Exception:
            return raw

    def fail_time8(r) -> str:
        raw = str(r.get("created_at") or "")
        try:
            c = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
            return c.replace(tzinfo=timezone.utc).astimezone(TZ8).strftime(
                "%Y-%m-%d %H:%M:%S")
        except Exception:
            return raw

    # 兼容保留：明细排序用原始串即可（同序）
    key_time = key_time8
    fail_time = fail_time8

    ok_today = [r for r in ok_all if key_time8(r).startswith(today)]
    fail_all = [r for r in fail_all if not r.get("api_key")]  # 建出 key 的不算失败
    fail_today = [r for r in fail_all if fail_time8(r).startswith(today)]

    def kind(r):
        e = (r.get("error") or r.get("error_kind") or "").strip()
        return (e.split(":")[0].split("，")[0][:28] or "unknown")

    fail_top = Counter(kind(r) for r in fail_all).most_common(8)

    # 最近 24h（北京时间）每小时成功数
    def _utc_to_8(s: str):
        try:
            c = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
            return c.replace(tzinfo=timezone.utc).astimezone(TZ8)
        except Exception:
            return None

    hourly = []
    base = now8.replace(minute=0, second=0, microsecond=0)
    for h in range(23, -1, -1):
        t0 = base - timedelta(hours=h)
        t1 = t0 + timedelta(hours=1)
        n = 0
        for r in ok_all:
            c = _utc_to_8(key_time(r))
            if c and t0 <= c < t1:
                n += 1
        hourly.append({"label": t0.strftime("%H:00"), "n": n})

    # 循环状态
    loop = {"status": "unknown", "rounds": 0, "keys_session": 0,
            "last_round": "", "tail": []}
    try:
        out = subprocess.run(["supervisorctl", "status", "regloop"],
                             capture_output=True, text=True, timeout=8).stdout
        m = re.search(r"(RUNNING|FATAL|EXITED|STOPPED|BACKOFF)", out)
        loop["status"] = m.group(1) if m else out.strip()[:24]
    except Exception as ex:
        loop["status"] = f"err:{type(ex).__name__}"
    try:
        txt = open(LOG, encoding="utf-8", errors="replace").read()
        loop["rounds"] = len(re.findall(r"^\[round", txt, re.M))
        loop["keys_session"] = len(re.findall(r"api_key  = sk-", txt))
        mr = re.findall(r"^\[round \d+\] exit=\d+.*$", txt, re.M)
        loop["last_round"] = mr[-1] if mr else ""
        loop["tail"] = txt.strip().splitlines()[-30:]
    except Exception:
        pass

    return {
        "now": now8.strftime("%Y-%m-%d %H:%M:%S"),
        "today": today,
        "total_accounts": len(recs),
        "total_keys": len(ok_all),
        "today_keys": len(ok_today),
        "today_fail": len(fail_today),
        "fail_top": fail_top,
        "hourly": hourly,
        "loop": loop,
        "records": sorted(
            ok_all + fail_all,
            key=lambda r: (key_time(r) if r.get("api_key")
                           else fail_time(r)) or "", reverse=True)[:120],
    }


# ────────────────────────── 登录页 ──────────────────────────
LOGIN_PAGE = """<!doctype html><html lang=zh><meta charset=utf-8>
<title>登录 · Intern-Register 看板</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0f1420;--card:#1a2233;--line:#2a3450;--tx:#dbe4f5;--dim:#8494b5;
--ok:#31c48d;--bad:#f05252;--acc:#5a8df5}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);
font:14px/1.6 "Segoe UI",system-ui,sans-serif;
display:flex;align-items:center;justify-content:center;min-height:100vh}
.box{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:32px 34px;width:320px}
h1{font-size:18px;margin:0 0 4px;text-align:center}
.sub{color:var(--dim);font-size:12px;text-align:center;margin-bottom:20px}
input{width:100%;background:#0a0e18;border:1px solid var(--line);border-radius:8px;
color:var(--tx);padding:10px 12px;font-size:14px;margin-bottom:12px;outline:none}
input:focus{border-color:var(--acc)}
button{width:100%;background:var(--acc);color:#fff;border:0;border-radius:8px;
padding:10px;font-size:14px;cursor:pointer;font-weight:600}
button:hover{filter:brightness(1.1)}
#err{color:var(--bad);font-size:12.5px;min-height:18px;margin-bottom:8px;text-align:center}
</style>
<div class=box>
<h1>Intern-Register 看板</h1>
<div class=sub>请登录以继续</div>
<div id=err></div>
<form onsubmit="return go()">
 <input id=u placeholder=账号 autocomplete=username autofocus>
 <input id=p type=password placeholder=密码 autocomplete=current-password>
 <button type=submit>登 录</button>
</form>
</div>
<script>
function go(){
 const u=document.getElementById('u').value.trim();
 const p=document.getElementById('p').value;
 fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({u,p})}).then(r=>r.json()).then(d=>{
  if(d.ok){location.href='/'}else{
   document.getElementById('err').textContent=d.msg||'登录失败'}
 }).catch(e=>{document.getElementById('err').textContent='网络错误: '+e});
 return false;
}
</script></html>"""


# ────────────────────────── 页面 ──────────────────────────
PAGE = """<!doctype html><html lang=zh><meta charset=utf-8>
<title>Intern-Register 看板</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0f1420;--card:#1a2233;--line:#2a3450;--tx:#dbe4f5;--dim:#8494b5;
--ok:#31c48d;--bad:#f05252;--warn:#f9a83a;--acc:#5a8df5}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);
font:14px/1.6 "Segoe UI",system-ui,sans-serif;padding:18px}
h1{font-size:20px;margin:0 0 4px}
.sub{color:var(--dim);font-size:12px;margin-bottom:14px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
gap:10px;margin-bottom:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:12px 14px}
.card .k{color:var(--dim);font-size:12px}
.card .v{font-size:24px;font-weight:700;margin-top:2px}
.v.ok{color:var(--ok)} .v.bad{color:var(--bad)} .v.warn{color:var(--warn)}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:14px}
@media(max-width:760px){.row2{grid-template-columns:1fr}}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{color:var(--dim);text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);
position:sticky;top:0;background:var(--card)}
td{padding:6px 8px;border-bottom:1px solid var(--line);
white-space:nowrap;max-width:260px;overflow:hidden;text-overflow:ellipsis}
tr:hover td{background:#202b42}
.tag{display:inline-block;padding:1px 8px;border-radius:99px;font-size:11px}
.tag.ok{background:#123d2c;color:var(--ok)}
.tag.bad{background:#43181b;color:var(--bad)}
button{background:var(--acc);color:#fff;border:0;border-radius:6px;
padding:5px 12px;cursor:pointer;font-size:12px}
.bar{display:flex;align-items:flex-end;gap:3px;height:90px;margin-top:8px}
.bar i{flex:1;background:var(--acc);border-radius:3px 3px 0 0;min-height:2px;
position:relative}
.bar i b{position:absolute;bottom:-18px;left:50%;transform:translateX(-50%);
color:var(--dim);font-weight:400;font-size:10px;white-space:nowrap}
.bar i s{position:absolute;top:-16px;left:50%;transform:translateX(-50%);
text-decoration:none;font-size:10px;color:var(--tx)}
#log{background:#0a0e18;border:1px solid var(--line);border-radius:8px;
padding:10px;font:11.5px/1.5 Consolas,monospace;color:#9fb0d0;
height:240px;overflow:auto;white-space:pre-wrap;word-break:break-all}
.pill{font-size:12px;padding:2px 10px;border-radius:99px;border:1px solid var(--line)}
.pill.RUNNING{color:var(--ok);border-color:var(--ok)}
.pill.FATAL,.pill.EXITED{color:var(--bad);border-color:var(--bad)}
.copy{cursor:pointer;color:var(--acc)}
h2{font-size:14px;margin:18px 0 8px;color:var(--dim);font-weight:600}
</style>
<body>
<h1>Intern-Register 看板 <span id=loop class=pill>…</span>
<a href=/logout style="float:right;font-size:12px;color:var(--dim)">退出登录</a></h1>
<div class=sub>ir-qwenpaw-tom.xzxyuan.ccwu.cc · 每 <span id=cd>10</span>s 自动刷新 ·
<button onclick=load()>手动刷新</button> <span id=now class=sub></span></div>

<div class=grid>
 <div class=card><div class=k>今日 Key</div><div class=v ok id=k_today>–</div></div>
 <div class=card><div class=k>今日失败</div><div class=v bad id=k_fail>–</div></div>
 <div class=card><div class=k>累计 Key</div><div class=v id=k_total>–</div></div>
 <div class=card><div class=k>累计账号</div><div class=v id=k_acc>–</div></div>
 <div class=card><div class=k>循环轮次</div><div class=v id=k_round>–</div></div>
 <div class=card><div class=k>本次循环 Key</div><div class=v id=k_sess>–</div></div>
</div>

<div class=row2>
 <div class=card><div class=k>最近 24 小时产出（北京时间）</div>
   <div class=bar id=hours></div><div style="height:20px"></div></div>
 <div class=card><div class=k>失败类型 Top</div><div id=fails style="margin-top:6px">
   <span class=sub>…</span></div></div>
</div>

<h2>账号明细（最近 120 条 · 点 key 复制）</h2>
<div class=card style="padding:0;max-height:420px;overflow:auto">
<table id=tbl><thead><tr>
<th>时间</th><th>邮箱</th><th>用户名</th><th>uid</th><th>credits</th>
<th>API Key</th><th>注册出口IP</th><th>状态</th><th>耗时</th></tr></thead><tbody></tbody></table>
</div>

<h2>循环日志（tail 30）</h2>
<div id=log>…</div>

<script>
const $=s=>document.querySelector(s);
function esc(s){return (s??'').toString().replace(/[&<>"]/g,
 c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function fmt_ms(m){return m>=1000?(m/1000).toFixed(1)+'s':m+'ms'}
function copy(t,navigator.clipboard.writeText(t)){}
function load(){
 fetch('/api/summary').then(r=>{
  if(r.status===401){location.href='/login';throw new Error('登录已过期')}
  return r.json()
 }).then(d=>{
  $('#k_today').textContent=d.today_keys;
  $('#k_fail').textContent=d.today_fail;
  $('#k_total').textContent=d.total_keys;
  $('#k_acc').textContent=d.total_accounts;
  $('#k_round').textContent=d.loop.rounds;
  $('#k_sess').textContent=d.loop.keys_session;
  const lp=$('#loop');lp.textContent=d.loop.status;lp.className='pill '+d.loop.status;
  $('#now').textContent='更新于 '+d.now;
  $('#hours').innerHTML=d.hourly.map(h=>{
   const mx=Math.max(...d.hourly.map(x=>x.n),1);
   return `<i style="height:${Math.max(4,h.n/mx*70)}px" title="${h.label} → ${h.n}">`+
    (h.n?`<s>${h.n}</s>`:'')+`<b>${h.label.slice(0,2)}</b></i>`}).join('');
  $('#fails').innerHTML=d.fail_top.length?d.fail_top.map(f=>
   `<div style="display:flex;justify-content:space-between;margin:3px 0">
    <span>${esc(f[0])}</span><b style="color:var(--bad)">${f[1]}</b></div>`).join('')
   :'<span class=sub>无失败</span>';
  const tb=$('#tbl tbody');
  tb.innerHTML=d.records.map(r=>{
   const ok=!!r.api_key;
   return `<tr><td>${esc((r.created_at||'').slice(5,16))}</td>
   <td title="${esc(r.email)}">${esc(r.email)}</td>
   <td>${esc(r.username||'')}</td><td>${esc(r.sso_uid||'')}</td>
   <td>${r.credits!=null?Number(r.credits).toFixed(0):''}</td>
   <td class=copy title="点击复制" onclick="copy('${esc(r.api_key||'')}')">${
     ok?esc(r.api_key.slice(0,10))+'…'+esc(r.api_key.slice(-4)):'–'}</td>
   <td title="${esc(r.proxy_slot||'')}">${esc(r.proxy_ip||'–')}</td>
   <td><span class="tag ${ok?'ok':'bad'}">${ok?'success':esc(r.error_kind||r.status)}</span></td>
   <td>${r.timings&&r.timings.login_detail?'–':fmt_ms(r.timings?.register||0)}</td></tr>`}).join('');
  $('#log').textContent=(d.loop.tail||[]).join('\\n')||'(空)';
 }).catch(e=>{$('#now').textContent='加载失败: '+e});
}
copy=t=>{navigator.clipboard.writeText(t).then(()=>{},()=>{})};
load();let left=10;setInterval(()=>{left--;$('#cd').textContent=Math.max(left,0);
 if(left<=0){left=10;load()}},1000);
</script></html>"""


class H(BaseHTTPRequestHandler):
    def _session_ok(self) -> bool:
        """纯检查：Cookie 里的会话 token 是否有效（不写任何响应）。"""
        tok = ""
        for part in self.headers.get("Cookie", "").split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                if k.strip() == COOKIE_NAME:
                    tok = v.strip()
        return _check_session(tok)

    def _auth(self) -> bool:
        """受保护路由用：会话无效则拒绝（页面跳登录页；API 返回 401 JSON）。"""
        if self._session_ok():
            return True
        if urlparse(self.path).path == "/api/summary":
            self._send(401, json.dumps({"ok": False, "msg": "unauthorized"}),
                       "application/json; charset=utf-8")
        else:
            self._send(302, "", location="/login")
        return False

    def _send(self, code, body, ctype="text/html; charset=utf-8",
              set_cookie=None, location=None):
        b = body.encode()
        self.send_response(code)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()
        if b:
            self.wfile.write(b)

    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/healthz":                      # 无需登录，供隧道探活
            self._send(200, "ok", "text/plain")
            return
        if p == "/login":
            if self._session_ok():               # 已登录则直接进看板
                self._send(302, "", location="/")
            else:
                self._send(200, LOGIN_PAGE)      # 未登录：只发登录页，不再跳转
            return
        if p == "/logout":
            gone = f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
            self._send(302, "", set_cookie=gone, location="/login")
            return
        if not self._auth():
            return
        if p == "/api/summary":
            self._send(200, json.dumps(summarize(), ensure_ascii=False),
                       "application/json; charset=utf-8")
        elif p in ("/", "/index.html"):
            self._send(200, PAGE)
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        p = urlparse(self.path).path
        if p != "/login":
            self._send(404, "not found", "text/plain")
            return
        try:
            n = int(self.headers.get("Content-Length", "0") or 0)
            d = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            d = {}
        if d.get("u") == USER and d.get("p") == PASS:
            tok = _new_session()
            ck = (f"{COOKIE_NAME}={tok}; Path=/; Max-Age={SESSION_TTL}; "
                  "HttpOnly; SameSite=Lax")
            self._send(200, json.dumps({"ok": True}),
                       "application/json; charset=utf-8", set_cookie=ck)
        else:
            time.sleep(1.0)                      # 减缓爆破
            self._send(200, json.dumps({"ok": False, "msg": "账号或密码错误"}),
                       "application/json; charset=utf-8")

    def log_message(self, *a):   # 静默访问日志
        pass


if __name__ == "__main__":
    _load_sessions()
    print(f"dashboard on :{PORT}  user={USER}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
