"""全局配置。

所有密钥类信息集中在此，可通过环境变量或项目根的 `.env` 文件覆盖。

🔴 凭据**不写死在代码里** —— 本项目托管在公开仓库，写死等于直接泄漏。
   首次使用：`cp .env.example .env`，填入真实值。`.env` 已在 .gitignore 中。
"""

import os
from pathlib import Path

from . import redact


def _load_dotenv(path: Path) -> None:
    """极简 `.env` 解析（stdlib 实现，不引 python-dotenv 依赖）。

    只填充「尚未存在于 os.environ」的键 —— 真实环境变量优先级更高，
    这样临时覆盖（`IR_XXX=1 python run.py`）依然生效。
    """
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv(Path(__file__).resolve().parents[1] / ".env")

# ── CF Worker 临时邮箱 ────────────────────────────────────────────
WORKER_BASE = os.getenv("IR_WORKER_BASE", "")
# ⚠ 默认留空。缺它时由 `validate()` 在入口处报错，而不是静默发一堆 401。
WORKER_ADMIN_TOKEN = os.getenv("IR_WORKER_ADMIN_TOKEN", "")
WORKER_DOMAIN = os.getenv("IR_WORKER_DOMAIN", "")

# ── 邮箱后端选择 ──────────────────────────────────────────────────
# `worker`  = 上面的自建 CF Worker 邮箱（三项必填）
# `temptf`  = 公共 temp.tf 临时邮箱（免注册，见 src/temptf.py）
# `tempmailio` = temp-mail.io 冷门域邮箱（免鉴权，见 src/tempmailio.py）
#
# 🔴 2026-10-17：WAF 按邮箱域名弹滑块（docs/waf-fingerprint-2026-10-17.md）
#    —— hotmail 烧穿、一次性域名库全拦、gmail/qq 撞 429，temp.tf 三域全灭。
#    temp-mail.io 的自动生成冷门域实测能过，当前后端为 **tempmailio**。
MAIL_PROVIDER = os.getenv("IR_MAIL_PROVIDER", "tempmailio").strip().lower()

# temp-mail.io 的**实测可过 WAF** 的域名清单（顺序即优先级）。
# ⚠ 域名会轮换/会烧（实测 yzcalo→429、lnovic/ooynib→滑块）：烧了改这里，
#    不用动代码。客户端会拿它与服务端在架清单求交（见 tempmailio.py）。
TEMPMAILIO_DOMAINS = [d.strip()
                      for d in os.getenv(
                          "IR_TEMPMAILIO_DOMAINS",
                          "ruutukf.com,gmeenramy.com,olipii.com,tanpony.com").split(",")
                      if d.strip()]

TEMPTF_BASE = os.getenv("IR_TEMPTF_BASE", "https://temp.tf").strip()

# 登录（浏览器建 key）是否走代理：默认关。
# 实测走 Resin 时 Page.goto 连 120s 都超时（页面资源加载不动），
# 而直连登录本来就能过。置 IR_LOGIN_USE_PROXY=1 强制登录也走槽位代理。
LOGIN_USE_PROXY = os.getenv("IR_LOGIN_USE_PROXY", "0").strip().lower() in (
    "1", "true", "yes", "on")
# 建箱时按这个顺序降级（实测池子：outlook 25 / hotmail 26 / gmail 5 个账号）
# 🔴 2026-10-14 实测：**`outlook.com` 域名被目标站限流** —— 用它建箱去注册
#    `register/byEmail` 恒返回 429 `{"error":"Too many request"}`（我们已用该
#    域名注册过 400+ 账号）。换成 `hotmail.com` 立刻 200 建号（实测
#    ssoUid 471900417）。所以默认**不再用 outlook**，从 hotmail 起降级。
#    要恢复：IR_TEMPTF_PROVIDERS=outlook,hotmail,gmail
TEMPTF_PROVIDERS = [
    p.strip()
    for p in os.getenv("IR_TEMPTF_PROVIDERS", "hotmail,gmail").split(",")
    if p.strip()
] or ["hotmail", "gmail"]

# 邮箱侧是否走代理（与 tempmail.py 里那个 `IR_PROXY_MAIL` 同义，提到 config 层
# 是为了让两个后端共用同一条判断，别各写一份）
MAIL_PROXY = os.getenv("IR_PROXY_MAIL", "").strip().lower() in ("1", "true", "yes")

