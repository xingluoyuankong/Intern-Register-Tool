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
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

def _resolve_root() -> str:
    """项目根目录：优先本文件所在项目（/app/working/intern-register），
    回退 /root/intern-register（老路径，平台 overlay 会重置，别依赖）。"""
    here = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))          # .../intern-register
    if os.path.isdir(os.path.join(here, "ledger")):
        return here
    return "/root/intern-register"


ROOT = _resolve_root()
LOG = os.path.join(ROOT, "logs", "loop.log")
USER = os.getenv("IR_DASH_USER", "admin")
PASS = os.getenv("IR_DASH_PASS", "irpanel")
TZ8 = timezone(timedelta(hours=8))          # 面板按北京时间显示
PORT = int(os.getenv("IR_DASH_PORT", "8000"))

# 增量日志读取的状态（offset + 累计计数 + 最近行缓存）
_LOG_STATE = {"off": 0, "rounds": 0, "keys": 0, "lines": [],
              "t1_ok": 0, "t1_rej": 0, "e429": 0}
_LOG_LOCK = threading.Lock()

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
# ── 增量台账缓存（2026-10-18）──────────────────────────────────────
# 🔴 旧实现每 15s glob+解析**全部**快照（上千份 × ~2MB）→ CPU 常 20%+。
#    快照是**累积式**的（最新=全量合并，见 run.py --out）且只增不改，
#    所以只解析「上次没见过的新文件」，按文件名时间序让新记录覆盖旧记录。
_REC_CACHE = {"files": set(), "recs": {}, "t": 0.0}
_REC_LOCK = threading.Lock()


