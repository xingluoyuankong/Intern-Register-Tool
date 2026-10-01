"""browser_post 诊断 v3：405 挑战页 → 页面上下文执行内联 JS → 重放 fetch。"""
import os
import re
import sys
import time

os.environ.setdefault("DISPLAY", ":1")
sys.path.insert(0, ".")
from playwright.sync_api import sync_playwright  # noqa: E402

from src import config  # noqa: E402
from src.crypto_rsa import encrypt_password  # noqa: E402

PROXY = "http://127.0.0.1:7901"
tag = str(int(time.time()))[-8:]
email = f"bp3test{tag}@hotmail.com"
payload = {
    "username": f"b3{tag[-6:]}",
    "email": email,
    "password": encrypt_password(email, "Watch!2026x"),
    "source": config.SOURCE,
    "clientId": config.CLIENT_ID,
}
PATH = "/gw/uaa-be/api/v1/register/byEmail"


def log(*a):
    print(f"[{time.time() % 1000:07.2f}]", *a, flush=True)


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

    def trim(route):
        if route.request.resource_type in ("document", "script", "xhr",
                                           "fetch"):
            route.continue_()
        else:
            route.abort()

    page.route("**/*", trim)
    t0 = time.time()
    page.goto(f"{config.SSO_BASE}/register",
              wait_until="domcontentloaded", timeout=30000)
    log(f"goto ok {time.time() - t0:.1f}s")
    page.wait_for_timeout(800)

    res = None
    for attempt in range(3):
        res = page.evaluate(JS, {"path": PATH, "payload": payload})
        log(f"try{attempt}: {res['status']} {res['text'][:60]!r}")
        if res["status"] != 405 or "acw_sc__v2" not in res["text"]:
            break
        m = re.search(r"<script>(.*?)</script>", res["text"], re.S)
        if not m:
            log("405 但无 <script>，放弃")
            break
        page.evaluate(m.group(1).replace(
            "document.location.reload()", "void 0;"))
        page.wait_for_timeout(300)
    log(f"final: {res['status']} {res['text'][:250]}")
    browser.close()
