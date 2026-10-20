"""人类化鼠标行为 —— 阿里云行为风控真正评估的信号。

风控看的是**轨迹与按键节奏**，所以这里的每个随机量都是功能的一部分，
不是"随便加点随机"。实测依据见 `docs/protocol.md` 的
「点击验证码：行为风控看的是轨迹」与「验证码有两条通路」两节。

⚠ 不要为了"提速"去掉步进延时或过冲回正 —— 那会直接导致 F001 拒绝。
"""

import math
import random

from .constants import MICRO_MOVE, MICRO_WAIT_MS


# ────────────────────────────────────────────────────────────────
# 人类化鼠标动作
# ────────────────────────────────────────────────────────────────
def _human_move(page, x0: float, y0: float, x1: float, y1: float,
                *, steps: int = None) -> None:
    """沿三次贝塞尔曲线分步移动鼠标，模拟人类轨迹。

    真人移动的特征：不是直线、有轻微弧度、速度先快后慢（ease-out）、
    步间间隔不均匀。这里用 smoothstep 缓动 + 随机控制点偏移还原。

    ⚠ 步间延时**不要去掉** —— 它是被风控评估的信号本身。

    🔴 2026-10-18 速度模型重写（F001 攻坚，实测依据见下）：
       旧实现每步间隔 `random.randint(9, 30)` —— **全程均匀随机抖动**。
       这不是人类特征：真人手臂由「加速 → 峰值 → 减速」驱动，
       点间隔呈**双峰/钟形**分布，且**起点与终点附近明显更慢**
       （最小抖动/瞄准修正），中段最快；同时还会有 1~3 次**微停顿**
       （约 40~120ms，瞄准或注意力跳变）。

       阿里云 F001 是**行为风控**（`captchaVerifyParam` 带轨迹特征），
       等间隔均匀抖动正是最容易被分类器抓的模式（step_interval 的
       熵高但**分布形状错**：真人偏态，均匀随机对称）。
       现在按 Fitts 定律的速度轮廓生成：v(t) ∝ 钟形，间隔 = 距离 / 速度，
       再叠加有意微停顿。指纹/UA 不参与，纯行为面。
    """
    dx, dy = x1 - x0, y1 - y0
    dist = math.hypot(dx, dy)
    if dist < 1.5:
        page.mouse.move(x1, y1)
        return
    if steps is None:
        # 距离越远步数越多，但**密度**不是常数（真人长距离单位时间步数更稀）
        steps = max(10, min(70, int(dist / 7)))
    amp = min(55.0, dist * 0.18)
    c1x = x0 + dx * 0.30 + random.uniform(-amp, amp)
    c1y = y0 + dy * 0.30 + random.uniform(-amp, amp)
    c2x = x0 + dx * 0.70 + random.uniform(-amp, amp)
    c2y = y0 + dy * 0.70 + random.uniform(-amp, amp)

    # 整体耗时：人类移动同样距离的耗时随距离亚线性增长（Fitts）
    # 🔴 校准依据：真人鼠标 300px 移动实测中位 ~450ms、900px ~750ms
    #    （Fitts：MT = a + b·log2(2D/W)，b≈0.15s/bit）。
    total_ms = min(780.0, 150.0 + 48.0 * math.log2(1.0 + dist)) \
        * random.uniform(0.80, 1.25)
    # 微停顿：真人常见 1~3 次极短停顿（瞄准修正），位置随机
    pause_at = set()
    for _ in range(random.randint(1, 3)):
        pause_at.add(random.randint(2, max(3, steps - 2)))

    # 🔴 速度权重先算好再归一化到 total_ms —— 直接按 bell 除法会因
    #    逐步钳制（min 3.5ms）把总时长放大约 1.5~2 倍（实测 300px 要 1.1s）。
    weights = []
    for i in range(1, steps + 1):
        t = i / steps
        bell = math.sin(math.pi * t) ** 1.5 + 0.12   # 两端慢、中段快的钟形
        weights.append((1.0 / bell) * random.uniform(0.88, 1.14))
    wsum = sum(weights) or 1.0
    delays = [max(2.0, total_ms * w / wsum) for w in weights]

    for i in range(1, steps + 1):
        t = i / steps
        e = t * t * (3 - 2 * t)          # smoothstep 缓动（位置曲线）
        mt = 1 - e
        x = (mt ** 3 * x0 + 3 * mt * mt * e * c1x
             + 3 * mt * e * e * c2x + e ** 3 * x1)
        y = (mt ** 3 * y0 + 3 * mt * mt * e * c1y
             + 3 * mt * e * e * c2y + e ** 3 * y1)
        page.mouse.move(x, y)
        page.wait_for_timeout(int(delays[i - 1]))
        if i in pause_at:
            page.wait_for_timeout(random.randint(40, 120))
        prev = (x, y)
    return prev


def _idle_wait(page, *, stats: dict = None) -> None:
    """**不发任何输入事件**，只泵送事件循环地等一小段。

    两个用途，语义相同（都是"让页面安静下来"）：
      1. 对照组（IR_NO_MICRO_MOVE=1）—— 隔离出"鼠标事件"这一个变量；
      2. 超过 `MICRO_BUDGET_S` 之后的安静期 —— 停止喂数据，
         给 SDK 一个不再被新轨迹延后的窗口去下降级决定。

    ⚠ 仍然必须用 `wait_for_timeout` 而不是 `time.sleep`（见文件头第 3 条）：
      安静期里恰恰最需要网络事件回调被执行。
    """
    if stats is not None:
        stats["idle_waits"] = stats.get("idle_waits", 0) + 1
    page.wait_for_timeout(MICRO_WAIT_MS)


