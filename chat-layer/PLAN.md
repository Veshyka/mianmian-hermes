# 聊天/干活分层 —— 落地计划

状态：**待主人确认后开工**。本文件只是计划，尚未动任何环境。
日期：2026-09-19

---

## 0. 我的理解（四要素，请核对）

**目标**
聊天层要人味、分段多气泡、群里能认人；干活层保持严谨结构化；两边都能查/存记忆。
通道层用现成框架解决（不自研 IM 协议）。

**约束**
- 官方 QQ bot（现号）不动，留作干活门 + 兜底；新开小号走个人号协议端。
- 宿主资源紧：3060 12G 已被 extract(≈3.9G) + embed(≈5.4G) 占去约 9.5G。
- **宿主侧改动（docker/compose/新容器）由主人执行**，棉棉只准备方案与脚本。
- 凭据不进产物/记忆/聊天；面板与服务不暴露公网。
- 本机无公网 IPv4 → QQ 官方 bot 只能走 WebSocket 模式；网页回调（需公网域名+HTTPS）不可选。

**交付物**
1. 可用的双门面系统（小号聊天门 + 现号干活门，同一个 Hermes 核心）
2. 可回滚的部署/配置脚本 + 运维文档（进 ops-changelog）
3. 记忆共享与隔离规则（同一 Hindsight bank，tag 区分）

**验收标准（待确认）**
- 小号私聊：像人一样闲聊，**多条短消息**而非一大坨
- 群聊：能认出说话的人、能正确处理引用、不误接别人的话
- 干活门：现号流程与现在一致，无回归
- 记忆：两边能互相查到对方存的内容，且各自检索不被噪音污染

---

## 1. 已核实的事实

| 事实 | 出处 |
|---|---|
| Hermes 带通用 webhook 入口：HMAC 签名 POST → 渲染成 agent 提示词 → 结果回投任意平台；每路由可配 skills/投递方式/限流/幂等/体积上限/重放保护 | `/opt/hermes/gateway/platforms/webhook.py` 源码 docstring |
| OpenAI 兼容端点 `/v1`：**无状态**、支持文本+image_url、**拒绝 file 类型（400）** | 官方文档 user-guide/features/api-server |
| Hermes 有 3 种被外部程序驱动的协议：ACP、TUI gateway JSON-RPC、OpenAI 兼容 HTTP API | 官方文档 developer-guide/programmatic-integration |
| **Bot Mode**：一个 Bot = 一个 profile，各有独立模型/记忆/技能/凭证/聊天历史；能跑例程、互相发消息；headless 下需手动补 2 步才能启用 `message_agent` 工具 | 官方文档 user-guide/bot-mode |
| Hermes 可驱动外部 agent CLI（Claude Code / Codex / OpenCode）；OpenClaw 那条是迁移指南，不是实时互调 | hermes-agent 技能 + 官方文档索引 |
| AstrBot：Python，AGPL-3.0，v4.28.1（2026-09-14）；官方 QQ 适配器 + OneBot v11 反向 WS（默认 6199）；支持 MCP；有 CVSS 9.8 的 RCE（CVE-2025-55449，须 ≥3.5.18）；插件=全权限 Python | 前一轮调研（GitHub API + 官方文档 + 安全公告） |
| qq-bridge（288★，JS，v0.1.5）：QQ → SnowLuma(OneBot v11 WS) → 桥接 → **DSH Web API(127.0.0.1:3080/api)** | 仓库 README |
| dsh = **DeepSeek Harness**（`deepseek-ai/deepseek-harness`，官方开源 agent harness，Cordis 插件架构） | 官方 README.zh.md + 社区文章 |
| SnowLuma：面向 NTQQ 的远程协议框架，桥接 NTQQ 与 OneBot v11 生态，内置 WebUI + TS SDK + MCP | snowluma.github.io |
| 微信通道现状：`weixin.py` = 个人微信号，走**腾讯 iLink Bot API**（扫码、AES 加密 CDN）；受限点：必须回带对方最新 context_token → 不能主动推送 | 适配器源码 |
| **AstrBot 可被完全截断**：插件注册 `@filter.event_message_type(ALL)`，`event.stop_event()` 后 scheduler 立即 break，`ProcessStage` 里的 LLM 调用块**永不执行**（官方文档原话：后续所有步骤不会被执行…比如请求 LLM） | `core/pipeline/scheduler.py:44-80`、`core/star/astr_message_event.py:348-354`、`docs/zh/dev/star/plugin.md:909`（本地检出 `b065505`） |
| 还有 3 重保险：`event.should_call_llm(True)`（置 True 反而不走默认 LLM）、handler 里 `await event.send()` 即满足跳过条件、`provider_settings.enable=false` 进程级关 AI | `astr_message_event.py:372-377`、`pipeline/process_stage/stage.py:52-66` |
| AstrBot 插件可**反复 `event.send()` 发多条**，也可主动发（`context.send_message(umo, chain)`）；但 `event.send()` 绕过 RespondStage → **它自带的分段/t2i/前缀装饰全部不生效** | `astr_message_event.py:483`、`respond/stage.py:169-175` |
| **分段回复由两半组成**：①切分在 `ResultDecorateStage`（读 `platform_settings.segmented_reply.*`：enable / only_llm_result / split_mode=regex\|words / regex / split_words / words_count_threshold 默认 **150** / content_cleanup_rule；**字数超过阈值就不分段，直接发**）②发送节奏在 `RespondStage`（interval_method=random\|log、interval 默认 **1.5,3.5 秒**、log_base 默认 2.6） | `result_decorate/stage.py:58-80,105-127,205-245`、`respond/stage.py:64-79,91-113,131-140` |
| **分段与平台解耦**（全局 `platform_settings`，只按平台名硬排除三家：`qq_official_webhook`、`weixin_official_account`、`dingtalk`）→ 但不是"可调用的工具函数"，而是**绑在 stage 上** | `result_decorate/stage.py:208-212`、`respond/stage.py:138-145` |
| AstrBot 插件取附件：`await image.convert_to_file_path()` 自动下载到本地；`File.get_file()`；有临时文件清理钩子 | `core/message/components.py:535-545, 829-855`、`scheduler.py:99` |
| 绕过 LLM 后 AstrBot **不维护会话历史**（历史只在 agent 路径写）→ 上下文必须由桥带给后端 | `agent_sub_stages/internal.py:457`、`utils/history_saver.py` |
| **源码改不动的部分**：管线 stage 不可插件化（`register_stage` 直接 `ValueError`，stage 列表硬编码）；内置 astrbot 星无法禁用 | `scheduler.py:23-25`、`bootstrap.py:7-17` |
| qq-bridge：**不是 fork、无上游、无 LICENSE 文件**（API license=null，LICENSE 404）→ 代码不能抄，只能借鉴思路 | `api.github.com/repos/Derpyu520/qq-bridge` |
| dsh 位 = **DeepSeek Harness**：MIT、229,336★、TypeScript、2026-08-13 创建——是完整 agent 运行时（会话/工作区/MCP/preset 权限分级/审批流），不是一个模型 API | `deepseek-ai/deepseek-harness` README.zh.md |
| 前人把 IM 前端接到 agent 后端：**cc-connect** 15,560★（NapCat ↔ Claude Code/Codex/Cursor）、AstrBot 官方「Agent 执行器」、DSH 生态几十个 QQ 桥；**未查到**有人用 Hermes `/v1` 接 NapCat/AstrBot 的公开案例 | 各仓库 README |