# ── 阿里云 WAF 挑战（acw_sc__v2）─────────────────────────────────
# `register/byEmail` 等写接口会返回 JS 挑战页（2026-09-30 实测），由
# `src/waf_bypass.py` 用真实浏览器算 cookie 后重放。这里是求解超时。
# 2026-10-13：浏览器主路径实测 60~90s（goto 60s + 过 WAF + fetch），90s 会频繁
# 超时（超时后求解线程仍在跑，只是响应被丢弃）。放宽到 150s。
WAF_SOLVE_TIMEOUT = int(os.getenv("IR_WAF_SOLVE_TIMEOUT", "150"))

# 🔴 2026-10-13：注册**主路径**用真实浏览器（`browser_post`）而不是 requests。
#    实测：requests 路径注册一次要发 3+ 次写请求
#      （裸 POST → WAF 挑战 → 假页面解题 → 重放 → 再遇滑块页 → 转浏览器），
#    同一出口短时间内多次写 → 触发 `register/byEmail` **按出口 IP 记的** 429。
#    而真实浏览器一次就能过 WAF（acw 挑战 / `aliyun_waf_aa` 滑块都能解），
#    只发一次写请求。默认开启；置 0 可退回旧的 requests 路径。
REGISTER_VIA_BROWSER = os.getenv("IR_REGISTER_VIA_BROWSER", "1").strip().lower() in (
    "1", "true", "yes", "on")

# ── OpenXLab SSO ─────────────────────────────────────────────────
SSO_BASE = "https://sso.openxlab.org.cn"
SSO_GW = f"{SSO_BASE}/gw/uaa-be/api/v1"

# 注册时使用的应用身份（来自 discovery 活动页的跳转链接）。
#
# 分类：**公开 app id，不是秘密** —— 它每次请求都会出现在 URL / 请求体里，
# 目标站本来就看得见，也不授予任何额外能力。所以**保留默认值**
# （删了流程直接跑不起来），只是允许用环境变量覆盖，方便换活动 / 换站点。
#
# 判据（与 docs/security-conventions.md「风控标识分类表」一致）：
#   某个值"贴进公开仓库会不会让别人获得你的能力 / 把行为关联到你" ——
#   会 ⇒ 必须 env 化且**默认空**；不会 ⇒ 可以留默认值，但要写清分类理由。
CLIENT_ID = os.getenv("IR_CLIENT_ID", "dagw07mkg1bazlxzoy31")
# 🔴 2026-10-14 实测（tools/probes/probe_reg_variants.py）：
#    `source="discovery"` 是**活动维度**，站点对它单独限流 —— 带它注册恒
#    429 `{"error":"Too many request"}`（直连也一样，与 IP/代理/WAF 无关）。
#    去掉 source（或 clientId）立刻 200 `msgCode:"10000"`、正常建号
#    （实测 ssoUid 853600446 / 952000228）。所以默认**不再带 source**。
#    要回到 discovery 活动入口：IR_SOURCE=discovery。
SOURCE = os.getenv("IR_SOURCE", "")

# RSA 公钥（SPKI/DER base64，服务端静态）
SSO_PUBKEY_B64 = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCOst3X5k3uqRpKtFOfLQdh5ZyakdP0fnP6CyPs"
    "9e2BWF/Jud+BZNNWOPtm5roUu3Cf0wFbvha4uD+XxmNz/3Ea+VOrfbhIeSWX3CTZ+9oAWERz0ftF"
    "oEYTf2nAt5LORhhNHt2Wea8yMTD8GoZ/asm2GX3B/CjIa6PwVlbRHX9/bwIDAQAB"
)

# ── Discovery 平台（API Key 与额度）────────────────────────────────
DISCOVERY_BASE = "https://discovery.intern-ai.org.cn"
DISCOVERY_API = f"{DISCOVERY_BASE}/api"

# ── API Key 专用推理网关 ──────────────────────────────────────────
# 🔴 别用 chat.intern-ai.org.cn —— 那是网页版聊天后端，只认 SSO JWT，
#    且要求账号绑定手机号（-20035），拿 sk- key 打会得到 401 A0211。
#    真正接受 sk- key 的是下面这个主机，路径前缀 /v1（OpenAI 兼容）。
CHAT_API_BASE = os.getenv(
    "IR_CHAT_API_BASE", "https://discovery-api.intern-ai.org.cn/v1"
)

