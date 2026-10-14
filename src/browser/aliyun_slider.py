"""阿里云验证码滑块（拼图）破解 —— 移植自 `qoder第二人机验证`。

来源：
  `qoder第二人机验证/gap_detect.py`（多算法缺口识别）
  `qoder第二人机验证/slider_pw.py`（Playwright 类人拖动 + 闭环校正）
  `qoder第二人机验证/cdp_batch_v3_ab.py`（JS_CAPTCHA_IMGS / _gap_x_from_bytes）

差异：qoder 用 CDP `cdp.eval`；这里换成 Playwright `page.evaluate`，
图片 src 是 base64（`data:image/...`）直接解码，不再走 CDP 取字节。

对外只暴露 `solve_aliyun(page)`：在当前 page 上把滑块过掉。
"""

import base64
import json
import math
import random
import time

# ── 取图 / 几何 ────────────────────────────────────────────────
JS_IMGS = """(function(){
  var b=document.querySelector('#aliyunCaptcha-img');
  var p=document.querySelector('#aliyunCaptcha-puzzle');
  if(!b||!p)return null;
  var br=b.getBoundingClientRect(), pr=p.getBoundingClientRect();
  return JSON.stringify({bg:b.src,pz:p.src,
    bnw:b.naturalWidth,bw:br.width,bx:br.x,
    pnw:p.naturalWidth,pw:pr.width,px:pr.x});
})()"""

JS_GEO = """(function(){
  function rc(sel){var e=document.querySelector(sel);if(!e)return null;
    var r=e.getBoundingClientRect();
    return {x:r.x,y:r.y,w:r.width,h:r.height};}
  var s=document.querySelector('#aliyunCaptcha-sliding-slider');
  return JSON.stringify({
    slider:rc('#aliyunCaptcha-sliding-slider'),
    track:rc('#aliyunCaptcha-sliding-body'),
    body:rc('#aliyunCaptcha-captcha-body'),
    text:rc('#aliyunCaptcha-captcha-text'),
    bar:rc('#aliyunCaptcha-sliding-text-box'),
    refresh:rc('#aliyunCaptcha-btn-refresh'),
    slider_cls:s?(s.className||''):null
  });
})()"""


def _img_bytes(page, src: str) -> bytes:
    """取图片字节：`data:` 直接解码；`http(s)` 走 `page.request`。

    🔴 阿里云滑块的背景图/拼图图是 **CDN 的 http URL**（`g.alicdn.com/...`），
    不是 `data:` —— 只解码 data URL 会拿到空字节，`cv2.imdecode` 直接失败
    （实测 `decode_fail`）。而页面里 `fetch()` 取 CDN 会被 CORS 拦，
    所以走 Playwright 的 `page.request`：它用浏览器上下文（含代理/cookie），
    不受页面同源策略约束。
    """
    if not src:
        return b""
    if src.strip().lower().startswith("data:"):
        return _decode_data_url(src)
    try:
        r = page.request.get(src, timeout=15000)
        return r.body() or b""
    except Exception:
        return b""


def _decode_data_url(src: str) -> bytes:
    if not src:
        return b""
    if "," in src and src.strip().lower().startswith("data:"):
        src = src.split(",", 1)[1]
    try:
        return base64.b64decode(src)
    except Exception:
        return b""


def _gap_x(bg_b: bytes, pz_b: bytes):
    """在背景图里定位缺口，返回 (缺口左缘native x, 诊断)。"""
    try:
        import cv2
        import numpy as np
    except Exception as e:  # noqa: BLE001
        return None, {"why": f"no_cv2:{type(e).__name__}"}
    bg = cv2.imdecode(np.frombuffer(bg_b, np.uint8), cv2.IMREAD_COLOR)
    pz = cv2.imdecode(np.frombuffer(pz_b, np.uint8), cv2.IMREAD_UNCHANGED)
    if bg is None or pz is None:
        return None, {"why": "decode_fail"}
    if pz.ndim < 3 or pz.shape[2] < 4:
        return None, {"why": "puzzle_no_alpha"}
    alpha = pz[:, :, 3]
    ys, xs = np.where(alpha > 10)
    if len(xs) == 0:
        return None, {"why": "alpha_empty"}
    x0, x1, y0, y1 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
    tpl = pz[y0:y1 + 1, x0:x1 + 1, :3]
    msk = alpha[y0:y1 + 1, x0:x1 + 1]
    if tpl.shape[0] < 8 or tpl.shape[1] < 8:
        return None, {"why": "tpl_too_small"}
    band = bg[y0:y1 + 1, :]
    if band.shape[1] <= tpl.shape[1]:
        return None, {"why": "band_narrow"}
    m3 = cv2.cvtColor(msk, cv2.COLOR_GRAY2BGR)
    try:
        res = np.nan_to_num(cv2.matchTemplate(band, tpl, cv2.TM_CCORR_NORMED, mask=m3))
    except Exception as e:  # noqa: BLE001
        return None, {"why": f"match_fail:{type(e).__name__}"}
    # 抑制拼图自身初始位置
    res[:, :min(res.shape[1], x1 - x0 + 6)] = -1
    cc = res.max(axis=0)
    k = int(np.argmax(cc))
    return k + x0, {"gap_x": k + x0, "score": float(cc[k]), "pz_x0": x0,
                    "bg_w": int(bg.shape[1])}