---

## 1.5 前例与可抄清单

| 先例 | 数据 | 对我们的意义 |
|---|---|---|
| **AstrBot 官方「Agent 执行器」** | 官方 wiki | "框架当通道、外部当 agent"是官方化设计（可接 Dify/Coze/百炼/DeerFlow，也可自研）→ 我们写转发插件属正规姿势 |
| **astrbot_plugin_hapi_connector** | 195★ / 448 commits / AGPL-3.0 / 活跃 | 最接近我们形态的成熟实现：AstrBot 插件 + SSE 长连接 + REST 操作会话 + **权限审批 + 文件双向传输**；后端是 HAPI（Claude Code 一族） |
| **cc-connect** | 15,560★ / Go | "IM 前端 + 本地 agent 后端"最有分量的先例，13 个平台 |
| **zxx624/astrbot_plugin_hermes_ecosystem** | ⭐4 | 现成把 Hermes 的 OpenAI 兼容 API 注册成 AstrBot provider（走它自己的 LLM 管线）——可拿来参考接线 |
| **konodiodaaaaa1/astrbot_plugin_hermes_connector** | ⭐4 | QQ/微信/TG 上操控 Hermes |
| **wsz987/dsh-channels** | DSH 生态 | IM 文件收发 + agent 读 PDF/DOCX/XLSX → 正好补"Hermes `/v1` 不吃 file"的短板 |

**可抄的 4 条架构约定**：
1. 一个 IM 会话 = 一个 agent 会话 + 持久映射 + 工作区归组
2. preset 决定工具权限 + **fail-closed 白名单**（群聊会话绝不能拿到 bash/文件工具）
3. 让 agent 用工具**反向**往 IM 发消息（桥只提供 `qq_get_unread_messages` / `qq_send_message` / `qq_wait_for_messages`），而非单向转发
4. 附件在桥侧解析，后端只收文本/图片