# TokenPlan 可用模型（2026-09-15 实测，来自 GET /v1/models）
# ⚠ intern-s1 不在其中，用它会得到 model_not_available
CHAT_MODELS = [
    "deepseek-v4-flash-0731",
    "minimax-m3",
    "deepseek-v4-flash-vision",
    "qwen3.8-27b",
    "intern-s2",
    "deepseek-v4-pro-0813",
    "Agents-A1",
    "Atria-Dawn-Preview",
    "glm-5.3",
    "kimi-k2.6",
]

# ── 活动邀请信息：已删除（2026-09-19 安全重构）──────────────────────
# 原来这里写死了 `INVITER_USER_ID` / `INVITER_USERNAME` / `ACTIVITY_PATH`。
# 经全库反查（按变量名 + 按值各查一遍），三者**从未被任何代码引用** ——
# 纯死代码，却把一个**邀请码身份**永久嵌在公开仓库里：
#   · 任何人 clone 都能看到"谁邀请的"
#   · 会让所有使用者的注册都归到同一个邀请人头上
#
# 🔴 将来确实需要邀请参数时，**不要写回这里**：
#    走环境变量（默认空），并在 docs/security-conventions.md 的
#    「风控标识分类表」里登记。邀请码属于"能把行为和某个身份关联起来"的标识，
#    与出口 IP 同级 —— 不进仓库。


# ── 行为参数 ──────────────────────────────────────────────────────
# 邮件实测在注册后 3 秒内到达；轮询间隔 0.8s 可在 1~2 次内命中，
# 而收信接口单次往返 ~600ms，再密就只是在打 Cloudflare。
MAIL_POLL_INTERVAL = 0.8    # 秒
MAIL_POLL_TIMEOUT = 15      # 秒
# 🔴 2026-10-18 从 120 降下来：实测激活邮件 2~3s 内到达；收不到就是永远
#    收不到（发信侧域名拉黑 —— temp-mail.io 的域一天 40+ 封烧穿信誉）。
#    而且激活邮件已**不是必需环节**（未激活账号实测可直接登录，见
#    pipeline stage_register 的 mail-timeout 分支）。等 120s = 每号白烧
#    2 分钟。15s：等正常邮件绰绰有余，异常时快速放行走登录。
#
# 🔴 2026-09-19 改造：收信改走 `/api/inbox?email=`，**不再走 `/admin/all`**。
#
#   为什么必须换（两条，第二条才是要命的）：
#     1. 读配额：实测 `rows_read` —— `/admin/all?limit=50` 读 **51 行**，
#        而 `/api/inbox?email=`（命中 1 封）只读 **2 行**、没命中读 **0 行**。
#        09-18 D1 读配额被烧到 173.9%（8,695,305 / 5,000,000）就是这个接口干的。
#     2. **窗口截断**：`/admin/all` 返回的是「最新 N 条」，而 D1 的 retention
#        只保留 100 行。实测这张表被**同机另一个项目**（grok 注册线，共用同一个
#        Worker）以 **21.6 封/小时**灌满 ⇒ 整表每 4.6 小时被冲刷一遍。
#        我们的激活邮件一旦被挤出「最新 N 条」窗口就**永远读不到**。
#        `/api/inbox` 按收件人索引查，别人的邮件挤不掉我们的。
#
#   下面这两个常量现在**只服务于向后兼容**（`list_mails()` 不传 email 时的
#   退回路径）。收信主路径已经不需要"窗口开多大"这个折中了 ——
#   索引查询只返回这一个收件人的邮件。
MAIL_LIST_LIMIT = 50        # 仅退回路径用（`list_mails()` 不传 email 时的默认 limit）
REQUEST_TIMEOUT = 30        # 秒

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)

def _find_chrome_path() -> str:
    """定位 Chrome：环境变量 > Program Files > LOCALAPPDATA（用户级安装）。

    🔴 2026-10-02 实测：本机 Chrome 装在 `%LOCALAPPDATA%\\Google\\Chrome\\...`，
    而默认写死的 `C:\\Program Files\\...\\chrome.exe` **不存在** —— 有头模式
    一直在用一个不存在的路径（注册激活都过、登录却起不来）。逐路径探测。
    """
    for c in (
        os.getenv("IR_CHROME_PATH", "").strip(),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome",
                     "Application", "chrome.exe"),
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ):
        if c and os.path.exists(c):
            return c
    return os.getenv("IR_CHROME_PATH", "").strip() or \
        r"C:\Program Files\Google\Chrome\Application\chrome.exe"


