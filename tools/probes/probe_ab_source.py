# A/B 定位：fetch 注册路径（与生产 browser_post 同款）+ source/clientId 清空。
# 邮件到 ⇒ 最小修复 = 把 IR_SOURCE/IR_CLIENT_ID 置空（一行配置）。
# 邮件不到 ⇒ 最小修复 = 生产注册换 UI 全流程（探针已验证可行）。

import os
import sys
import time

sys.path.insert(0, "/app/working/intern-register")
os.chdir("/app/working/intern-register")

import src.config as config                                   # noqa: E402

# ── A 组变量：清空来源 ──────────────────────────────────────────
config.SOURCE = ""
config.CLIENT_ID = ""

from src.sso import SSOClient                                 # noqa: E402
from src.pipeline import gen_password, gen_username           # noqa: E402
from src.tempmailio import TempMailIoClient                   # noqa: E402


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


# 0) 先把 UI 探针注册的号激活掉，验证激活链接可用（全链路闭环）
LINK = ("https://sso.openxlab.org.cn/active?token=DxEIwBjXQZ"
        "&sign=043c46a36621ccbf1bdc4853e653f1fb925c5c1df1ee3e9dfadac104c5e2497d")
try:
    ok = SSOClient().activate_from_url(LINK)
    log(f"UI探针账号激活: {ok}")
except Exception as e:
    log(f"UI探针账号激活异常: {type(e).__name__} {str(e)[:120]}")

# 1) A/B：fetch 路径 + 空 source/clientId
mail = TempMailIoClient()
addr = mail.create_mailbox()[0]
u, p = gen_username(), gen_password()
log(f"A/B probe: {u} {addr}")
s = SSOClient()
reg = s.register(u, addr, p)
log(f"register ok={getattr(reg, 'ok', None)} uid={getattr(reg, 'sso_uid', None)} "
    f"msg={getattr(reg, 'msg_code', None)}/{getattr(reg, 'msg', None)}")

if getattr(reg, "ok", False):
    m = mail.wait_for_mail(addr, timeout=180, interval=5)
    if m:
        log(f"MAIL! from={m.from_address} subj={m.subject}")
        print("VERDICT: MAIL_OK")
    else:
        log(f"no mail (polls={mail.last_polls})")
        print("VERDICT: MAIL_NONE")
else:
    print("VERDICT: REG_FAIL")
