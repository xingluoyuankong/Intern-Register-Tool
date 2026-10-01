"""有头 + mihomo 端口（不裁剪资源，等 networkidle）→ fetch 注册。"""
import os
import re
import sys
import time

os.environ.setdefault("DISPLAY", ":1")
sys.path.insert(0, ".")
from playwright.sync_api import sync_playwright  # noqa: E402

from src import config  # noqa: E402
from src.crypto_rsa import encrypt_password  # noqa: E402

PORT = sys.argv[1] if len(sys.argv) > 1 else "7901"
PROXY = f"http://127.0.0.1:{PORT}"
tag = str(int(time.time()))[-8:]
email = f"v4test{tag}@hotmail.com"
payload = {
    "username": f"v4{tag[-6:]}",
    "email": email,
    "password": encrypt_password(email, "Watch!2026x"),
    "source": config.SOURCE,
    "clientId": config.CLIENT_ID,
}
PATH = "/gw/uaa-be/api/v1/register/byEmail"


def log(*a):
    print(f"[{time.time() % 10000:07.2f}]", *a, flush=True)


JS = """async (args) => {
    const r = await fetch(args.path, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        credentials: 'include',
        body: JSON.stringify(args.payload),
    });
    return {status: r.status, text: await r.text()};
}"""

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=False,
        args=["--no-sandbox", "--disable-dev-shm-usage",
              "--disable-blink-features=AutomationControlled"])
    ctx = browser.new_context(proxy={"server": PROXY})
    page = ctx.new_page()
    t0 = time.time()
    page.goto(f"{config.SSO_BASE}/register", timeout=45000,
              wait_until="domcontentloaded")
    log(f"goto(dcl) {time.time() - t0:.1f}s")
    page.wait_for_timeout(12000)
    log("cookies:", sorted(c["name"] for c in ctx.cookies()))
    res = page.evaluate(JS, {"path": PATH, "payload": payload})
    log(f"fetch1 -> {res['status']} {res['text'][:80]!r}")
    if res["status"] == 405 or "acw_sc__v2" in res["text"]:
        m = re.search(r"<script>(.*?)</script>", res["text"], re.S)
        if m:
            page.evaluate(m.group(1).replace(
                "document.location.reload()", "void 0;"))
            page.wait_for_timeout(500)
            res = page.evaluate(JS, {"path": PATH, "payload": payload})
            log(f"fetch2 -> {res['status']} {res['text'][:200]!r}")
    browser.close()
