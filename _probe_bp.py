"""单测 browser_post：打印真实异常与响应。"""
import sys
import time

sys.path.insert(0, ".")
from src import config  # noqa: E402
from src.crypto_rsa import encrypt_password  # noqa: E402
from src.waf_bypass import WafSolver  # noqa: E402

tag = str(int(time.time()))[-8:]
email = f"bptest{tag}@hotmail.com"
payload = {
    "username": f"bp{tag[-6:]}",
    "email": email,
    "password": encrypt_password(email, "Watch!2026x"),
    "source": config.SOURCE,
    "clientId": config.CLIENT_ID,
}
t0 = time.time()
try:
    res = WafSolver.get().browser_post(
        "/gw/uaa-be/api/v1/register/byEmail", payload,
        proxy="http://Link.rl2.QXpOVU5qNFJYVlRYVVRmVdKh1usrOuYf:@resin-proxy.xzxyuan.ccwu.cc:2268")
    print(f"[{time.time() - t0:.1f}s] status={res.get('status')} "
          f"body={res.get('text', '')[:250]}", flush=True)
except Exception as ex:  # noqa: BLE001
    print(f"[{time.time() - t0:.1f}s] EXC: {type(ex).__name__}: "
          f"{str(ex)[:300]}", flush=True)
