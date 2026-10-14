"""验证 429 是否与**邮箱域名**有关：用真实临时邮箱（同 regloop）走代理注册。

regloop 与探针的 payload 完全一致，唯一差异是邮箱：
  - 探针用 `@example.com` → 成功
  - regloop 用真实 outlook 临时邮箱 → 429
若本测试（真实邮箱）也 429，则限流挂在**邮箱域名/邮箱源**上。
"""
import os
os.environ.setdefault("XDG_CACHE_HOME", "/app/working/intern-register/.cache")
os.environ.setdefault("DISPLAY", ":1")
import sys, time
sys.path.insert(0, "/app/working/intern-register")

from src.tempmail import build_mail_client
from src.waf_bypass import WafSolver

GW = "https://sso.openxlab.org.cn/gw/uaa-be/api/v1"
SLOTS = "/app/working/intern-register/.workbuddy-ai/proxypool/slots_active.txt"
slots = [l.strip() for l in open(SLOTS, encoding="utf-8") if l.strip()]

mail = build_mail_client()
try:
    emails = mail.create_mailbox(count=1)
    print(f"建箱: {emails}", flush=True)
    em = emails[0]
except Exception as e:
    print(f"建箱失败: {type(e).__name__}: {e}", flush=True)
    em = None

if em:
    import random, string
    from src.crypto_rsa import encrypt_password
    user = "zqx" + "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    payload = {"username": user, "email": em,
               "password": encrypt_password(em, "Aa11bb22!"),
               "source": "", "clientId": "dagw07mkg1bazlxzoy31"}
    t0 = time.time()
    try:
        res = WafSolver.get().browser_post(f"{GW}/register/byEmail", payload,
                                           proxy=slots[0])
        tx = str(res.get("text") or "")
        st = res.get("status")
        tag = "429" if (st == 429 or "too many" in tx.lower()) else "非429!"
        print(f"[{tag}] 真实邮箱注册: status={st} text={tx[:150]!r} "
              f"({round(time.time()-t0,1)}s)", flush=True)
    except Exception as e:
        print(f"[异常] {type(e).__name__}: {str(e)[:120]}", flush=True)

try:
    WafSolver.get().shutdown()
except Exception:
    pass