def load_records():
    """增量扫描 runs 快照，按 email 去重（新文件覆盖旧文件的记录）。

    首次调用解析全部（冷启动一次，可接受）；此后每轮只解析新增快照 ——
    每轮 1~2 份、每份只读一次。结果缓存在进程内，key 语义与旧实现一致。
    """
    files = sorted(glob.glob(os.path.join(
        ROOT, "ledger", "runs", "*", "*.json")))
    with _REC_LOCK:
        fresh = [f for f in files
                 if f not in _REC_CACHE["files"]]
        for f in fresh:
            try:
                d = json.load(open(f, encoding="utf-8"))
            except Exception:
                continue
            lst = d if isinstance(d, list) else (
                d.get("records") if isinstance(d, dict) else None) or []
            for r in lst:
                if not isinstance(r, dict):
                    continue
                em = r.get("email")
                if not em:
                    continue
                # 快照按文件名时间序增量读入，后读的天然覆盖先读的；
                # 有 key 的优先（收割流程会先写失败再补成功）
                old = _REC_CACHE["recs"].get(em)
                if old is None or not old.get("api_key") or r.get("api_key"):
                    _REC_CACHE["recs"][em] = dict(r)
            _REC_CACHE["files"].add(f)
        return list(_REC_CACHE["recs"].values())



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

    # 🔴 2026-10-18：**注册情况**核心指标 —— 光看 Key 数看不出卡点在哪一段。
    #    分阶段漏斗（今日，北京时间）：注册成功 → 登录成功 → 建 key。
    #    · 注册成功 = stages.register == "ok"（比 status 更细：登录挂了也算注册成）
    #    · 登录成功 = 登录链路拿到过 jwt（字段存在即算）
    reg_today = [r for r in recs
                 if (r.get("stages") or {}).get("register") == "ok"
                 and fail_time8(r).startswith(today)]
    act_today = [r for r in reg_today
                 if (r.get("stages") or {}).get("activate") == "ok"]
    login_today = [r for r in reg_today if r.get("jwt")]
    key_today = [r for r in reg_today if r.get("api_key")]

    # 邮箱域健康（今日注册数 / 其中 key 数）—— 域名被 WAF/发信侧拉黑
    # （hotmail 滑块、temp-mail.io 激活邮件停发）时在面板一眼可见
    dom = {}
    for r in reg_today:
        d = str(r.get("email") or "?").split("@")[-1]
        c = dom.setdefault(d, {"d": d, "reg": 0, "key": 0})
        c["reg"] += 1
        if r.get("api_key"):
            c["key"] += 1
    domains = sorted(dom.values(), key=lambda x: -x["reg"])[:8]

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
            "last_round": "", "tail": [], "pool": "?", "sync": "",
            "t1_ok": 0, "t1_rej": 0, "e429": 0, "cur_round": "",
            "quota": "?"}
    # 🔴 2026-10-17：业务进程由**独立 supervisord 实例**托管
    #    （/etc/supervisord-ir.conf，见 deploy/supervisor/），默认 socket
    #    /var/run/supervisor.sock 在容器重建后不存在 → 面板状态会显示成
    #    "unix:///var/run/supervis..." 这种报错串。按候选顺序取第一个可用的。
    _sup_cmd = ["supervisorctl"]
    for _c in ("/etc/supervisord-ir.conf",
               "/etc/supervisor/conf.d/supervisord.conf"):
        if os.path.exists(_c):
            _sup_cmd += ["-c", _c]
            break
    try:
        out = subprocess.run(_sup_cmd + ["status", "regloop"],
                             capture_output=True, text=True, timeout=8).stdout
        m = re.search(r"(RUNNING|FATAL|EXITED|STOPPED|BACKOFF)", out)
        loop["status"] = m.group(1) if m else (out.strip()[:24] or "unknown")
    except Exception as ex:
        loop["status"] = f"err:{type(ex).__name__}"
    # 🔴 2026-10-18：日志改**增量读**。旧实现每 15s open().read() 整个
    #    loop.log（几十 MB、append-only）再全量正则 —— 面板进程 CPU 常驻
    #    20%+ 的元凶。现在只读上次 offset 之后的新增字节，计数器累计。
    with _LOG_LOCK:
        try:
            size = os.path.getsize(LOG)
            off = _LOG_STATE["off"]
            chunk = b""
            if size < off:            # 日志被轮转/清空 → 从头再来
                off, _LOG_STATE["rounds"] = 0, 0
                _LOG_STATE["keys"] = 0
            with open(LOG, "rb") as f:
                if size > off:
                    f.seek(off)
                    chunk = f.read()
                _LOG_STATE["off"] = size
            txt = chunk.decode("utf-8", errors="replace")
            _LOG_STATE["rounds"] += len(re.findall(r"^\[round", txt, re.M))
            _LOG_STATE["keys"] += len(re.findall(r"api_key  = sk-", txt))
            # 🔴 2026-10-20 注册现场：T001 放行/拒绝、429 限流（会话累计，
            #    看板重启归零——口径标注"会话累计"避免误读为全天）
            _LOG_STATE["t1_ok"] += len(re.findall(r"✓ 登录", txt))
            _LOG_STATE["t1_rej"] += len(re.findall(r"captcha rejected", txt))
            _LOG_STATE["e429"] += len(
                re.findall(r"status=429|Too many request", txt))
            # 🔴 2026-10-20 修复：最近轮/出口池从**整块增量**里取（此前只扫
            #    末 50 行，轮次一长标记行早被冲掉 → 永远显示 ? 和 –）
            if "个槽位出口可达" in txt:
                _LOG_STATE["pool_line"] = [
                    ln for ln in txt.splitlines()
                    if "个槽位出口可达" in ln][-1]
            if "] ① exit=" in txt or "] ② exit=" in txt:
                _LOG_STATE["exit_line"] = [
                    ln for ln in txt.splitlines()
                    if re.match(r"^\[round \d+\] [①②] exit=", ln)][-1]
            if txt:
                _LOG_STATE["lines"].extend(txt.strip().splitlines())
                del _LOG_STATE["lines"][:-50]        # 只留最近 50 行
            # 🔴 2026-10-20：旧实现只扫末 50 行（轮次一长标记行被冲掉 →
            #    last_round 恒空、pool 恒 ?）。改由上方增量块内的
            #    pool_line / exit_line 缓存提供（见 summarize 尾部赋值）。
        except Exception:
            pass
        loop["rounds"] = _LOG_STATE["rounds"]
        loop["keys_session"] = _LOG_STATE["keys"]
        loop["tail"] = list(_LOG_STATE["lines"])[-30:]
        # 🔴 2026-10-20：最近轮/出口池改为从增量块内缓存行取（不冲掉）
        _pl = _LOG_STATE.get("pool_line", "")
        if _pl:
            m2 = re.search(r"(\d+)/(\d+) 个槽位", _pl)
            loop["pool"] = f"{m2.group(1)}/{m2.group(2)}" if m2 else "?"
        loop["last_round"] = _LOG_STATE.get("exit_line", "")
        # 🔴 2026-10-20 当前轮进度：最近 ROUND 头 + 最近 [x/N] 行
        try:
            rh, stage = "", ""
            for ln in _LOG_STATE["lines"]:
                m = re.match(r"^#+ ROUND (\d+)\s+(\S+ \S+)", ln)
                if m:
                    rh = f"R{m.group(1)} {m.group(2)[11:16]}"
                m2 = re.match(r"^\[(\d+)/(\d+)\]", ln)
                if m2:
                    stage = f"[{m2.group(1)}/{m2.group(2)}]"
            loop["cur_round"] = (rh + " " + stage).strip()
        except Exception:
            pass
        loop["t1_ok"] = _LOG_STATE["t1_ok"]
        loop["t1_rej"] = _LOG_STATE["t1_rej"]
        loop["e429"] = _LOG_STATE["e429"]
    # 🔴 2026-10-20 配额现场：直读 register_quota.jsonl（24h 滚动窗口）
    try:
        qp = os.path.join(ROOT, ".workbuddy-ai", "state",
                          "register_quota.jsonl")
        cutoff = time.time() - 24 * 3600
        qn = 0
        with open(qp, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    if json.loads(line).get("ts", 0) >= cutoff:
                        qn += 1
                except Exception:
                    pass
        qlim = 400
        with open(os.path.join(ROOT, ".env"), encoding="utf-8",
                  errors="replace") as f:
            for ln in f:
                m = re.match(r"^IR_REG_QUOTA_MAX\s*=\s*(\d+)", ln.strip())
                if m:
                    qlim = int(m.group(1))
        loop["quota"] = f"{qn}/{qlim}"
    except Exception:
        loop["quota"] = "?"
    # key-sync 最近一行（小文件，直接读尾部省事）
    try:
        sl = open(os.path.join(ROOT, "logs", "key_sync.log"),
                  encoding="utf-8", errors="replace").read().strip().splitlines()
        loop["sync"] = sl[-1] if sl else ""
    except Exception:
        loop["sync"] = ""

    return {
        "now": now8.strftime("%Y-%m-%d %H:%M:%S"),
        "today": today,
        "total_accounts": len(recs),
        "total_keys": len(ok_all),
        "today_keys": len(ok_today),
        "today_fail": len(fail_today),
        "today_reg": len(reg_today),
        "funnel": {"reg": len(reg_today), "act": len(act_today),
                   "login": len(login_today), "key": len(key_today)},
        "domains": domains,
        "fail_top": fail_top,
        "hourly": hourly,
        "loop": loop,
        "records": sorted(
            ok_all + fail_all,
            key=lambda r: (key_time(r) if r.get("api_key")
                           else fail_time(r)) or "", reverse=True)[:60],
    }


# ── 聚合缓存（后台线程刷新）──────────────────────────────────────
# 🔴 2026-10-14 实测：`summarize()` 每次请求都要 glob + 解析 `ledger/runs/**/*.json`
#    全部快照。快照最多时上千份、每份 ~2MB → 单次响应可达**数十秒**
#    → 浏览器/Cloudflare 超时 → 前端整页显示 "–"（就是"面板全 –"的真因，
#    不是接口挂、也不是 JS 语法）。改为**后台线程定时重算**，API 只读缓存，
#    响应恒定毫秒级。
_SUM_CACHE = {"t": 0.0, "v": None, "err": ""}
_SUM_LOCK = threading.Lock()
SUM_REFRESH = 15.0


def _refresh_loop():
    while True:
        try:
            v = summarize()
            with _SUM_LOCK:
                _SUM_CACHE["v"] = v
                _SUM_CACHE["t"] = time.time()
                _SUM_CACHE["err"] = ""
        except Exception as ex:                       # noqa: BLE001
            with _SUM_LOCK:
                _SUM_CACHE["err"] = f"{type(ex).__name__}: {ex}"[:200]
        time.sleep(SUM_REFRESH)


def summarize_cached():
    """立即返回缓存（不阻塞）；首轮还没算完时给个 warming 占位。"""
    with _SUM_LOCK:
        if _SUM_CACHE["v"] is not None:
            return _SUM_CACHE["v"]
        err = _SUM_CACHE["err"]
    now8 = datetime.now(TZ8)
    return {
        "now": now8.strftime("%Y-%m-%d %H:%M:%S"),
        "today": now8.strftime("%Y-%m-%d"),
        "total_accounts": 0, "total_keys": 0, "today_keys": 0,
        "today_fail": 0, "fail_top": [], "hourly": [], "records": [],
        "loop": {"status": "warming" if not err else "err",
                 "rounds": 0, "keys_session": 0, "last_round": "",
                 "tail": (["[面板] 正在首次聚合台账，请稍候（约十几秒）…"]
                          if not err else [f"[面板] 聚合失败：{err}"])},
    }


# ────────────────────────── 登录页 ──────────────────────────
LOGIN_PAGE = """<!doctype html><html lang=zh><meta charset=utf-8>
<title>登录 · Intern-Register 看板</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
/* 🔴 2026-10-20 白色主题（与主页一致） */
:root{--bg:#f4f6fa;--card:#ffffff;--line:#e5e9f0;--tx:#182230;--dim:#67788e;
--ok:#0e9f6e;--bad:#e02424;--acc:#2f6fed}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);
font:14px/1.6 "Segoe UI",system-ui,sans-serif;
display:flex;align-items:center;justify-content:center;min-height:100vh}
.box{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:32px 34px;width:320px;box-shadow:0 4px 16px rgba(16,24,40,.08)}
h1{font-size:18px;margin:0 0 4px;text-align:center}
.sub{color:var(--dim);font-size:12px;text-align:center;margin-bottom:20px}
input{width:100%;background:#fbfcfe;border:1px solid var(--line);border-radius:8px;
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
/* 🔴 2026-10-20 白色主题重构：浅底深字，卡片分区（KPI / 实时状态 / 图表） */
:root{--bg:#f4f6fa;--card:#ffffff;--line:#e5e9f0;--tx:#182230;--dim:#67788e;
--ok:#0e9f6e;--bad:#e02424;--warn:#c27803;--acc:#2f6fed}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);
font:14px/1.6 "Segoe UI",system-ui,sans-serif;padding:18px}
h1{font-size:20px;margin:0 0 4px}
.sub{color:var(--dim);font-size:12px;margin-bottom:14px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
gap:10px;margin-bottom:10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:12px 14px;box-shadow:0 1px 3px rgba(16,24,40,.05)}
.card .k{color:var(--dim);font-size:12px}
.card .v{font-size:26px;font-weight:700;margin-top:2px}
.v.ok{color:var(--ok)} .v.bad{color:var(--bad)} .v.warn{color:var(--warn)}
.chiprow{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
.chip{background:#f1f5fb;border:1px solid var(--line);border-radius:99px;
padding:1px 9px;font-size:11px;color:var(--dim)}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:12px}
@media(max-width:760px){.row2{grid-template-columns:1fr}}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{color:var(--dim);text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);
position:sticky;top:0;background:var(--card)}
td{padding:6px 8px;border-bottom:1px solid var(--line);
white-space:nowrap;max-width:260px;overflow:hidden;text-overflow:ellipsis}
tr:hover td{background:#f6f9fd}
.tag{display:inline-block;padding:1px 8px;border-radius:99px;font-size:11px}
.tag.ok{background:#e7f8f1;color:var(--ok)}
.tag.bad{background:#fdeded;color:var(--bad)}
button{background:var(--acc);color:#fff;border:0;border-radius:6px;
padding:5px 12px;cursor:pointer;font-size:12px}
button:hover{filter:brightness(1.08)}
.bar{display:flex;align-items:flex-end;gap:3px;height:90px;margin-top:8px}
.bar i{flex:1;background:var(--acc);border-radius:3px 3px 0 0;min-height:2px;
position:relative;opacity:.85}
.bar i b{position:absolute;bottom:-18px;left:50%;transform:translateX(-50%);
color:var(--dim);font-weight:400;font-size:10px;white-space:nowrap}
.bar i s{position:absolute;top:-16px;left:50%;transform:translateX(-50%);
text-decoration:none;font-size:10px;color:var(--tx)}
#log{background:#fbfcfe;border:1px solid var(--line);border-radius:8px;
padding:10px;font:11.5px/1.5 Consolas,monospace;color:#3a4a60;
height:240px;overflow:auto;white-space:pre-wrap;word-break:break-all}
.pill{font-size:12px;padding:2px 10px;border-radius:99px;border:1px solid var(--line)}
.pill.RUNNING{color:var(--ok);border-color:var(--ok)}
.pill.FATAL,.pill.EXITED{color:var(--bad);border-color:var(--bad)}
.copy{cursor:pointer;color:var(--acc)}
h2{font-size:14px;margin:16px 0 8px;color:var(--dim);font-weight:600}
</style>
<body>
<h1>Intern-Register 看板 <span id=loop class=pill>…</span>
<a href=/logout style="float:right;font-size:12px;color:var(--dim)">退出登录</a></h1>
<div class=sub>ir-qwenpaw-tom.xzxyuan.ccwu.cc · 每 <span id=cd>10</span>s 自动刷新 ·
<button onclick=load()>手动刷新</button> <span id=now class=sub></span></div>

<div class=grid>
 <div class=card><div class=k>今日注册</div><div class=v id=k_reg>–</div>
   <div class=k id=k_funnel style="margin-top:2px">漏斗：–</div></div>
 <div class=card><div class=k>今日激活</div><div class=v id=k_act>–</div>
   <div class=chiprow><span class=chip>激活率 = 激活/注册</span></div></div>
 <div class=card><div class=k>今日 Key</div><div class=v ok id=k_today>–</div></div>
 <div class=card><div class=k>今日失败</div><div class=v bad id=k_fail>–</div></div>
 <div class=card><div class=k>累计 Key</div><div class=v id=k_total>–</div>
   <div class=k style="margin-top:2px"><span id=k_acct>–</span> 累计账号</div></div>
</div>

<h2>实时现场（北京时间）</h2>
<div class=grid>
 <div class=card><div class=k>当前轮 · 出口池</div><div class=v id=k_curr
   style="font-size:18px">–</div>
   <div class=chiprow><span class=chip id=k_pool>–</span>
   <span class=chip id=k_lastround>–</span></div></div>
 <div class=card><div class=k>注册现场（会话累计）</div><div class=v
   style="font-size:15px"><span id=k_t1>–</span></div>
   <div class=chiprow><span class=chip id=k_quota>–</span>
   <span class=chip id=k_sync style="max-width:280px;overflow:hidden;
   text-overflow:ellipsis;white-space:nowrap">–</span></div></div>
</div>

<div class=row2>
 <div class=card><div class=k>最近 24 小时产出（北京时间）</div>
   <div class=bar id=hours></div><div style="height:20px"></div></div>
 <div class=card><div class=k>失败类型 Top</div><div id=fails style="margin-top:6px">
   <span class=sub>…</span></div></div>
</div>

<div class=row2>
 <div class=card><div class=k>邮箱域健康（今日注册 → 建 key）</div>
   <table style="margin-top:6px"><thead><tr>
   <th>域名</th><th>注册</th><th>Key</th><th>转化</th></tr></thead>
   <tbody id=domb></tbody></table></div>
 <div class=card><div class=k>运行统计（面板会话 · 日志累计口径）</div>
   <div style="display:flex;gap:24px;margin-top:8px">
   <div><div class=v id=k_round style="font-size:20px">–</div><div class=k>轮次</div></div>
   <div><div class=v id=k_sess style="font-size:20px">–</div><div class=k>Key 行</div></div>
   <div><div class=v id=k_acc style="font-size:20px">–</div><div class=k>累计账号</div></div></div>
   <div class=k id=k_lastround style="margin-top:10px;font-size:11.5px">–</div></div>
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
function copy(t){navigator.clipboard.writeText(t)}
let failStreak=0;
function load(tries){
 tries=tries||0;
 fetch('/api/summary',{cache:'no-store'}).then(r=>{
  if(r.status===401){location.href='/login';throw new Error('登录已过期')}
  if(!r.ok){throw new Error('HTTP '+r.status)}
  return r.json()
 }).then(d=>{
  failStreak=0;
  $('#k_today').textContent=d.today_keys;
  $('#k_fail').textContent=d.today_fail;
  $('#k_total').textContent=d.total_keys;
  $('#k_reg').textContent=d.today_reg;
  const f=d.funnel||{};
  $('#k_funnel').textContent=`漏斗：注册 ${f.reg??0} → 激活 ${f.act??0} → 登录 ${f.login??0} → key ${f.key??0}`;
  $('#k_act').textContent=f.act??0;
  $('#k_acct').textContent=d.total_accounts;
  $('#k_curr').textContent='轮 '+((d.loop.cur_round||'–'));
  $('#k_pool').textContent='出口池 '+(d.loop.pool||'?');
  const L=d.loop||{};
  const t1ok=L.t1_ok??0, t1rj=L.t1_rej??0;
  const t1col=t1ok>0?(t1rj===0?'var(--ok)':'var(--warn)'):'var(--dim)';
  $('#k_t1').innerHTML=`T001 <span style="color:${t1col}">放行 ${t1ok}</span> / 拒 ${t1rj} · 429×${L.e429??0}`;
  $('#k_quota').textContent=`注册配额(24h) ${L.quota??'?'}`;
  const sync=(d.loop.sync||'');
  $('#k_sync').textContent=sync?('GitHub: '+sync.slice(-52)):'GitHub: 还没跑过';
  $('#k_sync').style.color=/✅|无变化/.test(sync)?'var(--ok)':(/⚠|失败/.test(sync)?'var(--bad)':'var(--dim)');
  $('#k_lastround').textContent='最近轮：'+(d.loop.last_round||'–');
  const db=$('#domb');
  db.innerHTML=(d.domains&&d.domains.length)?d.domains.map(x=>{
   const pct=x.key&&x.reg?Math.round(x.key/x.reg*100):0;
   const col=pct>=80?'var(--ok)':(pct>=40?'var(--warn)':'var(--bad)');
   return `<tr><td>${esc(x.d)}</td><td>${x.reg}</td>
   <td style="color:${col}">${x.key}</td><td>${pct}%</td></tr>`}).join('')
   :'<tr><td colspan=4 class=sub>今日暂无</td></tr>';
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
 }).catch(e=>{
  const m=String((e&&e.message)||e);
  if(m.indexOf('登录已过期')>=0) return;
  failStreak++;
  if(tries<2){setTimeout(()=>load(tries+1),1500);return}
  $('#now').innerHTML='<span style="color:var(--bad)">取数失败×'+failStreak+'：'+m+'</span>';
 });
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
            self._send(200, json.dumps(summarize_cached(), ensure_ascii=False),
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
    threading.Thread(target=_refresh_loop, daemon=True).start()
    print(f"dashboard on :{PORT}  user={USER}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