**别人踩过的坑（qq-bridge 审计记录，当检查单用）**：
- 会话与权限必须绑定：模式/preset 一变就退役旧会话并清权限元数据
- 断线重连：事件 follow 集合必须跨重连重放，否则老会话**永久静默**
- preset 解析失败**必须 fail-closed**；回退默认 = 把带 bash 的 agent 丢给群聊
- 把 agent 接进 QQ = 把账号交给模型 → 白名单 + 默认拒绝

---

## 1.6 ★ 分段借用的正确姿势（重要修正）

**结论：分段能白拿，但接管方式必须换。**

| 接管方式 | 效果 |
|---|---|
| ❌ `event.stop_event()` + `event.send()` | 完全接管、最快，但**跳过 ResultDecorate + Respond** → 分段、回复前缀、t2i、平台装饰全部失效，全得自己写 |
| ✅ **只跳 LLM、不掐断事件**：`provider_settings.enable=false`（进程级关 AI，杜绝一切 LLM 调用）或 `event.should_call_llm(True)`，handler 里正常 `yield event.plain_result(回复)` | 结果进入消息链 → **切分 + 多条间隔节奏 + 前缀装饰照常生效**，等于白借 AstrBot 的分段层 |

**借到的具体是什么**：字数阈值（默认 150，超长不分段）、正则/词表两种切法、内容清理规则、以及"像人打字"的间隔算法（随机 1.5–3.5 秒，或按字数取对数）。自己写不难，但琐碎——**这就是 AstrBot 相对"自写轻桥"的具体增量**。

**代价与注意**：
- 不掐断事件 → 同一消息上其它插件的 handler 也会跑（可能重复回复）⇒ 不装其它聊天类插件；必要时对冲突插件单独处理。
- 分段对三家平台不生效（`qq_official_webhook` / 公众号 / 钉钉）——我们若走 OneBot 协议端不在名单里，正常生效。

---

## 2. 架构定稿（双门面）

```
小号 QQ ──► NapCat(协议端) ──► AstrBot(通道层) ──┐
                                    ▲            │  插件走 HTTP
                                    │            ▼
                             回复交回管线     Hermes gateway（同一进程，multiplex）
                          （分段/节奏白借）        ├─ profile: chat    ← 聊天（人味/分段/表情包/派活）
现号 QQ 官方bot ─────────────────────────────────►├─ profile: default ← 干活（现状不动）
微信 iLink ──────────────────────────────────────►└─ Hindsight（同一 bank，tag 隔离）
```

**接线定稿（零配置改动、已实测）**
- 新容器用 `network_mode: host`（与 hermes 容器一致）→ 直连宿主 `127.0.0.1`；**Hermes 主容器不改 bind、不重发布端口、不重启**
- **聊天门 = 独立容器**：同镜像 `nousresearch/hermes-agent:latest`，`HERMES_HOME=/opt/data/profiles/chat`，`hermes gateway run`，只开 api_server 绑 `127.0.0.1:8643`
- AstrBot 插件 → `POST http://127.0.0.1:8643/v1/chat/completions` → 同步拿回复文本
- 回复 `yield event.chain_result([...])` 交回管线 → **AstrBot 的分段与间隔节奏照常生效**（见 1.6）
- AstrBot 自己**不配任何模型 provider**（`provider_settings.enable=false`）→ 满足"不新增 API 配置、算力全从 Hermes 走"
- ⚠ **为什么不用 api_server 的 `/p/<profile>/` 多 profile 路由**：它由 `gateway.multiplex_profiles` 控制，而开机 preflight **明确排除 s6 容器**（我们正是）→ 必须改活着的 gateway 配置 + 重启才能用。**实测 404 已证**，因此放弃这条路，改用独立容器（零改动、零风险）。

要点：
- **官方接口通道（现号 bot / 微信 iLink）绝不下线**，作为兜底与干活门。
- 小号那条是新链路，**坏掉不影响现有任何东西**——这是本方案最大的安全垫。
- 通道层是"管道"：只传消息/文件，不做模型逻辑；所有 API 配置集中在 Hermes。

---

## 2.5 提示词分层（profile 拆分）与表情包

**主人的洞察成立**：主对话只做聊天之后，干活规则留在聊天层就是**纯粹的污染**（既占 prompt 预算，又把说话腔往报告模式拽）。

**做法：用 Hermes profile 拆，不拆框架**（已核实：`gateway.multiplex_profiles` 默认开，同一 gateway 进程可服务多 profile；api_server 有 `/p/<profile>/` 原生路由）→ **不多进程、不多容器、不改现有配置**。

