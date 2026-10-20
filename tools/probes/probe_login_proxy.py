# 实验：登录走新鲜槽位 IP（历史上从未试过——此前一直服务器直连登录）。
# T001 拒绝若与直连 IP 绑定 ⇒ 换槽位立刻出 key ⇒ 猎手/生产登录全改代理模式。
# T001 仍拒 ⇒ 排除 IP 因素，坐实行为/时间窗口。
import sys, time
sys.path.insert(0, ".")
from src import config
from src.proxypool import slot_alive
from src.browser import login as browser_login

slot = None
t0 = time.time()
while time.time() - t0 < 240 and not slot:
    for s in config.proxy_slots():
        if slot_alive(s, timeout=5.0):
            slot = s
            break
    if not slot:
        time.sleep(45)
print("login slot:", "OK" if slot else "NONE(作废)", flush=True)

EMAIL, PWD = "ACCOUNT@EXAMPLE.COM", None
import json
d = json.load(open("ledger/latest.json"))
recs = d if isinstance(d, list) else d.get("records") or []
for x in recs:
    if (x.get("email") or "").lower() == EMAIL:
        PWD = x.get("password") or x.get("pwd")
        break
assert PWD, "账号密码没找到"
print("account:", EMAIL, "pwlen", len(PWD), flush=True)

t1 = time.time()
res = browser_login(EMAIL, PWD, headless=True, attempts=1,
                    proxy=slot, verbose=True)
print(f"elapsed {time.time()-t1:.0f}s ok={res.ok} "
      f"reason={getattr(res,'reason',None)} jwt={'YES' if res.jwt else 'no'} "
      f"captcha={getattr(res,'captcha_stage',None)}", flush=True)
print("VERDICT:", "LOGIN_OK_PROXY" if res.ok else "LOGIN_REJECTED_PROXY")
