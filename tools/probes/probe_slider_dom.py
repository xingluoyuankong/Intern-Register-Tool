"""服务器端：在 camoufox 里加载 aliyun_waf_aa 滑块页，dump DOM 结构。"""
import os
os.environ.setdefault("XDG_CACHE_HOME", "/app/working/intern-register/.cache")
os.environ.setdefault("DISPLAY", ":1")
import sys, json
sys.path.insert(0, "/app/working/intern-register")
from src.browser.launch import launch_browser

HTML = "/app/working/intern-register/aa_page.html"
html = open(HTML, encoding="utf-8").read()
print("aa page len", len(html), flush=True)

cm = launch_browser(None, headless=True)
br = cm.__enter__()
try:
    ctx = br.new_context()
    page = ctx.new_page()
    page.route("https://sso.openxlab.org.cn/__aa_probe",
               lambda route: route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html))
    page.goto("https://sso.openxlab.org.cn/__aa_probe", wait_until="load", timeout=25000)
    page.wait_for_timeout(3500)
    info = page.evaluate("""() => {
        const out = {ids: [], imgs: [], canvases: [], scripts: [], bodyText: ''};
        document.querySelectorAll('[id]').forEach(e => {
            const r = e.getBoundingClientRect();
            out.ids.push({id: e.id, w: Math.round(r.width), h: Math.round(r.height), tag: e.tagName});
        });
        document.querySelectorAll('img').forEach(e => out.imgs.push({src: (e.src||'').slice(0,120), w: e.width, h: e.height}));
        document.querySelectorAll('canvas').forEach(e => out.canvases.push({id: e.id, w: e.width, h: e.height}));
        document.querySelectorAll('script').forEach(e => out.scripts.push({src: (e.src||'inline').slice(0,120)}));
        out.bodyText = (document.body ? document.body.innerText : '').slice(0, 300);
        return out;
    }""")
    print(json.dumps(info, ensure_ascii=False, indent=1)[:4000], flush=True)
    ctx.close()
finally:
    try:
        cm.__exit__(None, None, None)
    except Exception:
        pass