| | profile: chat（新建） | profile: default（现状） |
|---|---|---|
| 人格文件 | 聊天专用 SOUL：人格、语气、**对话示例**、颜文字/表情包调用规则、**派活规则** | 现有全部规则（安全红线、严谨流程、技能路由） |
| 技能 | 最小集（派活、表情包、记忆） | 全部（含运维/OCR/研究等） |
| 记忆 | Hindsight 同一 bank，`chat` tag | 同一 bank，`work` tag |
| 收益 | prompt 从 **21k token** 掉到几 k → 便宜 + 干净 + 更不容易"报告腔" | 不受聊天改动影响 |

**派活规则留在聊天层（主人说得对）**：只写「什么时候派、怎么派、交付什么格式」，不写干活细则——细则进 default。派活通道可走 `POST 127.0.0.1:8642/v1/...`（default profile）或 Bot Mode 的 `message_agent`。

**表情包子系统（新增件）**
- 素材：本地目录 `stickers/` + `manifest.json`（语义标签 → 图片文件，如 `开心.jpg`）
- 规则：聊天 prompt 允许输出 `[[表情:开心]]`；颜文字直接写在正文里（已有规范）
- 渲染：插件把标记解析成 AstrBot 的 `Image` 组件，作为**独立气泡**发出（`event.send()` 或追加到结果链）
- 兜底：标签找不到素材就静默剥掉标记，不报错
- **需要主人**：给一批表情素材（或指定来源），我先留好接口和目录结构

---

## 3. 阶段计划

### P0 可行性验证（不动生产，可随时停）—— ✅ **已全部完成**
| 项 | 结果 |
|---|---|
| P0-1 AstrBot 能否被当纯通道 | ✅ 能（源码级确认，机制见第 1 节） |
| P0-1b 分段能否白借 | ✅ 能，但接管方式必须"只跳 LLM、不掐断事件"（见 1.6） |
| P0-2 协议端选型 | ✅ NapCat（官方 `NapCat-Docker/compose/astrbot.yml` 与 AstrBot 官方 compose 互相背书） |
| P0-3 外部程序驱动 Hermes | ✅ 实测：POST `127.0.0.1:8642/v1/chat/completions` → 回"桥已接通" |
| P0-4 聊天门独立容器 | ✅ 实测：临时容器 + 独立 profile → `/v1/models` 返回该 profile，对话回"聊天门通了"；已清理 |
| P0-5 插件逻辑离线自测 | ✅ 全部通过（引用/昵称/附件渲染、表情链、缺素材剥除、白名单、endpoint 拼接），`self_test.py` 可复跑 |
| 回滚 | 容器已删、测试 profile 已移入 `.trash`；现有服务与通道全程未受影响 |

**P0 通过才进 P1。不通过就回到"只在 Hermes 内拆角色"的轻方案。**

### P1 最小可用：小号私聊能聊（最大风险步）
- 部署协议端 + 通道层（**资源上限先算清**，见 P-3 决策）
- 建小号、扫码登录；**只开私聊给主人**，先不开群
- 打通：小号消息 → 通道层 → Hermes → 回复；含**多条分句发送**
- 验收：连聊 30 分钟，主观评"人味"；观察 3 天封号/免登录情况
- 回滚：停容器、退登录，现号与微信不受影响

### P2 群聊增强（可复用 qq-bridge 的设计）
- 引用解析（`[引用 某人：原文]`）、群成员身份注入、`[SILENT]` 潜水、发送白名单
- 自主收发改成工具驱动（`qq_get_unread_messages` 等），而非被动一问一答
- 验收：群里不误接别人的话；被引用必回；能主动潜水

### P3 记忆共享与隔离
- 两个角色共用同一 Hindsight bank；聊天写"日常"tag、干活写"事实"tag，召回按 tag 过滤
- 验收：互查得到、检索不被污染

### P4 多平台（按需，非必要）
- 只有真要用 Telegram/飞书/Discord 等时才做；否则 AstrBot 这层的价值不成立

---

## 4. 待主人拍板（开工前必须定）

1. **通道层选型**（已验后的更新）：
   A) AstrBot 纯通道 —— 源码已证**可行**，且**分段/间隔节奏可白借**（见第 1.6 节，接管方式要选对）。代价：多背一个容器 + 它的安全面（RCE 历史 + 插件全权限）
   B) 自写轻桥 —— SnowLuma/NapCat SDK 直连 + 自己转发，少一个框架和安全面、可控性最高；但分段/间隔节奏/平台差异**全得自己造**
   C) 先不换门面，只在 Hermes 内用 Bot Mode 拆角色（最省，但群内认人/分条仍受限）
   → **权衡点已从"分段能不能借"变成"愿不愿意为这套现成件多养一个容器"**
