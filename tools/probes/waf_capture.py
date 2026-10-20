"""抓注册接口经浏览器拿到的**真实响应**（WAF 指纹取证，2026-10-17）。

用法（服务器上，需要 Xvfb :1）：
    DISPLAY=:1 .venv/bin/python tools/probes/waf_capture.py

背景：当天注册全部失败于 `JSONDecodeError: Expecting value: line 1 column 1
(char 0)` —— 日志读起来像"空响应"，真相是注册 POST 被回了一页
**200 + text/html 的 16KB 页面**（JSON 解析在第一个字符 `<` 上就炸了，
"char 0" 从来不是空 body）。本探针独立复现一次这个 POST：

  1. 扫槽位池找一个**当下活着**的出口（`proxypool.slot_alive`）；
  2. 起一个 Camoufox（与生产同款：有头、带槽位代理）；
  3. goto /register → 等 WAF JS 自己跑完（拿 acw_sc__v2）；
  4. 同页 fetch POST /register/byEmail（junk payload —— WAF 不认 RSA，
     业务层即使放行也只会回参数错误的 JSON）；
  5. 把响应体完整存 logs/waf_probe_response.html，打印指纹标记。

判读：
    status=200 + text/html     → WAF 层拦截（页面存档后 grep 标记定位是哪种）
    JSON（哪怕 4xx/参数错误）  → WAF 放行，问题在业务层/真实 payload
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from src import config, proxypool                      # noqa: E402
from src.browser.launch import launch_browser          # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


def find_live_slot(limit: int = 40) -> str | None:
    """从槽位清单里找第一个当下可达目标站的出口（GET /register 2xx/3xx）。"""
    slots = config.proxy_slots()
    for i, u in enumerate(slots[:limit], 1):
        if proxypool.slot_alive(u, timeout=10):
            print(f"✓ live slot #{i}")
            return u
        print(f"  slot #{i} dead")
    return None


def main() -> int:
    slot = find_live_slot()
    if not slot:
        print("✗ 整池当下无一可达出口")
        return 1
    payload = {"username": "probex", "email": "probe@example.invalid",
               "password": "junk", "source": config.SOURCE,
               "clientId": config.CLIENT_ID}
    cm = launch_browser(slot, headless=False)
    br = cm.__enter__()
    ctx = br.new_context()
    page = ctx.new_page()
    try:
        t0 = time.time()
        page.goto(f"{config.SSO_BASE}/register",
                  wait_until="domcontentloaded", timeout=60000)
        print(f"goto ok ({time.time() - t0:.1f}s) "
              f"cookies={[c['name'] for c in ctx.cookies()]}")
        page.wait_for_timeout(8000)      # 给挑战 JS 收尾时间
        js = """async (args) => {
            const r = await fetch(args.path, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                credentials: 'include',
                body: JSON.stringify(args.payload),
            });
            return {status: r.status, ct: r.headers.get('content-type') || '',
                    text: await r.text()};
        }"""
        res = page.evaluate(js, {"path": f"{config.SSO_GW}/register/byEmail",
                                 "payload": payload})
        print(f"POST /register/byEmail → status={res['status']} "
              f"ct={res['ct']!r} len={len(res['text'])}")
        out = os.path.join(ROOT, "logs", "waf_probe_response.html")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            f.write(res["text"])
        print(f"saved → {out}")
        print("head300:", res["text"][:300].replace("\n", "\\n"))
    finally:
        try:
            ctx.close()
        except Exception:
            pass
        try:
            cm.__exit__(None, None, None)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
