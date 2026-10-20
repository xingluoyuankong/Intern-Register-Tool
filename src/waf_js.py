"""用 JS 引擎执行 WAF 挑战页的动态算法，取回 `acw_sc__v2`。

🔴 为什么必须有这个模块（2026-09-30 逆向结论）：
   挑战页的混淆 JS **每次下发都不同**（16715 字符的 `_0x4818` 密钥数组 +
   动态混淆函数），不存在"固定 mask/poslist 算法"。早期版本恰好匹配过
   sso.py 里的 `_acw_sc_v2`，后来服务端换了动态算法 → 协议侧算出的 cookie
   全部无效 → 表现为"重放永远被挑战"，而浏览器路径正常（它在执行真 JS）。

   解法：把页面里的 script 交给 JS 引擎执行（stub 掉 DOM），拿到 cookie，
   再用 requests 发注册 —— **仍然是纯协议 HTTP**，不需要浏览器参与。

引擎优先级：
   1. py_mini_racer（进程内 V8，最快，无外部依赖）
   2. node（服务器上 /usr/local/bin/node 可直接用）
   两者都不可用时返回空串，调用方退回浏览器路径（waf_bypass.solve）。
"""

import re
import subprocess
import tempfile
from pathlib import Path

_SCRIPT_RE = re.compile(r"<script[^>]*>(.*?)</script>", re.S | re.I)

# stub：混淆 JS 需要的浏览器环境。
# 🔴 三个必须点（逆向挑战页尾部代码得出）：
#   - 脚本会探测 `window.addEventListener` 决定走 addEventListener 还是
#     IE 的 attachEvent 分支 —— 两条都得给，且**立即回调**（模拟事件触发），
#     否则真正的计算函数不执行，cookie 永远拿不到；
#   - 尾部 `setInterval(fn, 4000)` 才是第一次计算的入口之一 → stub 成
#     立即执行一次；
#   - document.location.reload 置空（防自刷新）。
_STUB = """
var __fire = function(fn) { if (typeof fn === 'function') { try { fn(); } catch (e) {} } };
var navigator = { userAgent: 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
    + '(KHTML, like Gecko) Chrome/153.0.0.0 Safari/53736', webdriver: false };
var location = { href: 'https://sso.openxlab.org.cn/register', reload: function() {},
                 protocol: 'https:', host: 'sso.openxlab.org.cn' };
var document = {
    cookie: '',
    location: location,
    referrer: '',
    createElement: function() { return { style: {}, setAttribute: function() {},
        appendChild: function() {} }; },
    getElementsByTagName: function() { return []; },
    getElementById: function() { return null; },
    addEventListener: function(type, fn, opts) { __fire(fn); },
    attachEvent: function(type, fn) { __fire(fn); },
    detachEvent: function() {},
    removeEventListener: function() {},
    documentElement: { style: {} },
    head: { appendChild: function() {} },
    body: { appendChild: function() {} }
};
var screen = { width: 1280, height: 800, availWidth: 1280, availHeight: 800 };
var window = this;
window.addEventListener = function(type, fn, opts) { __fire(fn); };
window.attachEvent = function(type, fn) { __fire(fn); };
window.document = document;
window.navigator = navigator;
window.location = location;
window.screen = screen;
var setInterval = function(fn, t) { __fire(fn); return 0; };
var setTimeout = function(fn, t) { __fire(fn); return 0; };
var clearInterval = function() {};
var clearTimeout = function() {};
"""

_TAIL = "\n;__result = document.cookie;"


def _scripts(html: str) -> str:
    """拼出可执行的脚本（stub + 页面所有 script + 读回 cookie）。"""
    blocks = [m.group(1) for m in _SCRIPT_RE.finditer(html or "")]
    blocks = [b for b in blocks if b.strip()]
    return _STUB + "\n".join(blocks) + _TAIL


def _run_mini_racer(code: str) -> str:
    from py_mini_racer import py_mini_racer

    ctx = py_mini_racer.MiniRacer()
    cookies = ctx.eval(code)
    return cookies or ""


def _run_node(code: str) -> str:
    # 🔴 两个坑：
    #   1. 混淆脚本会劫持 console.log（反调试）→ 不能用 console 取结果，
    #      改 fs.writeFileSync 落盘再读；
    #   2. .js 可能被当 ESM 加载 → 用 .cjs。
    with tempfile.TemporaryDirectory() as d:
        js = Path(d) / "ch.cjs"
        out = Path(d) / "out.txt"
        js.write_text(
            code + f"\ntry {{ require('fs').writeFileSync("
                   f"{str(out)!r}, String(document.cookie)); }} catch (e) {{}}",
            encoding="utf-8")
        try:
            subprocess.run(["node", str(js)], capture_output=True,
                           timeout=30, check=False)
        except Exception:  # noqa: BLE001
            return ""
        try:
            return out.read_text(encoding="utf-8")
        except OSError:
            return ""


def solve_acw(html: str) -> str:
    """执行挑战页脚本，返回 `acw_sc__v2` 的值；失败返回空串。"""
    code = _scripts(html)
    if not code.strip():
        return ""
    for runner in (_run_mini_racer, _run_node):
        try:
            cookies = runner(code)
        except Exception:  # noqa: BLE001 换引擎再试
            continue
        if not cookies:
            continue
        m = re.search(r"acw_sc__v2=([^;\s]+)", cookies)
        if m:
            return m.group(1)
    return ""