2. **协议端**：NapCat 还是 SnowLuma（两者都有风控未知）
3. **聊天层模型**：本地小模型（要腾显存）还是云端 API（有人味、不占卡）
4. **跑在哪**：本机容器 / 另一台（内存与显存上限）
5. **小号归属**：用哪个 QQ 号承担协议端风险

---

## 4.5 已交付物（都在 `/opt/data/chat-layer/`，映射后即 `chat-layer/`）

| 文件 | 作用 | 状态 |
|---|---|---|
| `PLAN.md` | 本文件 | — |
| `plugin/hermes_forward/` | AstrBot 纯通道插件（main.py / metadata.yaml / _conf_schema.json / README.md / self_test.py） | 逻辑自测通过 |
| `docker-compose.yml` | NapCat + AstrBot（host 网络、资源上限、数据卷） | 待宿主起 |
| `deploy.sh` / `rollback.sh` | 起容器 / 停容器（数据保留） | 待宿主执行 |
| `setup_chat_profile.sh` | 建聊天 profile（独立 SOUL/记忆，只开 api_server:8643） | 已实测同流程 |
| `run-chat-door.sh` | 起聊天门容器（同镜像、独立 HERMES_HOME、只监听 127.0.0.1:8643） | 已实测同流程 |
| `stickers/` | 表情包目录 + manifest.json（空，等素材） | 接口就绪 |

**实测数据（用于后续调优）**：default profile 一次调用 **21,108 prompt tokens**；新建 profile 空 SOUL 仍 **13,072 tokens** → 大头是工具 schema，**要压成本得给聊天 profile 收窄 toolset**（后续调优项）。

## 4.6 需要主人（只有这三件我替不了）

1. **小号 QQ**：给我一个 QQ 号用于登录（个人号协议端有封号风险，务必用小号）。
2. **扫码**：NapCat 起来后要扫码登录，二维码我取给你或你在 6099 面板扫。
3. **聊天人格 SOUL 审核**：聊天 profile 的 SOUL（人格 / 对话示例 / 表情包规则 / 派活规则）我起草，**你过一遍再上线**；表情包素材也等你给（或指定来源）。

## 4.6 已实现的状态（as-built，2026-09-20）

**容器**（宿主 `docker ps`）：`napcat`(mlikiowa/napcat-docker:latest) / `astrbot`(soulter/astrbot:latest) / `hermes-chat`(nousresearch/hermes-agent:latest) / 原有 `hermes` **未动**。全部 host 网络。

**线路**：主号 → 小号(<BOT_QQ>，NapCat 承载) → NapCat OneBot11 反向 WS 客户端 → `ws://127.0.0.1:6199/ws` → AstrBot aiocqhttp 适配器(Server) → 九段管线 → 插件 `hermes_forward`（`@filter.event_message_type(ALL)`）→ HTTP POST `http://127.0.0.1:8643/v1/chat/completions` → 聊天门容器（HERMES_HOME=/opt/data/profiles/chat）→ Hermes agent + Hindsight → 回复文本 → 插件 `yield event.chain_result(...)` → ResultDecorate 分段(150字/正则) → Respond 间隔(1.5–3.5s) → NapCat → QQ。

**关键配置点**：`provider_settings.enable=false`（关自带 LLM）+ **不 stop_event**（否则分段 stage 一起被跳过）+ `wake_prefix=[""]`（任何消息都算唤醒）+ `segmented_reply.only_llm_result=false`（转发结果才会分段）+ `friend_message_needs_wake_prefix=false`。

**端口**：6199 反向 WS / 6185 AstrBot 面板 / 6099 NapCat 面板 / 3000 NapCat HTTP(只绑 127.0.0.1，token，供自测) / 8643 聊天门 / 8642 现有 gateway（未动）。

**配置与备份**：`wire_up.py`（幂等接线，改前备份到 `backup/<ts>/`）、`setup_chat_profile.sh`、`run-chat-door.sh`；OneBot token 在 `chat-layer/.onebot_token`(600)。插件配置 `astrbot/data/config/hermes_forward_config.json`（base_url=8643、profile 空、owner_qq=<OWNER_QQ> 待主人确认、allow_private_owner_only=false 暂放宽、group_allowlist=[] 未接管任何群）。