# 本机 Chrome（Playwright 驱动，避免下载额外浏览器）
CHROME_PATH = _find_chrome_path()


# ── 注册配额保护（本地累计计数）───────────────────────────────────
# 🔴 `register/byEmail` 有**两层**限制，别只看到速率那层：
#     ① 瞬时速率：4 路并发同时到达 → 3 路立刻 429
#        → 由 `pipeline.REG_MIN_INTERVAL` 闸门控制
#     ② 累计配额：同一时段累计约 40 个后开始 `B0000 请求频繁`，
#        之后连**单账号**都注册不了
#        → 闸门**完全无效**，只能主动停下（见 `src/quota.py`）
#
# ⚠ 窗口长度的实测依据（2026-09-15 两次观测，**推翻了最初的"数分钟"假设**）：
#     最后成功注册 14:05:43 → 22:39（**8.6 小时后**）单账号探测**仍然 B0000**。
#     所以恢复窗口 **> 8.6h**，最初取的 6h **不是保守、而是太短** ——
#     它会让保护在服务端仍封着时放行。
#     现在取 24h 作为**保守上界**（很可能是"每天 N 个"这类日历窗口，
#     真实恢复点待测；滚动 24h 在恢复时间未知时只会多拦、不会漏拦）。
#
# 下面两个值决定 ② 的保护策略。默认上限来自实测（触顶点约 40 个）。
# ⚠ 这是本地保护，不是权威计量：换机器 / 删 state 文件都会重置。
#   被误拦时用 `run.py --ignore-quota` 或调大 `IR_REG_QUOTA_MAX` 放行。
REG_QUOTA_MAX = int(os.getenv("IR_REG_QUOTA_MAX", "40"))
REG_QUOTA_WINDOW_H = float(os.getenv("IR_REG_QUOTA_WINDOW_H", "24"))


# ── 出口代理（换 IP 绕过 IP 维度的封禁）───────────────────────────
# 注册封禁是 **IP 维度**（见 README「封禁是 IP 维度」），所以换出口 IP 是对症的解法。
#
# 🔴 实测背景（2026-09-16）：本机默认出口是 **机房 IP**（203.0.113.20，
#    NTT America / 天风通信，洛杉矶）—— 这类 IP 信誉差，很可能就是被封的原因。
#    住宅 IP（Google Fiber / Spectrum 等家宽）信誉高得多。
#
# 格式（与常见代理商控制台一致）：
#     IR_PROXY=host:port:user:pass          # 单条
#     IR_PROXY=http://user:pass@host:port   # 也接受完整 URL
#
# ⚠ 三条硬规矩（都是实测踩出来的，见 `tools/probes/probe_proxy.py`）：
#   1. **别用 TCP 连通性判断代理是否可用**。本机跑着 Clash TUN，连接
#      `203.0.113.30:764` 其实是连到本地虚拟网卡（实测 0.02s —— 中国到
#      美国不可能是 20ms）。必须真的发一个请求拿到出口 IP 才算数。
#   2. **别只看状态码**。代理的域名 ACL 拒绝时返回 `403` +
#      `errorMsg: <host>:80 not accessible`，正文才是判据。
#   3. **必须拿真实目标域名试**。一个代理能通 google/baidu，不代表能通
#      `sso.openxlab.org.cn` —— 实测有代理商精确屏蔽了 `openxlab.org.cn`
#      和 `intern-ai.org.cn`。
IR_PROXY = os.getenv("IR_PROXY", "").strip()

