# 判定实验：生产 browser_post 路径 + @olipii.com + 活槽出口 → 激活邮件到否？
# 收到  ⇒ 站方按收件域扣信（tanpony/gmeenramy 被拉黑，olipii 幸存）
#          ⇒ 修法 = IR_TEMPMAILIO_DOMAINS=olipii.com
# 收不到 ⇒ 槽位出口的注册被扣信 ⇒ 需换注册出口策略
import sys, time
sys.path.insert(0, ".")
from src import config
from src.sso import SSOClient
from src.pipeline import gen_password, gen_username
from src.tempmailio import TempMailIoClient
from src.proxypool import slot_alive

slot = None
t0 = time.time()
while time.time() - t0 < 240 and not slot:
    for s in config.proxy_slots():
        if slot_alive(s, timeout=5.0):
            slot = s
            break
    if not slot:
        time.sleep(45)
print("exit slot:", "OK" if slot else "NONE(结果作废)", flush=True)

mail = TempMailIoClient()
addr = mail.create_mailbox(domain="olipii.com")[0]
print("box:", addr, flush=True)

u, p = gen_username(), gen_password()
r = SSOClient(proxy=slot).register(u, addr, p)
print(f"register: ok={r.ok} uid={r.sso_uid}", flush=True)
if not r.ok:
    print("VERDICT: REG_FAIL")
    raise SystemExit

for i in range(15):                      # 180s
    try:
        ms = mail.list_mails(addr)
    except Exception:
        ms = []
    if ms:
        lk = ms[0].find_link("active", "activat", "verif", "confirm")
        print(f"MAIL! {ms[0].subject} link={'YES' if lk else 'no'}")
        print("VERDICT: MAIL_OK")
        break
    time.sleep(12)
else:
    print("VERDICT: MAIL_NONE")
