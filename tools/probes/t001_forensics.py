#!/usr/bin/env python3
"""T001 现场取证（2026-10-18）：跑一次登录，verbose 全开 + DOM/请求/截图。

用法：
    DISPLAY=:1 .venv/bin/python -u tools/probes/t001_forensics.py [邮箱]

抓什么：
  1. 登录逐阶段事件（[login] 行：init count / click# / captcha_stage）
  2. 验证码 SDK 的网络回包（VerifyCaptchaV3 / Init / 挑战）状态与片段
  3. 登录结束时的 DOM：验证码容器、浮层、错误文案
  4. 截图 logs/t001_*.png
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(k, None)
os.environ.setdefault("DISPLAY", ":1")
os.environ.setdefault("XDG_CACHE_HOME", os.path.join(ROOT, ".cache"))

from src import config                                   # noqa: E402
from src.pipeline import login_identifier                # noqa: E402
from src.browser import BrowserSession                   # noqa: E402


def pick_target(email: str = None) -> dict:
    import glob
    snaps = sorted(glob.glob(os.path.join(
        ROOT, "ledger", "runs", "*", "results-*.json")))
    recs = json.load(open(snaps[-1], encoding="utf-8"))
    if isinstance(recs, dict):
        recs = recs.get("records") or []
    cand = [r for r in recs
            if not str(r.get("api_key") or "").startswith("sk-")
            and str(r.get("sso_uid") or "").strip()
            and str(r.get("email") or "").split("@")[-1].lower()
            not in {"hotmail.com", "outlook.com", "gmail.com", "qq.com"}]
    if email:
        for r in recs:
            if r.get("email") == email:
                return r
    return cand[0] if cand else None


def main() -> int:
    d = pick_target(sys.argv[1] if len(sys.argv) > 1 else None)
    if not d:
        print("✗ 没有可测的 keyless 新域账号")
        return 1
    em = d["email"]
    print(f"目标: {em}  username={d.get('username','')}  uid={d.get('sso_uid','')}")

    login_id = login_identifier(em, d.get("username", ""))
    print(f"登录标识: {login_id}")

    shot = os.path.join(ROOT, "logs", "t001_shot")
    with BrowserSession(headless=False) as sess:
        print(f"[browser ready {sess.launch_ms}ms]")
        res = sess.login(login_id, d["password"], verbose=True,
                         attempts=1, screenshot_prefix=shot)
        print(f"\n=== 结果: ok={res.ok} reason={res.reason}")
        print(f"    captcha_stage={res.captcha_stage}")
        print(f"    timings={res.timings}")
        try:
            ctx = sess._browser.contexts[-1]
            page = ctx.pages[-1]
            dom = page.evaluate("""() => {
              const q = s => document.querySelector(s);
              const vis = e => !!(e && e.getBoundingClientRect().width > 0);
              return JSON.stringify({
                url: location.href,
                has_captcha_container: vis(q('#aliyunCaptcha')),
                has_checkbox_icon: vis(q('#aliyunCaptcha-checkbox-icon')),
                has_popup: vis(q('#aliyunCaptcha-window-popup')),
                has_slider: vis(q('#aliyunCaptcha-sliding-slider')),
                err_text: (q('#aliyunCaptcha-sliding-errorCode')||{}).innerText || '',
                captcha_text: (q('#aliyunCaptcha-captcha-text')||{}).innerText || '',
                body_head: document.body.innerText.slice(0, 300)
              });
            }""")
            print("\n=== DOM 快照:")
            for k, v in json.loads(dom).items():
                print(f"    {k}: {str(v)[:160]}")
        except Exception as ex:
            print(f"（DOM 快照失败: {type(ex).__name__}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