# ── 槽位代理池（一槽一端口，用于绕 IP 维度封禁）────────────────────
# 🔴 这是本项目的**规模化杠杆**：注册封禁是 IP 维度累计配额，
#    而旧实现只有一条 `IR_PROXY` —— 也就是整个批次共用**一个**出口 IP，
#    `workers` 调到几都一样会撞配额。真正的约束是"有多少个不同出口 IP"。
#
# 来源优先级：`IR_PROXY_SLOTS_FILE` > `IR_PROXY_SLOTS`。
# 槽位多时（20~50 个）用文件，别把 50 个 URL 塞进环境变量。
IR_PROXY_SLOTS = os.getenv("IR_PROXY_SLOTS", "").strip()
IR_PROXY_SLOTS_FILE = os.getenv("IR_PROXY_SLOTS_FILE", "").strip()
# 一个槽位被判"这个 IP 被目标站点封了"后，冷却多久才重新启用（秒）。
# 参考 aBaiFreeGPT 的 `MIHOMO_NODE_COOLDOWN_SECONDS=120`。
# ⚠ 这是**基础**冷却：同一槽位反复被封时按 2 的幂退避（见下一条）。
IR_PROXY_COOLDOWN = float(os.getenv("IR_PROXY_COOLDOWN", "120") or 120)

# 🔴 反复被封时冷却退避的**封顶**（秒），默认 6 小时。
#
# 为什么需要退避（2026-09-19 补）：基础冷却 120s，而服务端配额窗口是
# **24h**（`REG_QUOTA_WINDOW_H`）—— 差 720 倍。而 `quota.record()`
# **只在注册成功时调用**，封禁**不写**配额台账，于是 120s 后本地计数没涨、
# `pipeline` 侧的 `accept` 照样放行 ⇒ 这个已被封的出口被重新租出去，
# 再打一次注定失败的请求 —— **每 120s 一次，一天约 720 次**，每次都在加深封禁。
#
# 退避序列（base=120 / cap=21600）：
#     120 → 240 → 480 → 960 → 1920 → 3840 → 7680 → 15360 → 21600（封顶）
# 即"一天 720 次"压到"一天 6 次左右"。
#
# ⚠ 封顶取 6h 而不是 24h 的理由：这是**没有证据时的猜测**，不能猜得太激进。
#    取 24h 等于"封一次就整天不用它"，而单次 B0000 有可能是服务端抖动
#    （`pipeline.QUOTA_STREAK_STOP` 取 2 就是同一个理由）。
#    6h 让"真被封"的出口基本退出本轮，同时"偶发抖动"的出口不至于被长期闲置。
IR_PROXY_COOLDOWN_MAX = float(os.getenv("IR_PROXY_COOLDOWN_MAX", "21600") or 21600)
# 等一个空闲槽位最多等多久（秒）。全部槽位都在冷却时 `acquire()` 会阻塞，
# 这个值就是它的上限 —— 超时抛 `TimeoutError`，那个任务按失败记账。
# 🔴 别设成 0/无穷：0 会让"全冷却"瞬间变成一片假失败；
#    无穷会让批量任务永远挂在那里，看不出是卡死了。
IR_PROXY_SLOT_TIMEOUT = float(os.getenv("IR_PROXY_SLOT_TIMEOUT", "240") or 240)
# 起飞前是否检查槽位监听端口是否活着（`1` 开 / `0` 关，默认开）。
# 🔴 为什么默认开：槽位实例是**独立的前台进程**，很容易"配置还留着、进程已经没了"。
#    那种状态下每条注册记录都会以**代理连接错误**收场，而在这个项目里
#    "注册全失败"最容易被误读成"换 IP 也不行 / 还在封" —— 结论完全错。
#    6 个本地端口的 TCP 连通检查是毫秒级的，代价可以忽略。
IR_PROXY_PREFLIGHT = os.getenv("IR_PROXY_PREFLIGHT", "1").strip().lower() \
    not in ("0", "false", "no", "off")

# 起飞前**出口可达性**预检（`1` 开 / `0` 关，默认开）。
# 🔴 端口活 ≠ 出口通：槽位进程监听着本地端口（TCP 连通），但代理链的**下游出口**
# 可能已被墙 / 被 ACL 拦 / 节点过期 —— 那种状态端口检查永远通过，每条注册却都以
# "代理连接/超时"收场，和"配置丢了"完全分不清。这里在端口检查之后再**真打一枪**
# 目标站 `/register` 验证出口能到，打不到的槽位当场剔除。代价是每条槽位多一次
# HTTPS 握手（秒级），起飞前付一次，换来"假活槽位"不再污染整批注册。
IR_PROXY_PREFLIGHT_REACH = os.getenv("IR_PROXY_PREFLIGHT_REACH", "1").strip().lower() \
    not in ("0", "false", "no", "off")