# ── 拖动 ───────────────────────────────────────────────────────
def _drag(page, sx, sy, dx):
    page.mouse.move(sx - random.uniform(4, 9), sy + random.uniform(-3, 3))
    page.wait_for_timeout(random.randint(100, 220))
    page.mouse.move(sx, sy)
    page.wait_for_timeout(random.randint(120, 260))
    page.mouse.down()
    page.wait_for_timeout(random.randint(80, 170))
    steps = max(25, int(abs(dx) / random.uniform(4.5, 7.5)))
    overshoot = random.uniform(3, 9)
    peak = dx + overshoot
    for i in range(1, steps + 1):
        t = i / steps
        e = 4 * t * t * t if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2
        page.mouse.move(sx + peak * e,
                        sy + math.sin(t * math.pi * random.uniform(1.6, 2.4))
                        * random.uniform(0.6, 1.8))
        page.wait_for_timeout(random.randint(3, 6 if 0.25 < t < 0.75 else 12))
    for back in range(random.randint(2, 3)):
        page.mouse.move(sx + peak - overshoot * (back + 1) / 3,
                        sy + random.uniform(-0.6, 0.6))
        page.wait_for_timeout(random.randint(40, 90))
    page.mouse.move(sx + dx, sy)
    page.wait_for_timeout(90)
    page.wait_for_timeout(random.randint(130, 260))
    page.mouse.up()


def _open_panel(page):
    """点验证条展开拼图面板（滑块拿到真实尺寸）。"""
    try:
        g = json.loads(page.evaluate(JS_GEO) or "{}")
    except Exception:
        return False
    s = g.get("slider")
    if s and s.get("w", 0) > 5 and s.get("x", 0) > 1:
        return True
    tgt = g.get("text") or g.get("bar")
    if not tgt or tgt.get("w", 0) <= 0:
        return False
    x = tgt["x"] + tgt["w"] / 2
    y = tgt["y"] + tgt["h"] / 2
    page.mouse.move(x - random.uniform(20, 50), y + random.uniform(-6, 6))
    page.wait_for_timeout(random.randint(100, 200))
    page.mouse.move(x, y)
    page.wait_for_timeout(random.randint(80, 180))
    page.mouse.down()
    page.wait_for_timeout(random.randint(50, 110))
    page.mouse.up()
    deadline = time.time() + 10
    while time.time() < deadline:
        page.wait_for_timeout(600)
        try:
            g = json.loads(page.evaluate(JS_GEO) or "{}")
        except Exception:
            return False
        s = g.get("slider")
        if s and s.get("w", 0) > 5 and s.get("x", 0) > 1:
            return True
    return False


def _verdict(page) -> str:
    """pass / fail / unknown。"""
    try:
        raw = page.evaluate("""() => {
            const s=document.getElementById('aliyunCaptcha-sliding-slider');
            const err=document.getElementById('aliyunCaptcha-sliding-errorCode');
            const txt=document.getElementById('aliyunCaptcha-captcha-text');
            const otp=document.querySelectorAll('.ant-otp-input').length;
            return JSON.stringify({
              sw: s?s.getBoundingClientRect().width:0,
              scls: s?(s.className||''):null,
              err: err?(err.innerText||''):'',
              txt: txt?(txt.innerText||''):'',
              otp: otp});
        }""")
        d = json.loads(raw or "{}")
    except Exception:
        return "unknown"
    if d.get("otp", 0) >= 4:
        return "pass"
    blob = f"{d.get('scls') or ''} {d.get('txt') or ''}".lower()
    if "success" in blob or "pass" in blob or "验证通过" in blob:
        return "pass"
    if d.get("err"):
        return "fail"
    if (d.get("sw") or 0) <= 0:
        return "pass"
    return "unknown"


def solve_aliyun(page, *, max_attempts: int = 6, timeout_s: float = 40.0) -> bool:
    """在当前 page 上过阿里云滑块。返回是否通过。

    两种形态都覆盖：
      - 拼图（`#aliyunCaptcha-img`/`#aliyunCaptcha-puzzle` 存在）→ 缺口识别 + 拖到位
      - 纯滑动（无拼图）→ 拖到轨道最右端
    """
    deadline = time.time() + timeout_s
    for attempt in range(1, max_attempts + 1):
        if time.time() > deadline:
            return False
        if not _open_panel(page):
            page.wait_for_timeout(1500)
            continue
        # 等图
        imgs = None
        for _ in range(12):
            try:
                imgs = json.loads(page.evaluate(JS_IMGS) or "null")
            except Exception:
                imgs = None
            if imgs:
                break
            page.wait_for_timeout(1000)

        try:
            g = json.loads(page.evaluate(JS_GEO) or "{}")
        except Exception:
            g = {}
        s = g.get("slider") or {}
        track = g.get("track") or {}
        sx = s.get("x", 0) + s.get("w", 0) / 2
        sy = s.get("y", 0) + s.get("h", 0) / 2

        if imgs:
            # 拼图模式
            # 🔴 CDN 图（http URL）也要能取 —— 见 `_img_bytes` 的说明。
            bg_b = _img_bytes(page, imgs.get("bg", ""))
            pz_b = _img_bytes(page, imgs.get("pz", ""))
            gap, info = _gap_x(bg_b, pz_b)
            if gap is None:
                page.wait_for_timeout(1200)
                continue
            scale = (imgs.get("bw", 0) / imgs.get("bnw", 1)) if imgs.get("bnw") else 1.0
            x0 = info.get("pz_x0", 0)
            dx = (gap - x0) * scale
            max_dx = (track.get("w", 300) - s.get("w", 40) - 2)
            dx = max(8, min(dx, max_dx))
        else:
            # 纯滑动模式：拖到最右
            dx = max(8, (track.get("w", 320) - s.get("w", 40) - random.uniform(1, 3)))
        _drag(page, sx, sy, dx)
        page.wait_for_timeout(2500)
        v = _verdict(page)
        if v == "pass":
            return True
        page.wait_for_timeout(random.randint(400, 900))
    return False
