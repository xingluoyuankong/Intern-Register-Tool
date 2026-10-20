# WAF 指纹调查报告 — 2026-10-17

> **✅ 已解决（同日）**：接入 temp-mail.io 免费邮箱后端（`src/tempmailio.py`，
> 冷门域名可过 WAF），`.env` 切 `IR_MAIL_PROVIDER=tempmailio`。
> 当天 14:37 生产恢复出号：注册→激活→登录→领额度→建 Key→验证全链路
> 1/1 成功（uid=348400022，sk-aeba…，11 模型可用）。
> 域名会烧/轮换：改 `IR_TEMPMAILIO_DOMAINS` 一行即可，无需动代码。

## 结论（TL;DR）

注册全挂的根因**不是代理、不是 TLS 指纹、不是 IP 封禁**，而是：

> **阿里云 WAF 按「注册 POST 的邮箱域名」弹滑块验证。**
> `hotmail.com` 已被烧穿（本项目经它注册过 400+ 账号）→ 一律弹
> `aliyun_waf_aa` 滑块页；`gmail.com` / `qq.com` / `proton.me` 命中域名级
> 429；**任何"一次性邮箱域名"（阿里云维护的库）也一律弹滑块**；
> 陌生域名 / 主流域（yandex、mail.ru、zoho、163）放行。

## 指纹识别

目标站 `sso.openxlab.org.cn` 的防护栈：

| 层 | 证据 |
|---|---|
| **阿里云 WAF** | `acw_tc` 会话 cookie；`acw_sc__v2` JS 挑战；`aliyun_waf_aa`+`aliyun_waf_bb` meta 滑块页（v2.0.1 wrapper）；SDK = `o.alicdn.com/captcha-frontend/aliyunCaptcha/AliyunCaptcha.js`（**verifyType 1.0**，widget id `#aliyunCaptcha-sliding-slider`） |
| istio-envoy 网关 | `server: istio-envoy`、`x-envoy-upstream-service-time` |
| 站点改版时间点 | `/register` 页 `last-modified: 2026-10-15 10:55:35 GMT`（问题开始于此后） |

## 触发规则（同槽位同会话控制变量实测）

| 域名 | 结果 |
|---|---|
| hotmail.com | 🔴 滑块（与密码形态无关：junk 密码也弹 → **纯域名规则**） |
| grr.la / sharklasers.com / maxxspace.com / mailsac / inboxkitten / temp-mailo / smailpro / pokemail / spam4.me / guerrillamail.com / outlook.com / gmx.com / yeah.net | 🔴 滑块（阿里云一次性邮箱域名库 + outlook/gmx 信誉） |
| gmail.com / qq.com / proton.me / guerrillamailblock.com | 🟡 429 "Too many request"（域名级限流窗口） |
| **yandex.com** | 🟢 **注册直接成功**（msgCode 10000） |
| mail.ru / zoho.com / 163.com | 🟢 到达业务层（A0210 用户名冲突 — WAF 放行） |
| 任意陌生域名（不存在也行） | 🟢 注册成功 |

排除项（均实测）：
- **IP 维度**：同一出口、同一会话，junk payload 过、hotmail 被拦 → 与 IP 无关
- **TLS/JA3**：requests 直连（python 指纹）POST 同样到达业务层 → 无 TLS 指纹过滤
- **密码形态**：`password=junk` + hotmail 仍弹滑块

## "JSONDecodeError: char 0" 之谜

日志里所有注册失败的 `Expecting value: line 1 column 1 (char 0)` **从来不是
空响应**——是 JSON 解析器在 16KB HTML 的第一个字符 `<` 上炸了。真实响应：
`status=200, content-type=text/html, len≈16400`，即 aliyun_waf_aa 滑块页。

## 滑块页机制（wrapper v2.0.1 逆向）

- 页面把**被拦的原始 POST payload** base64 塞进 `requestInfo.data`，带
  `token`（u_atoken 素材）、`traceid`、`sceneId`、加密 `userId/userUserId`
- 滑块成功回调里构造隐藏表单重放原始 POST，query 带
  `u_atoken=<requestInfo.token>&u_asig=<certifyId>`（**过验证凭证**）
- ⚠ wrapper 的表单重放把 body 按 `&` 拆键值——**对 JSON body 是坏的**，
  正确用法是拦截表单、偷出 query 凭证、自己用 JSON body 重放
- 现有代码 `waf_bypass._do_post` 的缺口：`set_content` 把挑战页放在
  `about:blank`（跨源、丢 cookie），重放永远失败；`is_aa` 检测存在但
  流程走不通

## 登录路径（harvest F008/T001）

登录/建 Key 的滑块是同一 WAF 体系；当前拖动行为疑似被行为检测拒绝
（拖了但 rejected）。即使修好注册侧滑块流程，行为检测仍是独立风险。

## 修复路径（按性价比排序）

1. **换邮箱域名**（WAF 域名规则绕开）：自有域 + catch-all（如用户的
   CF Worker 邮箱，`.env` 已有 `IR_WORKER_ADMIN_TOKEN`，缺
   `IR_WORKER_BASE` / `IR_WORKER_DOMAIN`）→ 新注册全链路（注册/激活/
   登录/建 Key）都带新域名，滑块整条绕开。
2. 攻滑块（同源真实页 + 解题器 + 偷凭证重放）：流程可修，但**行为
   检测**通过率未知（harvest 的拒绝记录不乐观），工程量大。
3. 等域名信誉窗口滑出（hotmail 何时解封未知，gmail 429 同理）。

## 本轮已落地的配套修复（与 WAF 无关但同轮完成）

- **浏览器前活体复查**：拿到槽位租约后、开浏览器前 GET /register 复查，
  死出口 60s 冷却换下一个，不消耗注册尝试（`pipeline.py`
  `SLOT_VERIFY_MAX=12`、`proxypool.slot_alive()`）
- **顺序轮转持久化改键**：`slot_uses.json` v2 按槽位用户名继承（旧签名
  在预检剔除后每轮清零 → 每轮都从 slot1 重新开始）；写盘合并保留池外键
- **降频**：`loop_run.sh` 轮间隔 60s → 300s
- **取证**：`waf_bypass` 打印 browser_post 最终响应（status/ct/len/head），
  非 JSON 存 `logs/waf_browser_block.html`；独立探针
  `tools/probes/waf_capture.py`
- 代理链接**一条未删**（slots_active.txt 原样），筛选全部是运行时行为