def proxy_slots() -> list[str]:
    """读槽位清单。**没配置就返回空列表**（调用方据此退回单代理行为）。

    文件里允许一行一个，也允许逗号分隔；`#` 开头视为注释。
    """
    raw = ""
    if IR_PROXY_SLOTS_FILE:
        p = Path(IR_PROXY_SLOTS_FILE)
        if not p.is_file():
            raise ValueError(f"IR_PROXY_SLOTS_FILE 指向的文件不存在：{p}")
        raw = p.read_text(encoding="utf-8")
    elif IR_PROXY_SLOTS:
        raw = IR_PROXY_SLOTS
    out: list[str] = []
    for line in raw.replace("\n", ",").split(","):
        item = line.strip()
        if item and not item.startswith("#"):
            out.append(item)
    return out


# ── 槽位端口 -> 出口 IP（配额记账的维度）────────────────────────────
# 🔴 配额必须按**出口 IP** 记，绝不能按槽位号（`slot1`/`slot2`）记。
#
#    为什么：目标站点是按 **IP** 封的，而 `slots.txt` 的条目顺序随时会变
#    （加出口、删死掉的出口、调顺序）。用"位置号"当 scope 时，
#    `slots.txt` 一改动，既有记录就**整体错配到别的 IP 头上** ——
#    而且不报错、不抛异常，只是让某个 IP 偷偷超限、另一个被提前停掉。
#
#    本项目实测已经踩到过：`slots.txt` 从 4 项变 6 项又变 3 项的过程中，
#    台账里出现了 3 条错配（某个 scope 混进了别的槽位的记录）。
#    用出口 IP 当 scope 后，池子顺序怎么排都不影响记账。
#
# 🔴 这张表**不进仓库**（出口 IP 属于基础设施标识，公开可被搜索，
#    节点容易被盯上）。放在 `.env` 里：
#
#        IR_SLOT_EGRESS_IPS=7901=10.0.0.1,7902=10.0.0.2
#
# 🔴 换订阅 / 换节点 / 换机房之后这张表就过期了，必须重测：
#        python tools/probes/probe_slots.py
#    改完还要**同步迁移台账**（否则旧记录挂在旧 IP 名下）：
#        python tools/data/migrate_quota_scope.py --apply
IR_SLOT_EGRESS_IPS = os.getenv("IR_SLOT_EGRESS_IPS", "").strip()


def _parse_slot_egress(raw: str) -> dict:
    """把 `7901=1.2.3.4,7902=5.6.7.8` 解析成 `{端口: IP}`。

    容忍换行分隔与空项；格式不对的项直接跳过（不抛），
    因为缺项会在 `slot_scope()` 里被更明确地报出来。
    """
    out: dict = {}
    for item in raw.replace("\n", ",").split(","):
        item = item.strip()
        if not item or item.startswith("#") or "=" not in item:
            continue
        port, _, ip = item.partition("=")
        port, ip = port.strip(), ip.strip()
        if port and ip:
            out[port] = ip
    return out


SLOT_EGRESS_IPS: dict = _parse_slot_egress(IR_SLOT_EGRESS_IPS)


