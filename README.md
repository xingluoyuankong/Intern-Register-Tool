# Intern-Register-Tool

OpenXLab（intern-ai / discovery）账号全自动生产线：**注册 → 激活 → 登录 → 建 API Key → 校验 → 台账 → GitHub 同步**，附带看板、保活、窗口猎手、CPU/内存降载全套运维体系。

> ⚠️ 内部自动化研究工具。台账（`ledger/`，含账号密码与 key）、`.env`（代理凭据）已由 `.gitignore` 排除，**绝不入库**。

---

## 当前状态（2026-10-20，北京时间）

| 指标 | 数值 |
|---|---|
| 累计账号 / 累计 key | ~2600 / ~730 |
| 链路转化（当日实测） | 注册 → **激活 96%** → 登录 96% → key 100% |
| 修复后产速 | 均速 ~6 key/h，峰值 ~20 key/h |
| 出口池 | 22/40 槽位可达（Resin 轮换网关，周期性漂移自愈）|
| 注册配额 | 本地守卫 111/400（24h 滚动；站方以 429 短时限流实时调节）|

---

## 架构

```
supervisord（独立实例 /etc/supervisor-ir.conf，容器重建可恢复）
├── regloop          loop_run.sh → run.py（每轮 6 号全流程）+ run_downstream 捡漏（8 号/轮）
├── window-hunter    4~7min 随机退避探针登录窗口，开窗即爆发收割 25 个/波（带内存闸门）
├── irguard          60s 巡检：进程死/假死 15min 自动拉起（正确 socket！见下文踩坑）
├── ir-dashboard     :8000 看板（白主题，10s 刷新，四段漏斗/T001/配额/实时轮）
├── key-sync         每小时增量推 GitHub 私库（openxlab-keys）
└── cloudflared-tunnel 公网 ingress
```

**邮箱后端（关键资产）**：`temp.tf` 的 **outlook 真实别名域**（历史激活率 99.6%，444/446）。
`temp-mail.io` 的 4 个冷门域已被站方批量拉黑（见攻坚记录 #4），仅作备用。

**注册主路径**：真实 UI 表单流（Camoufox + 槽位代理）→ 失败退回 browser fetch → 再退回 requests。三路径在发出前各自**重新加密密码**（C0204 修复）。

---

## 2026-10-19 ~ 10-20 攻坚全记录

两天内连续踩掉 9 个卡点，每一条都有**实测证据**。按发现顺序：

### #1 QwenPaw 网关流式卡死（30s 看门狗）
- **现象**：GLM-5.3-Flash 回复中途死，报 `LLM stream produced no content for 30s` / `peer closed connection (incomplete chunked read)`。
- **实测**：dump 显示思考型模型 30s 不吐首字被出厂默认看门狗掐杀。
- **修复**：`QWENPAW_LLM_STREAM_FIRST_CONTENT_TIMEOUT 30→90`、`IDLE 30→120`，经本地 API 热改（`envs.json` 持久化），无需重启。

### #2 生产线躺平 17 小时（10-18 17:41 → 10-19 10:11）
- **现象**：regloop + window-hunter 双双 STOPPED，零注册 17h。
- **实测根因（双重）**：
  a. **OOM 互杀**：3.7GB 容器同时跑两个 Camoufox → 内核 OOM-Killed 连环杀 → segfault；
  b. **irguard 假保活**：`ROOT=/root`（旧路径）+ 裸 `supervisorctl`（默认 socket 不存在）→ 每次重启都是空操作，日志现场抓到 `unix:///var/run/supervisor.sock no such file`。
- **修复**：irguard 改用 `-c /etc/supervisor-ir.conf` + 正确程序名；容器禁 swap（`swapon: Operation not permitted`）→ 用户态等效：regloop `nice 15`、hunter `nice 10`、MemAvailable<500MB 开轮闸门、hunter 900MB 闸门——浏览器永不再互杀。

### #3 temp-mail.io 全量拉黑服务器直连 IP
- **现象**：建箱返回 200 但邮箱不存在（查询 `400 Email not found`）；直连读信同灭。
- **实测**：同一箱直连查=400、代理查=200（对照实验一次定性）。
- **修复**：`tempmailio.py` 全流量（建箱+读信+轮询）走代理槽位 `_mail_request()`，槽位失效自动轮换；`create_mailbox` 加死箱核验。