def _micro_move(page, cur: tuple, vw: int, vh: int, *, stats: dict = None) -> tuple:
    """做一次小幅鼠标移动，返回新位置。

    用途：在"等验证码 SDK 初始化"这类原本空转的等待里持续产生行为数据。
    每次约 150~350ms，比整段 warmup 更贴近真人（真人不会停手不动）。

    对照组（IR_NO_MICRO_MOVE=1）：只等同样长的时间，**不发鼠标事件**。
    这是为了回答一个具体问题 —— 微移动到底加快了还是拖慢了 SDK 的
    TRACELESS→CHECK_BOX 降级时序。

    统计口径（`stats`）刻意把三种动作分开计数，否则"移动次数"会把
    纯等待也算进去，实验组和对照组就没法比：
      moves      真正发出了鼠标轨迹的微移动次数
      points     累计发出的轨迹点数
      idle_waits 未发事件的纯等待次数（对照组 + 超预算后的安静期）
    """
    if not MICRO_MOVE:
        _idle_wait(page, stats=stats)
        return cur
    x = min(max(cur[0] + random.uniform(-170, 170), 20), max(vw - 20, 21))
    y = min(max(cur[1] + random.uniform(-120, 120), 20), max(vh - 20, 21))
    steps = random.randint(5, 13)
    _human_move(page, cur[0], cur[1], x, y, steps=steps)
    if stats is not None:
        stats["moves"] = stats.get("moves", 0) + 1
        stats["points"] = stats.get("points", 0) + steps
    page.wait_for_timeout(random.randint(50, 150))
    return (x, y)


def _pre_submit_human_session(page, vw: int, vh: int, *, stats: dict = None,
                              seconds: float = 6.0) -> tuple:
    """提交登录前，模拟一段**真实用户会有的页面交互**（2026-10-18）。

    🔴 为什么必须有这一段（F001/F008 攻坚的最后一块）：
       阿里云验证码 `captchaVerifyParam` 评估的不只是鼠标轨迹，
       而是**整段会话的行为密度**：真人打开登录页必然有
       滚动、焦点切换、鼠标悬停停留、偶发误点空白处再收回。
       我们此前的行为数据只有「直线式微移动 + 打字」，密度和多样性
       都远低于真人，这是会话级风控（F001→F008）最可能识别的空白。

       这里做四类真人动作，全部随机：
         ① 滚动页面（登录页内容高于一屏，真人会滚动确认）
         ② 鼠标在输入框附近悬停停留（读字、确认内容）
         ③ 偶发点击空白区域（误点）然后移开
         ④ 焦点在账号/密码框之间切换（TAB / 点击）
    这些动作**不改变表单内容**，只产生真实事件流。
    """
    import time as _t

    end = _t.time() + max(1.0, seconds)
    x = random.uniform(vw * 0.3, vw * 0.7)
    y = random.uniform(vh * 0.3, vh * 0.6)
    page.mouse.move(x, y)
    acts = 0
    while _t.time() < end:
        roll = random.random()
        try:
            if roll < 0.30:
                # ① 滚动：真人看页面必然滚动，滚轮事件是强人类信号
                dy = random.choice([-120, -80, 80, 120, 160])
                page.mouse.wheel(0, dy)
                acts += 1
                page.wait_for_timeout(random.randint(300, 900))
            elif roll < 0.55:
                # ② 悬停停留（读内容）
                nx = random.uniform(vw * 0.25, vw * 0.8)
                ny = random.uniform(vh * 0.25, vh * 0.75)
                _human_move(page, x, y, nx, ny)
                x, y = nx, ny
                acts += 1
                page.wait_for_timeout(random.randint(400, 1100))
            elif roll < 0.72:
                # ③ 误点空白 → 立即收回（真人极常见）
                nx = random.uniform(vw * 0.15, vw * 0.85)
                ny = random.uniform(vh * 0.2, vh * 0.8)
                _human_move(page, x, y, nx, ny)
                x, y = nx, ny
                page.mouse.down()
                page.wait_for_timeout(random.randint(50, 110))
                page.mouse.up()
                acts += 1
                page.wait_for_timeout(random.randint(250, 700))
            elif roll < 0.86:
                # ④ 焦点切换（不改内容）
                page.keyboard.press("Tab")
                acts += 1
                page.wait_for_timeout(random.randint(200, 600))
            else:
                # 微移动保持连续
                x, y = _micro_move(page, (x, y), vw, vh, stats=stats)
        except Exception:
            break
    if stats is not None:
        stats["pre_submit_acts"] = stats.get("pre_submit_acts", 0) + acts
    return x, y


def _warmup_mouse(page, vw: int, vh: int, *, stats: dict = None) -> tuple:
    """提交前的短暖场（2~3 轮）。

    真正的长时间鼠标活动交给 `_micro_move` 在验证码初始化等待期间做 ——
    那样不额外占用时间。这里只保证"提交动作前手是动过的"。
    """
    x = random.uniform(vw * 0.25, vw * 0.75)
    y = random.uniform(vh * 0.25, vh * 0.60)
    page.mouse.move(x, y)
    page.wait_for_timeout(random.randint(120, 260))
    for _ in range(random.randint(2, 3)):
        x, y = _micro_move(page, (x, y), vw, vh, stats=stats)
    return x, y