def slot_scope(url: str) -> str:
    """把槽位 URL 映射成**配额记账的 scope**（= 那个槽位的出口 IP）。

    出口 IP 才是目标站点真正封的那个东西，也是唯一不随配置漂移的标识。
    用位置号（`slot1`/`slot2`）当 scope 是错的 —— 见 `SLOT_EGRESS_IPS` 的说明。

    🔴 映射键有两种模型（2026-09-30 起）：

      - **mihomo 本地多端口**：`127.0.0.1:7901`（无账密）→ 键 = **端口**。
        一个端口一个出口，`IR_SLOT_EGRESS_IPS=7901=1.2.3.4,...`。
      - **同端点多会话**（Resin 这类代理网关）：`http://user:@host:2268`，
        10 条槽位**共用同一个端口**、靠用户名区分出口 → 键 = **用户名**。
        `IR_SLOT_EGRESS_IPS=Link.rl2.abc=1.2.3.4,Link.rl2.def=5.6.7.8,...`。
        按端口做键的话 10 条会全部映射到同一个 IP —— 配额全记进一个桶、
        同出口互斥把并发压成 1，整个槽位池等于白配（实测踩过）。

    🔴 键不在映射表里时**大声报错，不退回位置号**：
    静默错配会让某个 IP 悄悄超过上限（真被目标站封），
    比"跑不起来、逼你去补映射"危险得多。
    """
    key = slot_key(url)
    ip = SLOT_EGRESS_IPS.get(key)
    if not ip:
        # 🔴 回退：按**订阅链接本身**记账，不再抛异常（2026-10-04 改动）。
        #
        #    背景：用户给的订阅链接（Resin 这类网关）**出口 IP 会定时自动变**。
        #    原实现在"键不在 IP 映射表里"时直接抛 ValueError，意味着每换一批
        #    订阅链接（或 IP 一漂）就必须重新探测并维护 IR_SLOT_EGRESS_IPS，
        #    否则整个注册直接崩 —— 这不是保护，是把活儿推给人肉运维。
        #
        #    为什么退回按链接记账是安全的：配额的本质是"这条订阅链接还能注册
        #    几个"，而目标站限流看的是它当时那条出口。出口会漂 ⇒ IP 桶本身
        #    就是个会失效的键；订阅链接（用户名）才是稳定标识。
        #    用链接当 scope：每条链接一个独立配额桶，换链接即换桶，语义干净。
        #
        #    ⚠ 与"同出口 IP 互斥"的关系：那个互斥只在**显式配置了
        #    IR_SLOT_EGRESS_IPS** 时才启用（见 proxypool 的 slot_ips）。
        #    出口会漂的场景本就不该启用它 —— 所以这里回退与那边是自洽的。
        if not key:
            raise ValueError(f"槽位 URL 解析不出映射键：{redact.redact_url(url)}")
        return f"slot:{key}"
    return ip


def slot_key(url: str) -> str:
    """槽位 → 映射表键：带 `@` 取**用户名**（同端点多会话模型），
    否则取端口（mihomo 本地多端口模型）。解析不出返回空串（调用方报错）。"""
    raw = (url or "").strip()
    if not raw:
        return ""
    if "@" in raw:
        cred = raw.split("://", 1)[-1].rsplit("@", 1)[0]
        return cred.split(":", 1)[0]
    hostport = raw.split("://", 1)[-1].split("/", 1)[0]
    return hostport.rpartition(":")[2]


def proxies(raw: str = None) -> dict | None:
    """把代理串解析成 requests 的 `proxies` 字典；空则返回 `None`。

    支持两种写法：
        host:port:user:pass          → http://user:pass@host:port
        scheme://user:pass@host:port → 原样使用（可指定 socks5）

    `raw` 传 None 时用全局 `IR_PROXY`。**传具体值时不碰全局状态** ——
    槽位池在并发场景下必须这样用，否则多线程改 `IR_PROXY` 会互相踩。
    """
    raw = (IR_PROXY if raw is None else raw).strip()
    if not raw:
        return None
    if "://" not in raw:
        parts = raw.split(":")
        if len(parts) == 4:
            host, port, user, pwd = parts
            raw = f"http://{user}:{pwd}@{host}:{port}"
        elif len(parts) == 2:
            raw = f"http://{raw}"
        else:
            raise ValueError(
                f"代理串格式无法识别：{raw!r}（期望 host:port:user:pass "
                f"或 scheme://user:pass@host:port）")
    return {"http": raw, "https": raw}


def _fill_empty_proxy_pwd(raw: str) -> str:
    """⚠ **不要接进 `proxies()`** —— 2026-10-14 实测这是个错误的修复。

    曾以为 Resin 槽位的空密码（`http://user:@host:port`）会让 requests 的
    CONNECT 被回 407，于是补了占位符 `x`（Camoufox 侧确实需要，见
    `src/browser/launch.py`）。但**requests 侧补了反而全灭**：改前出口预检
    5/10 可达，改后 **40/40 全部失败**。即网关对 requests 这条链接受的
    就是空密码，多写一个 `x` 反而认证不过。

    函数保留在此，仅作为"已证伪"的记录与将来的对照，不要调用。
    """
    if "://" not in raw or "@" not in raw:
        return raw
    scheme, rest = raw.split("://", 1)
    userinfo, host = rest.rsplit("@", 1)
    if ":" in userinfo:
        user, pwd = userinfo.split(":", 1)
        if user and not pwd:
            return f"{scheme}://{user}:x@{host}"
    return raw


