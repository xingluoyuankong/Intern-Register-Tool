"""阿里云 WAF（acw_sc__v2 JS 挑战）破解器。

2026-09-30 实测：`sso.openxlab.org.cn` 的写接口（`/register/byEmail` 等）开始
返回阿里云 WAF 的 JS 挑战页（HTTP 200 + `text/html`，内含 `arg1` 与混淆 JS），
`requests` 拿到的是 HTML 而不是 JSON —— 纯 HTTP 路径整个被打断。

破解原理（`_probe_waf.py` 实测通过，注册返回 `success:true`）：
  1. 挑战页自带完整算法：`setCookie("acw_sc__v2", x)` 由页内混淆 JS 算出。
  2. 用真实浏览器执行这段 JS（把 `document.location.reload()` 替换掉防死循环），
     cookie 就落在浏览器 context 里 —— **同源**（route 到 sso.openxlab.org.cn
     的假页面）保证 cookie 域正确。
  3. 把 `acw_sc__v2` + 首发响应的 `acw_tc` 一起塞回 `requests.Session`，重放原
     POST → 拿到 JSON。

🔴 三个实测坑（都写在调用方注释里，这里只存结论）：
  - 只带 `acw_sc__v2` 不带 `acw_tc`：请求被**挂起**不响应；
  - 重放必须与首发走**同一条网络路径**（同代理配置），换路径也会挂起；
  - `browser.close()` / `sync_playwright().stop()` 在本机会**挂死**
    （系统 Chrome 与 headless shell 都一样）→ 本模块的浏览器**从不 close**，
    进程退出用 `atexit` 杀 driver 进程树兜底。

并发模型：Playwright 同步 API 绑定创建线程，而注册的 producer 是多线程 ——
所以 playwright 跑在**专用线程**里，`solve()` 经队列跨线程提交，任意线程可调。
整个进程共用一个浏览器（challenge 页无状态，cookie 每次现算）。
"""

import atexit
import glob
import os
import queue
import subprocess
import threading

from . import config


# ── 浏览器可执行文件发现 ─────────────────────────────────────────
def find_headless_browser() -> str:
    """headless 用 playwright 自带 chromium（系统 Chrome 的 close 会挂死）。

    顺序：`IR_WAF_BROWSER_PATH` → playwright 缓存里**最新**的
    chromium_headless_shell → 回退 `config.CHROME_PATH`。
    """
    env = os.getenv("IR_WAF_BROWSER_PATH", "").strip()
    if env and os.path.isfile(env):
        return env
    cache = os.path.join(os.path.expanduser("~"), "AppData", "Local",
                         "ms-playwright")
    cands = sorted(glob.glob(os.path.join(
        cache, "chromium_headless_shell-*", "*", "chrome-headless-shell.exe")))
    if cands:
        # 版本号排序取最新（目录名 chromium_headless_shell-<build>）
        return cands[-1]
    return config.CHROME_PATH


def kill_driver_tree(pw) -> None:
    """杀掉 playwright driver（node）进程树 —— 连着里面的浏览器一起。

    为什么不用 `browser.close()`：本机实测它**挂死**（不是抛异常，是永远不
    返回）。playwright 没有官方的"浏览器进程 PID"接口，而 driver（node）是
    python 的子进程、chrome 是 node 的子进程，所以杀 driver 的进程树 =
    全部收掉。Windows 上用 `taskkill /T /F`。
    """
    if pw is None:
        return
    try:
        proc = pw._connection._transport._proc       # noqa: SLF001 内部结构
        pid = getattr(proc, "pid", None)
    except Exception:
        pid = None
    if not pid:
        return
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True, timeout=15, check=False)
    except Exception:
        pass


# ── 破解器 ───────────────────────────────────────────────────────
class WafSolver:
    """单例。`solve(challenge_html) -> dict[name, value]`，任意线程可调。"""

    _lock = threading.Lock()
    _instance = None

    @classmethod
    def get(cls) -> "WafSolver":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def __init__(self):
        self._req_q: queue.Queue = queue.Queue()
        self._resp_q: queue.Queue = queue.Queue()
        self._started = False
        self._start_lock = threading.Lock()

    # ── 对外 ─────────────────────────────────────────────────
    def solve(self, challenge_html: str) -> dict[str, str]:
        """算出挑战页要求的 cookie（阻塞，最长 `WAF_SOLVE_TIMEOUT` 秒）。

        返回浏览器 context 里的**全部** cookie（至少含 `acw_sc__v2`；
        `acw_tc` 由调用方从首发响应的 set-cookie 里拿 —— 假页面产生不了它）。
        """
        self._ensure_started()
        self._req_q.put(challenge_html)
        try:
            kind, val = self._resp_q.get(timeout=config.WAF_SOLVE_TIMEOUT)
        except queue.Empty as ex:
            raise RuntimeError(
                f"WAF cookie 计算超时（>{config.WAF_SOLVE_TIMEOUT}s）"
                f"—— 浏览器没在跑？") from ex
        if kind == "err":
            raise RuntimeError(f"WAF cookie 计算失败: {val}")
        return val

    def shutdown(self):
        """杀 driver 进程树（run.py 收尾时调用；atexit 也会兜一次）。"""
        self._req_q.put(None)

    # ── 专用线程 ─────────────────────────────────────────────
    def _ensure_started(self):
        with self._start_lock:
            if self._started:
                return
            t = threading.Thread(target=self._loop, daemon=True,
                                 name="waf-solver")
            t.start()
            self._started = True

    def _loop(self):
        """playwright 全生命周期都在这个线程（同步 API 的线程绑定约束）。"""
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        atexit.register(kill_driver_tree, pw)
        browser = pw.chromium.launch(
            executable_path=find_headless_browser(), headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = browser.new_context()
        page = ctx.new_page()
        self._current_html = ""

        def _fulfill(route):
            route.fulfill(status=200,
                          content_type="text/html; charset=utf-8",
                          body=self._current_html)

        page.route(f"{config.SSO_BASE}/__waf_probe", _fulfill)

        while True:
            html = self._req_q.get()
            if html is None:            # shutdown：强杀进程树，不指望优雅退出
                kill_driver_tree(pw)
                return
            try:
                self._current_html = html.replace(
                    "document.location.reload()", "void 0;")
                page.goto(f"{config.SSO_BASE}/__waf_probe",
                          wait_until="load", timeout=20000)
                page.wait_for_timeout(1500)     # 给混淆 JS 一点执行时间
                cookies = {c["name"]: c["value"] for c in ctx.cookies()}
                if not cookies.get("acw_sc__v2"):
                    raise RuntimeError(f"cookie 没算出来，只得到 {list(cookies)}")
                self._resp_q.put(("ok", cookies))
            except Exception as ex:             # noqa: BLE001 单次失败不退出循环
                self._resp_q.put(("err", str(ex)))
