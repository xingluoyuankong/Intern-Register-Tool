"""有头 Chromium on Xvfb 的 browser_post 验证（headless 特征被 WAF 405 后的对策）。"""
import os
import sys
import time

os.environ.setdefault("DISPLAY", ":1")
sys.path.insert(0, ".")
from playwright.sync_api import sync_playwright  # noqa: E402

from src import config  # noqa: E402
from src.crypto_rsa import encrypt_password  # noqa: E402

tag = str(int(time.time()))[-8:]
email = f"headful{tag}@hotmail.com"
payload = {
    "username": f"hf{tag[-6:]}",
    "email": email,
    "password": encrypt_password(email, "Watch!2026x"),
    "source": config.SOURCE,
    "clientId": config.CLIENT_ID,
}

t0 = time.time()
with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=False,
        args=["--no-sandbox", "--disable-dev-shm-usage",
              "--disable-blink-features=AutomationControlled"])
    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto(f"{config.SSO_BASE}/register", wait_until="load", timeout=30000)
    deadline = time.time() + 25
    while time.time() < deadline:
        names = {c["name"] for c in ctx.cookies()}
        if "acw_sc__v2" in names:
            break
        page.wait_for_timeout(500)
    page.wait_for_timeout(800)
    js = """async (args) => {
        const r = await fetch(args.path, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            credentials: 'include',
            body: JSON.stringify(args.payload),
        });
        return {status: r.status, text: await r.text()};
    }"""
    res = page.evaluate(js, {
        "path": "/gw/uaa-be/api/v1/register/byEmail", "payload": payload})
    print(f"[{time.time() - t0:.1f}s] status={res['status']} "
          f"body={res['text'][:250]}", flush=True)
    browser.close()