def apply_proxy(session, proxy: str = None) -> None:
    """把代理挂到 `requests.Session` 上（未配置则什么都不做）。

    `proxy` 传 None 时用全局 `IR_PROXY`；传具体值则只作用于这个 session
    （槽位池并发场景必须走这条，见 `proxies()` 的说明）。

    🔴 **必须同时关掉 `trust_env`** —— 否则环境变量会**静默盖掉**
    `session.proxies`。这是实测踩出来的（2026-09-16），机制在
    `requests/sessions.py` 的 `merge_environment_settings`：

        env_proxies = get_environ_proxies(url)      # 先读环境变量
        for k, v in env_proxies.items():
            proxies.setdefault(k, v)                # ← 环境变量先进字典
        proxies = merge_setting(proxies, self.proxies)   # ← 已存在的键不覆盖

    所以 `session.proxies` 的优先级**低于**环境变量。本机有
    `HTTP_PROXY=http://127.0.0.1:7897`（Clash），于是实测：

        session.proxies = {...}                    → 被忽略，走了 Clash  ✗
        session.proxies = {...} + trust_env=False  → 生效               ✓
        session.get(url, proxies={...})            → 生效（per-request 优先级最高）

    现象特别隐蔽：请求**成功了**（200），只是**没走你指定的代理** ——
    既不报错也不告警，很容易误判成"代理已生效"。

    ⚠ 未配置代理时**不动 `trust_env`**：那样环境代理（Clash）仍是
      默认出口，与加这个功能之前的行为一致。
    """
    px = proxies(proxy)
    if not px:
        return
    session.proxies = px
    session.trust_env = False


# ── 启动校验 ──────────────────────────────────────────────────────
def validate(*, need_worker_token: bool = True) -> list[str]:
    """返回缺失 / 有问题的必需配置项（空列表 = 就绪）。

    刻意**不在 import 时抛错** —— 那样连 `--help` 和离线分析都跑不起来。
    由入口显式调用，报错时直接给出修法。
    """
    missing = []
    if MAIL_PROVIDER in ("temptf", "tempmailio"):
        # temp.tf / temp-mail.io 都免注册免 token —— 只校验后端名本身。
        pass
    else:
        if need_worker_token and not WORKER_ADMIN_TOKEN:
            missing.append("IR_WORKER_ADMIN_TOKEN")
        # 这两项不再有写死的默认值（本仓库是公开的），缺失时在入口报错，
        # 而不是带着空 base 去发一堆注定失败的请求。
        if not WORKER_BASE:
            missing.append("IR_WORKER_BASE")
        if not WORKER_DOMAIN:
            missing.append("IR_WORKER_DOMAIN")
        # 提示有退路：没 Worker 也能跑（很多人卡在这一步就以为项目跑不起来）
        if missing and not TEMPTF_BASE:
            missing.append("（或设 IR_MAIL_PROVIDER=temptf 走公共临时邮箱）")

    # 配了槽位池却没给「端口 -> 出口 IP」映射：`slot_scope()` 会在**第一个任务**
    # 才抛错，那时已经跑了一半。提前到启动阶段报，并指路到探测器。
    # ⚠ 这里吞掉 `proxy_slots()` 的异常 —— 槽位文件不存在也是配置错误，
    #   但用"缺项"的形式报出来比抛 traceback 可读得多。
    try:
        has_slots = bool(proxy_slots())
    except (ValueError, OSError):
        missing.append(f"IR_PROXY_SLOTS_FILE（指向的文件读不到：{IR_PROXY_SLOTS_FILE}）")
        has_slots = False
    if has_slots and not SLOT_EGRESS_IPS:
        # 🔴 不再是"缺配置"错误（2026-10-04 改动）：订阅链接的**出口 IP 会定时
        #    自动漂**，`IR_SLOT_EGRESS_IPS` 只在"出口固定、需要按 IP 记配额 /
        #    做同出口互斥"时才配。不配 → `slot_scope()` 退回按**订阅链接**记账
        #    （见那里的说明），功能完全正常，不该拦在启动阶段。
        #    这里只留一条提示，告知当前是哪种记账口径。
        pass
    return missing