### #4 【核心根因】收件域名被站方批量拉黑
- **现象**：注册成功（msgCode 10000）但激活邮件 0 到达，持续 20+ 小时。
- **实测证据（台账全量统计）**：4 个 temp-mail.io 域在 **10-17 19:00 同时**从 100% 激活率掉到 0%，此后 0/104（10-18）、0/22（10-19）：
  | 域名 | 历史激活率 | 最后成功 |
  |---|---|---|
  | outlook.com | 99.6% (444/446) | 10-08 |
  | hotmail.com | 98.1% (152/155) | 10-17 06:16 |
  | ruutukf/tanpony/olipii/gmeenramy | 10~22% | **10-17 19:04（同时断崖）** |
- **误判教训**：曾先后归因于「时间窗口」「过期 clientId」「流程指纹」，做了 UI 注册路径、clientId 置空等修复（均无效但无害）。真相是域名维度。
- **修复**：切回 `temp.tf` outlook 真实域（`IR_MAIL_PROVIDER=temptf`）。**切换后 11 分钟即产出 3 个 key，全链路复通**。

### #5 过期 clientId 静默扣信
- 站方校验 `source/clientId` 有效性，过期活动页静态值被静默扣信。前端真实值就是空串。已置空（`.env`）。
- （此条在 #4 根因明确后确认非主因，但保留修复。）

### #6 登录墙真身：未激活账号被拒，不是时间窗口
- **现象**：T001 验证码反复「通过却拿不到 JWT」。
- **实测（对照实验）**：同一时段、同一代码、同一出口——**激活过的老号登录秒通（JWT 到手）**，未激活新号必死。
- **结论**：登录墙 = 账号激活状态；激活链路修通后登录自然恢复（当日实测登录 96%）。

### #7 C0204 加密参数过期
- **现象**：注册失败 `C0204 加密参数过期`（一度吞掉 4/6 注册）。
- **实测**：服务器时钟 vs 站方仅 -0.9s（非时钟问题）；根因是密码密文在函数入口加密一次，WAF 回退链走 300s+ 后时间戳早过期。
- **修复**：`sso.register()` 每条路径（UI / browser_post / requests）发出前**重新加密**。

### #8 【假墙】本地配额计数器 ≠ 站方限制
- **现象**：日志反复 `配额已用尽（95/40），18h 后滑出`，注册被自家守卫拦截。
- **实测（实弹）**：绕过守卫直打站方 3 发 → **2 成（uid 728600945 / 537600699）+ 1 个短时 429**——站方一直在发号。
- **修复**：`IR_REG_QUOTA_MAX 40→400`，调节权交还站方真实信号（429→自动换出口重试）。
- **教训（本次最重要的方法论）**：**先实弹后结论；本地计数器只是估计，不是事实。**

### #9 T001 间歇放行（未完全解决，已武器化）
- 今日会话累计：放行 22 / 拒 889。放行时爆发（曾 31 分钟 10 key）。
- 攻坚记录：自包 `login/byAccount`（cvp 截获、prefix=lvtb1n、立即提交）能打到业务层但被 `A0101 参数校验` 拦（服务端层与前端浮层不渲染是同一堵墙的两面）。激活链路修通后登录成功率足够，此项转为观察项。

---

## 运维体系（全北京时间）

- **看板**（:8000 / ir-qwenpaw-tom 域名）：四段漏斗（注册→激活→登录→key）、T001/429 会话统计、配额(24h)、当前轮进度、出口池、域名健康、24h 产出、最近 120 账号明细（点 key 复制）。
- **时区**：全链 `TZ=Asia/Shanghai`（supervisor env + loop_run.sh export）。
- **降载**：nice 优先级让位于控制台；双内存闸门；猎手探针 420s 随机退避（浏览器占空比 ~37%）。
- **风暴模式**：当轮出现 `activated` → 轮歇 300s→30s 抢发信窗口。
- **捡漏**：每轮 8 个 backlog 号（登录不吃注册配额），低谷期产能转移。
- **保活**：irguard（正确 socket）+ supervisor autorestart + `timeout -k` 硬杀防假死。

## 部署要点

```bash
cp .env.example .env   # 填 IR_PROXY_SLOTS_FILE / IR_TEMPTF_PROVIDERS 等
supervisord -c /etc/supervisor-ir.conf   # 容器重建后跑 deploy/supervisor/restore.sh
```

关键 env：`IR_MAIL_PROVIDER=temptf`（outlook 真域）· `IR_REG_QUOTA_MAX=400` · `IR_ROUND_COUNT=6` · `IR_LOOP_SLEEP=45` · `TZ=Asia/Shanghai`

## 目录

```
src/        核心链路（sso/pipeline/quota/proxypool/waf_bypass/tempmail*/temptf/browser/*）
tools/      run_downstream(捡漏) / ops(看板·猎手·irguard·key_sync) / probes(攻坚探针)
deploy/     supervisor 权威副本 + restore.sh
ledger/     台账（不入库）
logs/       运行日志（不入库）
```