**当前卡点**：无 —— **2026-09-20 17:09 整条链路已验证通过**（主人发「1」→ AstrBot 收到 → 插件转发 → 聊天门 agent 回复 → 分段成两条气泡 → NapCat 发出；17:10 那条带记忆的回答也正确）。
- ⚠ 未解决：日志里出现 HTTP 402「Insufficient Balance」（聊天门 4 次 / 主容器 22 次，最近一次 17:11），但用 `.env` 里的 key 直查账户余额是 **19.87 元可用** → 说明失败的不是这个 key，很可能是另一个 key/通道。定位需要读密钥文件，主人未授权，**已停手待定**。
- 观察：分段把「在呢主人，什么事喵？(´･_･`)」按问号切成两条，颜文字被孤立成一条 → 可调 `platform_settings.segmented_reply.regex` 让短尾巴跟上一句。

**遗留**：AstrBot 面板仍是初始口令（未换）；表情素材未给（`[[表情:x]]` 全部静默剥掉）；群聊认人/白名单未开；记忆 tag 隔离未接。

**持久化（重启/断电后要不要重扫）** — 2026-09-20 重整后的实际布局
- **容器运行时数据统一在 `stack/`**（宿主 hermes 文件夹内）：`stack/napcat/config`（账号与 OneBot 配置）/ `stack/napcat/ntqq`（QQ 登录态，417M）/ `stack/astrbot/data`（AstrBot 配置与插件）/ `stack/hindsight/{data,docker-compose.yml}`（记忆库 6.8G，从 docker 卷迁出）。**代码与文档留 `chat-layer/`**：compose、脚本、`plugin/hermes_forward`（挂进 AstrBot）、`stickers`、PLAN、`prompts/`；聊天门 profile 在 `profiles/chat`。
- 容器都是 `restart: unless-stopped` → 宿主重启后自动拉起。
- **免扫码：靠环境变量 `ACCOUNT=<BOT_QQ>`**（不是 compose `command`！镜像 `entrypoint.sh` 忽略命令行参数，早先写的 `command: ["-q",...]` 从未生效——2026-09-20 查源码定位）。**实测结论**：改对之后 `docker restart`（22:22）与 `docker compose up -d --force-recreate`（22:23）**都免扫码**，日志出现「正在快速登录 <BOT_QQ>」且不出现二维码。
- 扫码姿势：`/app/napcat/cache/qrcode.png` 只有 147×147，取出来要用 Pillow NEAREST 放大 6–10 倍再发；约两分钟失效。
- 改配置的临时备份**验证通过即删**（主人定：备份只在改到一半期间存在）。

**已知坑：NapCat 面板里的「反检测开关」不要开**（2026-09-20 查证）
- 维护者 MliKiowa 在 issue #1805 原话：「一般该功能不要修改，**暂不可用**」；发帖人复现：开了保存、重启后又变回未开。
- issue #1813：「打开反监测中的部分选项导致**启动时崩溃/无法正常登录**」。
- 翻遍近期 issue，**没有一条**能证明开了之后风控/掉线变少 → 收益无证据、风险明确 → 全部保持 false（现状）。`o3HookMode` 是默认值，不动。
- 掉线问题另有其因、另有解法：issue #1728（最新版几小时被踢、钉到 4.15.0 能稳好几天）、#1915 频繁掉线、#2027 被踢后无法重登、#2013 周期性崩溃。对策 = 观察频率 + 必要时钉 NapCat/QQ 版本；可用 #2049 新特性「被踢下线时执行自定义 Hook」做掉线通知。

## 5. 主要风险与对策

| 风险 | 对策 |
|---|---|
| 协议端封号 | 用小号；渐进放量（先私聊，再群）；保留官方 bot 兜底 |
| 协议端跟版本断供 | 钉版本；通道层与协议端解耦，坏了只停小号那条 |
| 资源不足（显存/内存） | P1 前先算配额；聊天模型优先走云端，或先停 extract |
| AstrBot 安全面（RCE/插件全权限） | 版本 ≥3.5.18；只装必要插件并审源码；面板只绑本机 |
| 范围膨胀 | P4 之前不接任何新平台 |

---

## 6. 待验证 / 未知（诚实清单）

**已解决**
- ✅ AstrBot 能否被当纯通道 —— **能**，源码级确认（机制见第 1 节）。代价：它自带的分段/t2i/回复装饰用不了，分段得自己在插件里实现。
- ✅ 是否有人做过同类 —— **有，且不止一条**（见第 1.5 节），但没有"拿 Hermes 当后端"的公开先例。

**仍未决**
- Bot Mode 的"群聊"是否指真实 IM 群（只查到 "bots message each other over the messaging platform"，未查透）
- NapCat / SnowLuma 在 2026-09 的封号与稳定性实况 —— **没有任何权威数据**，只能靠小号实测
- 小号走协议端后，图文/文件链路的实际体验
- Hermes 侧"按会话分级授权"怎么落（preset 的替代物）——**P1 前必须定**
- 转发插件要自己实现"分段发送"（AstrBot 装饰层被绕过）

---

## 7. 明确不做（本轮范围外）

- 不改 `/opt/hermes` 源码，不新增平台适配器
- 不动现号 QQ bot 与微信通道
- 不做微信主动推送（iLink 协议限制）
- 不为了"接平台"而接平台（无实际需求的多平台一律不做）

---

## 8. 聊天层成本优化实测（2026-09-20，全部只动 chat profile）

### 事实纠偏（重要）
- AstrBot 侧 LLM 全关 → **它不消耗任何 token**，不存在"两份提示词"
- 聊天门此前 **`memory.provider` 为空** → 根本没接记忆引擎；之前"记得昨天"的答案是 `session_search` 翻会话库翻出来的
- 330k token 那次的真凶：`tools.tool_search.enabled: auto`（源码注释：auto 等于 on）+ 14 个 toolset → 模型反复用 `tool_call` 批量取被隐藏的工具、被拒后重试

### 改动清单（chat profile）
| 项 | 改前 | 改后 | 为什么 |
|---|---|---|---|
| `platform_toolsets.api_server` | 14 个 toolset（browser/terminal/file/code_execution/skills/...） | `[memory, vision, delegation]` → 3 个工具 | 聊天只需要 说话+看图+派活；顺手堵掉"QQ 群消息触发终端"的安全口 |
| `tools.tool_search.enabled` | auto（=on） | off | 干掉 deferred bridge，消除 tool_call 重试循环 |
| `memory.provider` | 空 | `hindsight` | 接入共享库 `mianmian-history` |
| `hindsight/config.json` | 不存在（走默认 mode=cloud/bank=hermes） | local_external + bank mianmian-history + auto_recall/auto_retain + `recall_sync: true` + `memory_mode: context` + `recall_max_tokens: 1024` + indicator 关 | 每条消息都能召回（不靠上一轮预热）；只注入不暴露记忆工具，避免 `hindsight_reflect` 在本地实例 500 卡 40s |
| `recall_indicator`/`retain_indicator` | true | false | 聊天里不出现"👁️ recalled N"状态行 |

### 实测（同一组探针，聊天门 8643）
| 探针 | 改前 | 改后 |
|---|---|---|
| 简单问候 | 16,632 prompt / 1.4s | **8,546 / 2.4s** |
| 需要记忆的问题 | 329,577 prompt / 4,162 输出 / 30.6s | **8,677 / 496 / 3.1s** |
| 需要人物记忆的问题 | 76,042 / 2,136 / 124.6s | **8,481 / 483 / 3.6s** |

### 记忆双向已验证
`GET /v1/default/banks/mianmian-history/documents` 里能看到聊天门写回的记忆：
`retain_params.context = "棉棉与主人的 QQ 聊天（聊天门）"`、`metadata.source = hermes-chat`、`agent_identity = chat`。

### 遗留问题（要主人拍板或用 SOUL 解决）
- **派活会拖慢闲聊**：问"今天天气怎么样"时它没有 web 工具，于是派了个子代理，耗时 **79s / 33k prompt**。两条路：① chat SOUL 写明"闲聊不派活、没把握就说不知道"；② 把 `web` 加回聊天层（快，但多一份工具 schema）
- 记忆库现状：`observation` 只有 437 条（`world` 20,106 / `experience` 8,036），待整合 1,583 条、整合失败 193 条 → 召回多返回原始事实而非提炼结论
- 聊天层的 SOUL 目前仍是**干活 SOUL 的原样拷贝**（主人自己会重写）

---

## 9. 聊天层权限边界（主人 2026-09-20 拍板）

| 对象 | 谁能改 | 说明 |
|---|---|---|
| `SOUL.md`（人格/规则） | **只读** | 防线是：① 规则写死"不许改自己的 SOUL"；② **SOUL.md 以只读方式挂进聊天门容器**（物理防线，待实施）。⚠️ 不要再指望框架那道闸：`security.protected_instruction_files` 确实拦 SOUL/AGENTS/CLAUDE/.cursorrules，但它**故意不走 approvals**、是"一次一审批、无人工通道 fail-closed"，在我们这种无人应答通道上实测预期是**白等 `approvals.timeout`（300s）再拒**；且有绕路案例（issue #86878：审批超时后改用 `python3 -c`+shutil 把文件删掉） |
| `USER.md` | **聊天门自己可改** | 群友信息写这里（简短印象式）。上限 1,375 字符，满了自己淘汰。已知坑 #116788：无人值守下 `add` 不 gate → **"主人说别记"必须写进 SOUL**，框架不拦 |
| `MEMORY.md` | **聊天门自己可改** | 上限 2,200 字符（实测可写）。已知坑 #42405：`replace` 是纯子串匹配，匹配失败会重试到耗尽整轮 → 用户端静默无回复 |
| **工作区（文件权限）** | **给（主人 2026-09-20 定）** | 待实施：开 `file` 工具集（+约 1k token/轮）+ 设 `HERMES_WRITE_SAFE_ROOT`（两个根＝工作区 + `$HERMES_HOME`，否则它自己写状态/cron 会失败）。注意该闸**只管 `write_file`/`patch`**，不管 `terminal`（聊天门不开 terminal，够用） |
| 宿主其余数据 / 干活门记忆 | **碰不到** | 靠挂载 + `HERMES_WRITE_SAFE_ROOT` 双重限制 |

**干活门（default profile）**：主人 2026-09-20 定 **保持 full 功能**，不收窄；但"专为聊天加进去的东西"后续可以清理（暂缓）。

**待落地清单（尚未开工）**：
1. 插件补**发送者身份**（QQ 号/昵称/群号）——现在只发 `model/messages/stream`，库里聊天门写的记忆 `user_id`/`user_name` 全空 → **认人能力为零**。可顺带用官方会话头 `X-Hermes-Session-Id`（续接）/ `X-Hermes-Session-Key`（记忆 scope，可编码用户）
2. ✅ **已落地 2026-09-22**：聊天门开 `file` 工具集（`profiles/chat/config.yaml` 的 `platform_toolsets.api_server`）；`HERMES_WRITE_SAFE_ROOT=/opt/data` 是**容器级 env，两个门共享**（单容器双 profile 的必然结果，未按门单独收窄 → 聊天门可读写 `/opt/data` 全树，靠提示词约束）
3. ✅ **已解决（换方案）**：不挂只读 SOUL —— `security.protected_instruction_files=true` + `approvals.unattended_mode=deny`（`hermes -p chat config get` 实测）已能挡住指令文件写入，无需挂载手段
4. `a2a`（agent 间通话）：若要派活到干活门，需在干活门开 A2A 服务端（`gateway.platforms.a2a` + port）、聊天门加 `a2a_agents` 对端并启用 `a2a` 工具集（+约 561 token/轮）；`a2a_call` 是**同步等待**（默认 120s），长任务不适合

**写 SOUL 时对应的三条线**（别和边界打架）：
- 常驻认知 + 群友印象 → `USER.md`、`MEMORY.md`（**每轮付费**，越短越好；上限兜底，满了要自己淘汰）
- 细节 / 要翻的旧事 → Hindsight 召回（语义召回，想记什么不一定召得回）
- 要交付的文档 → 工作区文件；**落盘承诺只对能写的地方成立**（给文件权之后，聊天门可以说"我写到工作区"）

---

## 10. 挂起待办（2026-09-23 凌晨；主人说"明天闲的时候弄"）

**主人自己要做的**
- 两份人设终稿的微调：`/opt/data/SOUL.md`（干活门）、`/opt/data/profiles/chat/SOUL.md`（聊天门）；副本在共享文件夹 `人设文件备份/`

**等主人拍板的**
- 决策锚点职责怎么落进聊天门（四选项：只写 SOUL / SOUL 钩子 + skill `anchor-observation` / 灌进共享记忆库 / 聊天门 `USER.md` 摘要；棉棉建议后两个组合）
- 锚点里"周五/周六汇总"的触发方式（心跳 cron 已暂停，现在没人管）
- 40GB 旧镜像 + 残留容器（mineru 29.7GB、wechat-on-cloud ×3）要不要清

**欠着没做的（主人本轮点名只做 4、5）**
- 早晨简报 cron（07:30 一次性）
- 宿主侧复核脚本：容器重启后确认 DNS 兜底有没有被 docker 重写、四个 s6 服务是否自动回线、A2A 端口是否在听

**已落地、待重启生效**
- 双门 SOUL 已落盘 → 要 `s6-svc -r /run/service/gateway-default`、`gateway-chat` 各重启一次才生效（约 15 秒断线）
- hindsight provider `timeout` 60 → 180 同理；生效前写记忆会报"假失败"（记忆其实已落库，别重复写）

## 相关计划（另册）

* `PLAN-主动插话-群聊.md` —— 群聊主动插话（**不做**，主人 2026-10-03 明确：「群的保持原样就好，现在这个主动起头这个功能不往群搞」；文件留作决定记录 + 复用清单）。私聊那半已上线：`plugins/onebot/proactive.py` + `ops-changelog/变更-20261003-主动私聊斜坡.md`。
