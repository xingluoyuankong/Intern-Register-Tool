"""诊断：playwright 浏览器走 Resin 代理能否打开最简单的 https 页面。"""
import os
import sys
import time

os.environ.setdefault("DISPLAY", ":1")
sys.path.insert(0, ".")
from playwright.sync_api import sync_playwright  # noqa: E402

PROXY = "http://Link.rl2.QXpOVU5qNFJYVlRYVVRmVdKh1usrOuYf:@resin-proxy.xzxyuan.ccwu.cc:2268"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False, args=["--no-sandbox", "--disable-dev-shm-usage"])
    for label, kw in [("with-proxy", {"proxy": {"server": PROXY}}),
                      ("direct", {})]:
        t0 = time.time()
        ctx = browser.new_context(**kw)
        page = ctx.new_page()
        try:
            page.goto("https://api.ipify.org?format=json",
                      timeout=20000, wait_until="domcontentloaded")
            print(f"[{label}] {time.time() - t0:.1f}s body={page.content()[:120]!r}",
                  flush=True)
        except Exception as ex:  # noqa: BLE001
            print(f"[{label}] {time.time() - t0:.1f}s FAIL: "
                  f"{type(ex).__name__}: {str(ex)[:120]}", flush=True)
        finally:
            try:
                ctx.close()
            except Exception:
                pass
    browser.close()
