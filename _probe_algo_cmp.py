"""对比：浏览器 JS 算出的 acw_sc__v2 vs python 算法（同一 arg1）。"""
import os
import re
import sys
import time

os.environ.setdefault("DISPLAY", ":1")
sys.path.insert(0, ".")
from playwright.sync_api import sync_playwright  # noqa: E402

from src.sso import SSOClient  # noqa: E402

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=False,
        args=["--no-sandbox", "--disable-dev-shm-usage"])
    ctx = browser.new_context()
    page = ctx.new_page()

    got = {}
    arg1_holder = {}

    def on_resp(resp):
        if "sso.openxlab.org.cn" in resp.url and resp.status == 200:
            try:
                body = resp.text()
            except Exception:
                return
            m = re.search(r"arg1='([0-9A-Fa-f]{40})'", body)
            if m:
                arg1_holder["arg1"] = m.group(1)

    page.on("response", on_resp)
    page.goto("https://sso.openxlab.org.cn/register",
              wait_until="domcontentloaded", timeout=30000)
    deadline = time.time() + 20
    while time.time() < deadline:
        got = {c["name"]: c["value"] for c in ctx.cookies()}
        if "acw_sc__v2" in got and "arg1" in arg1_holder:
            break
        page.wait_for_timeout(500)

    browser_js = got.get("acw_sc__v2", "")
    arg1 = arg1_holder.get("arg1", "")
    py_js = SSOClient._acw_sc_v2(arg1) if arg1 else "(no arg1)"
    print("arg1        :", arg1, flush=True)
    print("browser calc:", browser_js, flush=True)
    print("python calc :", py_js, flush=True)
    print("MATCH       :", browser_js == py_js and bool(browser_js), flush=True)
    print("all cookies :", sorted(got), flush=True)
    browser.close()
