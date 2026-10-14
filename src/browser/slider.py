"""阿里云「访问验证」滑块（`aliyun_waf_aa` 页）破解。

背景（2026-10-13 抓包确认）：WAF 对高风险出口不再下发 JS 挑战（`acw_sc__v2`），
而是下发一页 `<!doctype html><meta name="aliyun_waf_aa" ...><title>滑动验证页面</title>`，
渲染出阿里云验证码 SDK 的**滑动验证**：

    #aliyunCaptcha-sliding-wrapper
      #aliyunCaptcha-sliding-body        (320x40 轨道)
        #aliyunCaptcha-sliding-left      (已滑过部分)
        #aliyunCaptcha-sliding-slider    (40x40 滑块，可拖)
      #aliyunCaptcha-sliding-text-box / -text

过验证 = 用**类人轨迹**把滑块拖到轨道最右端，等 SDK 判定通过（滑块消失 / 出现成功态），
之后 WAF 放行并种下放行 cookie。

本模块只做「在给定 page 上把这一页滑块过掉」；由 `waf_bypass` 在拿到 aa 页时调用。
"""

import random
import time


def _slider_visible(page) -> bool:
    try:
        return bool(page.evaluate("""() => {
            const el = document.getElementById('aliyunCaptcha-sliding-slider');
            if (!el) return false;
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
        }"""))
    except Exception:
        return False


def _done(page) -> bool:
    """滑块消失 / 出现通过态 → 视为过验证。"""
    try:
        return bool(page.evaluate("""() => {
            const s = document.getElementById('aliyunCaptcha-sliding-slider');
            if (!s) return true;
            const r = s.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) return true;
            // 错误码出现且非空 = 失败
            const err = document.getElementById('aliyunCaptcha-sliding-errorCode');
            if (err && (err.innerText || '').trim()) return false;
            return false;
        }"""))
    except Exception:
        return False


def _failed(page) -> str:
    try:
        return str(page.evaluate("""() => {
            const err = document.getElementById('aliyunCaptcha-sliding-errorCode');
            return err ? (err.innerText || '').trim() : '';
        }""") or "")
    except Exception:
        return ""


def _drag(page, x0: float, y0: float, x1: float, *, dur_ms: int = None) -> None:
    """从 (x0,y0) 拖到 x1（同一 y），带过冲-回拉 + 微抖动的类人轨迹。"""
    page.mouse.move(x0, y0)
    page.wait_for_timeout(random.randint(120, 260))
    page.mouse.down()
    page.wait_for_timeout(random.randint(80, 180))
    dist = x1 - x0
    dur = dur_ms or random.randint(900, 1600)
    steps = random.randint(28, 46)
    for i in range(1, steps + 1):
        t = i / steps
        # ease-out：先快后慢（人类拖动特征）
        eased = 1 - (1 - t) ** 2.2
        x = x0 + dist * eased
        y = y0 + random.uniform(-1.2, 1.2)
        page.mouse.move(x, y)
        # 非均匀步进间隔
        page.wait_for_timeout(max(1, int(dur / steps * random.uniform(0.6, 1.5))))
    # 过冲一点点再回拉，模拟手抖
    over = random.uniform(3, 9)
    page.mouse.move(x1 + over, y0 + random.uniform(-1, 1))
    page.wait_for_timeout(random.randint(60, 140))
    page.mouse.move(x1, y0)
    page.wait_for_timeout(random.randint(80, 180))
    page.mouse.up()


def solve_slider(page, *, timeout_s: float = 25.0, max_try: int = 3) -> bool:
    """在当前 page 上过阿里云滑动验证。返回是否通过。

    调用前 page 必须已经渲染出滑块页（见 waf_bypass 的 aa 分支）。
    """
    deadline = time.time() + timeout_s
    # 等滑块渲染
    while time.time() < deadline:
        if _slider_visible(page):
            break
        page.wait_for_timeout(300)
    if not _slider_visible(page):
        return False

    for _try in range(max_try):
        try:
            box = page.evaluate("""() => {
                const s = document.getElementById('aliyunCaptcha-sliding-slider');
                const b = document.getElementById('aliyunCaptcha-sliding-body');
                const rs = s.getBoundingClientRect();
                const rb = b.getBoundingClientRect();
                return {sx: rs.x, sy: rs.y, sw: rs.width, sh: rs.height,
                        bx: rb.x, bw: rb.width};
            }""")
        except Exception:
            return False
        x0 = box["sx"] + box["sw"] / 2
        y0 = box["sy"] + box["sh"] / 2
        # 目标：滑块中心到轨道右端（留一点边距）
        x1 = box["bx"] + box["bw"] - box["sw"] / 2 - random.uniform(1, 3)
        _drag(page, x0, y0, x1)
        # 等结果
        t = time.time() + 6
        while time.time() < t:
            page.wait_for_timeout(300)
            if _done(page):
                return True
            if _failed(page):
                break
        # 失败 → 重试前先回位
        page.wait_for_timeout(random.randint(400, 900))
        if not _slider_visible(page):
            return True
    return False
