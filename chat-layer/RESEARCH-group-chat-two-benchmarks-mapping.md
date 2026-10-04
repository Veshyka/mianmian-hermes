# 群聊拟人机制 → 新架构复用调研（双基准：**麦麦 MaiBot** + **qq-bridge／DSH**）

> **性质：只读调研。** 全程未改任何配置/代码/服务，未启用群聊，未重启，未向主人或任何群发消息。
> 时间：2026-09-23（UTC+8）。基准仓库为当日浅克隆/下载的快照，均记录 commit。
> 上位文档：`/opt/data/chat-layer/RESEARCH-qq-bridge-group-mechanics.md`（基准②的详版，本文件不重复其行号细节，只引用）。

---

## 0. 一句话结论

**两个基准里真正能搬的机制，99% 都是"适配器层的门控 + 库存"，不是"人格层的话术"** —— 麦麦（MaiBot）把「要不要说话」做成了**LLM 之前的纯规则评分**（`reply_necessity.py`，不触发就不花钱），把「偷表情包」做成**收到消息时顺手入库**（sha256 去重 + VLM 打标签，上限 64 张）；qq-bridge 把「潜水」做成**引擎不唤醒**（不 @ 也全量收，但按概率/关键词才唤醒 agent）、把「偷表情包」做成**QQ 收藏库同步**。
搬到现在这套（NapCat + `hermes_onebot` + chat profile），**成本红线**是：群消息**必须不进 LLM**，只有命中门控的才进。其余都能小改落地。

---

## 1. 基准认定

### 1.1 主人原话（逐字，去密钥；来自本地会话记录）

- 2026-09-19 05:27（发给我的链接）：
  > 「你验一下吧，也可以看看有没有别人这么想过，为什么我想到这个问题呢，GitHub项目地址：**https://github.com/Derpyu520/qq-bridge** 我之前给你发过这个，但是当时没想到过可以直接用 **hms 代替 dsh 的位置**，就是不知道 hms 有没有这么开放，然后 astr 是评论区的建议，说这个更成熟，反正就是一种杂糅的思想吧」
- 05:33（追问）：「还有就是**这里的原项目**你也可以看看有没有参考价值」
- 05:34（我当时的结论）：「`dsh` 的谜底出来了——**DeepSeek Harness**。qq-bridge 的描述是：> Bridge between QQ (**SnowLuma OneBot v11**) and **DeepSeek Harness agents**: social simulation, safe MCP tools, slang learning and more.」
- 09-20 07:21（主人评价，请以此对齐）：**「我就是说那种设计比较先进嘛，毕竟从效果上看感觉仿真效果比 astrbot 更好」**

→ **`dsh` = DeepSeek Harness = DeepSeek 官方开源 agent harness**（`deepseek-ai/deepseek-harness`，TypeScript，**23.4 万 star**，描述「DeepSeek Harness: Everything is a Plugin.」，最近推送 2026-09-22）。它不是语音转写走样，是官方缩写，跟 `hms`=Hermes 一个用法。

### 1.2 候选比对（GitHub API 实搜，「基于 DSH 的群聊项目」全部候选）

| 仓库 | ★ | 语言 | 最近推送 | 特征匹配度 | 判定 |
|---|---|---|---|---|---|
| **Derpyu520/qq-bridge** | 393 | JS | 09-18 | **QQ(OneBot v11) + DSH；social simulation（潜水/插话）、slang learning（黑话）、safe MCP tools、表情包收藏库** | ✅ **主人发的就是它**（链接逐字命中 2 次） |
| xmanrui/dsh-im | 1456 | JS | 09-23 | 多平台 IM↔DSH 连接器；**群聊只做「被 @ 才答」**，无潜水概率、无表情包偷取 | ⚠️ 同类但机制不同，不是"像真人"那个 |
| wenbin-wb/dsh-bridge | 174 | JS | 09-16 | 远程访问/隧道 + 多通道对话，无社交仿真 | ❌ |
| tencent-connect/dsh-qqbot | 107 | TS | 09-08 | 腾讯官方 Bot API（不是个人号） | ❌ |
| sliverp/DeepSeek-harness-qqbot、wang-22-code/dsh-qqbot-bridge、jixishi/dsh-adapter-qq | 2–6 | TS/JS | 08–09 | 均为官方 Bot API 桥，无潜水/表情包 | ❌ |
| Mai-with-u/MaiBot（**基准①**） | 6036 | Python | 09-23 | 群聊拟人、潜水、偷表情包、群记忆 | ✅ 独立一票（麦麦） |

**结论**：基准①= `Mai-with-u/MaiBot`（官方组织 `Mai-with-u`，勿用 fork）；基准②= `Derpyu520/qq-bridge`（"基于 DSH"的那个）。两者都已被我深读。

---

## 2. 基准①：麦麦 MaiBot 的机制清单（快照 `7ca4f06`，2026-09-23）

**架构**：Python，738 个 `.py`，Docker 部署（`Dockerfile`/`docker-compose.yml`），自带 WebUI dashboard，插件体系 + 适配器（NapCat / SnowLuma 等）分离；核心是 **Planner → Replyer 两段式**（`planner_mode` / `replyer_mode` = text\|multimodal\|auto，`src/config/official_configs.py:348,365`），带**规划器可打断**控制器（`src/maisaka/runtime.py:94` `PlannerInterruptController`，上限 `official_configs.py:616`）。

### 2.1 偷表情包（全链路，带行号）

| 环节 | 实现 | 位置 |
|---|---|---|
| 采集入口 | 收到的消息里若有表情组件 → 调 `emoji_manager.get_emoji_description(emoji_bytes=component.binary_data, is_emoji=True)`，**用消息里的二进制，不去下载 URL** | `src/chat/message_receive/message.py:214-217` → `process_emoji_component` `:322-360`（调用点 `:345-349`） |
| 入库 | `ensure_emoji_saved` 落盘到表情目录 | `src/emoji_system/emoji_manager.py:226`、`:373` |
| 去重 | **sha256** 哈希 + DB `(image_hash, image_type=EMOJI)` 查重（已存在直接复用，不重复入库） | `emoji_manager.py:325`、`:571-575`、`get_emoji_by_hash` `:756` |
| 打标签 | VLM 生成描述 `build_emoji_description`（视觉模型调用，**每张新图 1 次**） | `emoji_manager.py:300`、`:981` |
| 上下文呈现 | 表情在对话文本里变成 `[表情包: <描述>]`，**随后二进制即丢弃**（不占上下文） | `message.py:353-357` |
| 上限 | 最多 **64** 张可用表情（`max_reg_num`），满了按 `do_replace=True` 替换 | `official_configs.py:4219`（注释 `:4230`）、`:4232` |
| 维护 | 周期任务（`check_interval` 默认 600s）扫描新增/替换/淘汰 | `emoji_manager.py:1150-1153`、`official_configs.py:4246` |
| 体积护栏 | 单文件 `max_emoji_size_mb`（默认 5MB，0=不限） | `official_configs.py:4272-4285`、`emoji_manager.py:245-258` |
| 开关 | `steal_emoji`（默认 **True**）、`content_filtration`（默认 **False**） | `official_configs.py:4259`、`:4287` |
| 淘汰/审查 | LLM 替换 `replace_an_emoji_by_llm`、注册审查 `review_emoji_for_registration`、`ban_emoji`、缓存清理 | `emoji_manager.py:870`、`:949`、`:798`、`src/emoji_system/emoji_cache_cleanup.py` |
| 失败处理 | VLM 描述失败 → 文本退化为 `[表情包]`，**不崩不丢消息** | `message.py:349-356` |

**自己发出来时怎么选**（关键差异点）：
- 由 **LLM 工具** `send_emoji` 决定发（`src/maisaka/builtin_tool/send_emoji.py`，`src/emoji_system/maisaka_tool.py` 的 `send_emoji_for_maisaka`），**要求 planner 是视觉模型**（`send_emoji.py:301-310`）。
- 具体挑哪张：`get_emoji_for_emotion` (`emoji_manager.py:843-869`) —— 用 **Levenshtein 相似度**在该情绪的候选里排序，取 **top10 里随机一个**（避免每次同一张）；相似度表 `_calculate_emotion_similarity_list` (`:1276-1310`)。
- **隐私**：只存"入库的表情图片本体 + VLM 描述"，不存发送者身份；`content_filtration` 开关可做内容过滤；`ban_emoji` 可拉黑单张。**没有**记录"这表情是谁发的"。

### 2.2 潜水 / 插话（要不要说话）

| 机制 | 实现 | 位置 |
|---|---|---|
| 核心：**回复必要性评分（纯规则，无 LLM）** | `score_reply_necessity`：被 @ / 点名 → 顶格直通；话题相关度累积加分；**闲置压力补偿** `idle_pressure_bonus`；**最近自己说话太多则惩罚** `recent_self_ratio`；最后乘一个「生效回复频率」 | `src/maisaka/reply_necessity.py`（全文 277 行；评分主体 `:1-140`、细则 `:140-210`） |
| 门控闸 | `turn_gates.py`（196 行）——进 Planner 前的闸门 | `src/maisaka/turn_gates.py` |
| 静默退避 | `idle_backoff.py`（95 行）——长时间没人理 → 指数退避，别硬凑话 | `src/maisaka/idle_backoff.py` |
| 注意力漂移 / 模式策略 | `attention_drift.py`、`mode_policy.py`（含"只规划不回复"的模式） | `src/maisaka/` |
| 强制触发 | 命中触发原因 → 日志「下一轮 Planner 将强制触发」 | `src/maisaka/runtime.py:1320`、`:1340` |
| 频率旋钮 | `talk_value`（群聊主动说话频率）、`private_talk_value`（私聊）、`reply_timing`、`focus_mode`/`focus_cool_time`（专注模式=全回，冷却） | `official_configs.py:537`、`:724`、`:897`、`:1065-1117` |

**要点（可直接照搬的思想）**：**"要不要回"是 LLM 之前的确定性函数**，@/点名是 100% 直通，其余靠"话题压力 + 闲置压力 − 自己话多"评分 + 冷却退避。因此**潜水的默认状态是不花钱的**。

### 2.3 群聊记忆与私聊隔离

- 中/短期记忆 + 人物画像：`src/maisaka/memory/{mid_term.py, person_profile.py, heuristic_injector.py}`；长期记忆另有 A_memorix（`docs/a_memorix_sync.md`）。
- **隔离开关**：`cross_chat_enabled` —— `chat_id = "" if cross_chat_enabled else session.session_id`（`src/maisaka/memory/heuristic_injector.py:197`）→ **默认按会话（群/私聊各自一份）隔离，跨会话记忆是显式 opt-in**。
- 结论：麦麦**默认不会让群记忆污染私聊**，跟我们最担心的"Hindsight 同一个 bank"形成对照。

---

## 3. 基准②：qq-bridge（基于 DSH）机制清单

详版见 `RESEARCH-qq-bridge-group-mechanics.md`（含全部 bridge.js 行号）。要点：

| 机制 | 实现（`/tmp/qq-bridge`，commit `dea3ce8`） |
|---|---|
| 群消息全量收 | `bridge.js:8259+` `onGroupMessage` **不要求 @**，全量入站 → 潜水是「引擎不唤醒」而不是「适配器丢包」 |
| 一代仿真状态机 | `watching / active / probing / leaving`（观望/活跃/试探/退场），`bridge.js:277-300` 默认参数、`:5893-5920` 判定、`:6240-6290` 触发相位 |
| 二代唤醒 | `bridge.js:334,350-363`：`batchWindowMs 8000` / `recommendedProbability 0.05` / `recommendedKeywords` / `maxWakePerMinute 1` / `maxWakePerHour 12` / `noActionLimit 3`；触发项 `atMention/nameMention/keyword/question/poke/probability/speakerIds/anyMessage`（`src/mcp-snowluma-safe.js`） |
| 发言节奏/防刷屏 | `bridge.js:365-374`：单次 burst ≤8 条、间隔 1–3s 抖动、`maxSendPerMinute 8`、`maxSendPerHour 60`、单条 `maxMessageChars 500`；静默判定 `:379-386`（`quietMs 8000`、`minQuietAfterNewMs 10000`） |
| 不说标记 | `[SILENT]`（`bridge.js:124-125`） |
| 分条/错落 | `docs/PROJECT_GUIDE.md:191-199` |
| **偷表情包** | **收藏库同步**：`syncStickerLibrary`（`:729-758` / `:8285+`）→ `fetch_custom_face_detail` 拉 QQ **自定义表情收藏** → 转 base64 → `upload_custom_face` 回存拿新 file_id/url（完成判定 `fetched.length < count`）；发送用 `sendStickerV2` / `emoji(file_id)` 段；选图提示词 `sticker-lib.js:186-200` |
| 采集限速 | `bridge.js:392-403`：`collect{maxPerMinute 2, maxPerHour 10, maxRemarkChars 20}`、`syncTtlMs 60000`、`maxListCount 100`、`promptMaxStickers 8` |
| 安全硬边界 | `RULES.md:35-45`（agent 无本地工具、发送白名单、回复审计） |

**主人为什么觉得它"比 astrbot 效果好"**（对齐他的评价）：它把「人味」做在**触发与节奏**上（概率唤醒、静默窗口、burst 抖动、黑话学习），而不是靠提示词求模型说人话；且它**不花 LLM 钱在"要不要说话"上**。

---

## 4. 新架构实测事实（今天核过的，不是推测）

| 项 | 事实 | 证据 |
|---|---|---|
| 部署 | `plugins.enabled: [onebot]`、`platforms.onebot.enabled: true`、`read_only: false`；连 NapCat 反向 WS | `profiles/chat/config.yaml:10-12,28-38`；`logs/gateway.log` 08:04:12 `read_only=False scope=private` |
| 群开关 | **config.yaml:38 `group_enabled` 已是 true**，而 `RUNBOOK.md:8` 写着 false → **文档/配置漂移**；且实测**无群消息入站**（state `dropped_count` 平稳跳增） | 上条 + `profiles/chat/onebot-state.json` 采样 |
| 群准入 | 适配器**没有群号白名单**，`_authorized_sender` 对群只认 `group_ids` 是否允许、不校验发送者 | `plugin/hermes_onebot/adapter.py:82,85,86,87,96,511-525,560-567` |
| 群会话键 | `onebot:group:<群号>:<QQ号>` —— **群内每个人一份独立上下文** | `adapter.py:527-533`；`FEASIBILITY.md:70-81` |
| 认人 | **已免费**：Hermes 把发送者昵称注入到 `User:` 行 | `/opt/hermes/gateway/session.py:389-420`（实测昵称出现在注入行） |
| 入站媒体 | 图片 → 文本 `[图片]`；**face/mface 表情段直接丢弃**；reply 段丢弃；**纯图消息整条被 ignore** | `onebot_proto.py:41-42,50-51,55-56,99-108` |
| 出站 | `build_action` **只生成 text 段** → 发不了图/表情 | `onebot_proto.py:112-129`（`:119`） |
| @识别 | at 段渲染成 `[@<BOT_QQ>]` → 适配器层可判 @/点名 | `onebot_proto.py:52-54` |
| 防抖 | 默认仅私聊合并；群要 `scope=both` 才按人分桶；**指令类消息立即放行**（可复用为"@/点名立即放行"通道） | `debounce.py:29-47,50-60` |
| 记忆 | `auto_recall` + `auto_retain` 均开，bank 固定 **`mianmian-history`**（与主人私聊同库） | `profiles/chat/hindsight/config.json`（全文 19 行） |
| NapCat | **4.18.28 / protocol v11**；`get_group_list` 返回 **3 个群**（1095283483 / <GROUP_ID> / <GROUP_ID4>）；`get_group_member_list`、`get_group_msg_history` 可用；`get_image`/`get_file` 存在（伪造 file 返回 `retcode 200 file not found`）；**`enableLocalFile2Url=false`**；只挂 config+ntqq 两卷（其 cache 不在卷里）；`/opt/data` **82G 可用** | OneBot HTTP `127.0.0.1:3000` 实测；`stack/napcat/config/onebot11_<BOT_QQ>.json:35`；`chat-layer/docker-compose.yml:32-34`；`df -h` |
| 单回合成本 | 一次闲聊调用实测 **prompt_tokens = 21108**（09-19 `/v1` 路径，人格+记忆+技能全量进上下文） | 本地会话记录原文 |

---

## 5. 逐条映射表（判定 + 工作量 + 风险）

工作量＝**人时（含单测，不含主人拍板等待）**；「直接复用」指现成能用不用改。

| # | 机制（来自哪个基准） | 落到新架构哪里 | 判定 | 人时 | 风险 |
|---|---|---|---|---|---|
| 1 | **群消息全量入站**（两基准都这么做） | 适配器：修 `group_enabled` 漂移 + 确认 NapCat 推送群消息 | **小改** | 1–2 | 低 |
| 2 | **群号白名单**（麦麦/qq-bridge 都有 allowlist；qq-bridge `RULES.md:41`） | 适配器 `_authorized_sender` 加 `group_ids` 校验（现在**任何群**都放行） | **小改（安全必需）** | 1–1.5 | 不做则小号进的任何群都会被应答 |
| 3 | **不 @ 不说话** | 适配器在 `_dispatch` 前判 `[@self]`/群名片点名 | **小改** | 3–4 | 需先有 #1 |
| 4 | **概率插话 + 冷却 + 防刷屏**（麦麦 `reply_necessity` 评分；qq-bridge `recommendedProbability 0.05` + `maxWakePerMinute/Hour`） | 适配器内**纯规则门控**（不进 LLM）：话题压力+闲置压力−自说率，@=直通，冷却/配额 | **重写**（无现成实现；但算法可照麦麦/qq-bridge 抄） | 8–12 | 参数调不好会变话痨或死鱼；必须落配置可回退 |
| 5 | **群会话隔离**（麦麦默认按会话；qq-bridge 每群一份 state） | **已天然有**：`onebot:group:<gid>:<uid>` | **直接复用** | 0 | 注意：现在是"每成员一份"，与麦麦"整群一份"不同（见 §7） |
| 6 | **群内认人/群名片** | Hermes 已注入昵称 | **直接复用** | 0–1 | 无 |
| 7 | **引用回复（能引用某条消息答）** | 出站 `build_action` 加 `reply` 段（OneBot `[CQ:reply,id=]`） | **小改** | 2–3 | NapCat 需有效 message_id（要缓存入站 id） |
| 8 | **被引用的消息进上下文** | 入站 `:55-56` 现在**丢弃 reply 段** → 改成 `get_msg` 取原文拼进文本 | **小改** | 2–3 | 依赖 `get_msg` 可用性（已探到 API 在） |
| 9 | **偷表情包：采集** | 入站 media 段 → `get_image`/`get_file` 拿字节 → 落 `/opt/data/chat-layer/stickers/`（**不落 NapCat cache**）→ sha256 去重 + 入库 | **重写**（当前连原料都收不到：#入站媒体） | 6–10 | URL/rkey 过期（对策=**取字节不存 URL**，同麦麦）；磁盘（82G 可用，需配额+LRU） |
| 10 | **偷表情包：理解/打标签** | VLM 描述入库（麦麦用视觉模型）；或先只存不打标 | **重写（可分期）** | 3–5 | 每张新图 1 次视觉调用＝**有成本**；可先用本地小模型省 |
| 11 | **偷表情包：发出去** | 出站加 `image`（`base64://` 或 `file://`）段；选图策略照麦麦：同情绪候选 **top-N 随机** | **小改→重写** | 4–6 | `enableLocalFile2Url=false` → 用 **base64://** 最稳（需实测 NapCat 接受度） |
| 12 | **表情库同步 QQ 收藏**（qq-bridge 独有） | NapCat 有 `fetch_custom_face_detail`/`upload_custom_face` 族 API 才行 | **不做**（除非实测确认） | — | 无 API 则整条路断，不如自建本地库存 |
| 13 | **分条发送/错落节奏** | 已有 `⁂` 分段 + 1.5–3.5s 抖动（`segmentation`） | **直接复用** | 0 | 无 |
| 14 | **安静窗口/静默判定**（qq-bridge `quietMs 8000`） | 群防抖不要用 10s 合并；@走立即通道 | **小改** | 1–2 | 群里消息密，攒 10s 会"迟到得莫名其妙" |
| 15 | **群聊记忆写入** | 关 `auto_retain` 或换 bank/加 tag（**profile 级配置，需实测能否条件化**） | **小改（但未验证）** | 2–4 | **不做会让群聊污染主人私聊记忆**（同 bank `mianmian-history`） |
| 16 | **黑话学习**（qq-bridge `slang-learner`；麦麦 `src/learners/`） | 无对应物 | **不做（二期可选）** | 3–5 | 收益慢、要洗数据 |
| 17 | **群里不接活/不查身份/不落文件** | **只靠提示词**（SOUL.md 群聊段 + 边界段） | **小改（提示词）** | 1 | 提示词不是硬约束；真正的硬边界得靠工具面（Hermes 侧已窄） |
| 18 | **群聊语气/不发散/别列清单** | **只靠提示词**（人格） | **小改（提示词）** | 1–2 | 模型腔调是主因，提示词只能压一部分 |

**合计**：
- 「只看不说」最小集（#1,2,6,9,10,15,17）≈ **16–25 人时**，**0 次群消息 LLM 调用**。
- 全套（#1–#11,13–18）≈ **35–55 人时**。

---

## 6. 哪些是 Hermes「天生没有、必须自写」，哪些提示词能解决

**必须自写（Hermes/适配器里根本没有这个原语）**
1. **「要不要说话」的门控**——Hermes 只处理"收到的消息"，没有"该不该应答"的判定层（现只有私聊白名单）。麦麦的 `reply_necessity` / qq-bridge 的 wake 概率，本质是**适配器内的前置闸**。
2. **群消息的媒体落地**（图片/表情字节 → 落盘 + 去重记录）。
3. **出站非文本段**（image/reply），当前 `build_action` 只发 text。
4. **引用消息的还原**（入站 reply 段现在被丢）。
5. **群号白名单**（安全必需）。
6. **群聊记忆的分流**（同 bank 现状）。

**只能靠提示词解决（没有机械层）**
- 群聊语气、不发散、别列清单、@ 谁、什么时候点到为止；
- 「群里不接活、不查身份、不透露主人信息」——现在 `SOUL.md` 已有群聊段（247–253）、群聊解禁（377–380）、派活边界（214），加 1 条群内硬规矩即可。

**已经在 Hermes 里的（不用做）**：群会话隔离、群内昵称、分条节奏抖动、群消息防抖框架、白名单机制骨架。

---

## 7. 技术风险与未知（附验证方法，均未验证完，标注清楚）

| # | 风险/未知 | 现状 | 验证方法（只读） |
|---|---|---|---|
| R1 | **群消息到底推没推给反向 WS** | config 有 `group_enabled`，但无群消息入站 | 打开群消息（需主人许可）后看 `onebot-state.json` 的 `rx_count`/`dropped_count` 跳变；或临时把适配器日志调 DEBUG 观察后回滚 |
| R2 | **`group_enabled` 漂移** | `config.yaml:38`=true vs `RUNBOOK.md:8`=false | `hermes -p chat config get platforms.onebot.group_enabled`；并把文档与实值对齐 |
| R3 | **图片能否拿到字节** | 图片段被转成 `[图片]` 文本，URL 都没留 | 用一条真实图片消息测 `get_image` 返回（`file`/`url` 字段）与 URL 时效（**rkey 会过期**）；对策：拿到就立刻落盘，永不存 URL |
| R4 | **出站发图 NapCat 是否吃 `base64://`** | `enableLocalFile2Url=false`；未实测 | 需要一次真发（要主人许可）。首选 `base64://`，备选 `file://`+开 `enableLocalFile2Url` |
| R5 | **偷表情包会不会撑爆磁盘** | `/opt/data` 82G 可用；NapCat 自带 cache **不在卷里**（重建即丢），不能当库存 | 建立配额：≤500 张 / ≤200MB / LRU 淘汰 + sha256 去重；目录放 `/opt/data/chat-layer/stickers/`（在持久卷） |
| R6 | **群里消息密 vs 10s 防抖** | 群默认不合并；合并只在 `scope=both` | 群聊**不要**开合并；@/点名走 `is_command` 立即放行；其余消息在适配器内**直接落采集、不进 Hermes**（连"合并"都不需要） |
| R7 | **群聊记忆污染主人私聊**（同 bank `mianmian-history`） | `auto_retain=true`，bank 固定 | 查 Hindsight 能否按会话/平台条件化 retain（`profiles/chat/hindsight/config.json` 是 profile 级）；不行则群消息**只走采集、不入记忆**，或另起 `chat-group` bank（需 Hindsight 侧支持多 bank） |
| R8 | **群内有人让她查身份/干不良请求** | `SOUL.md` 现有人格硬规矩（派活边界 214、群聊段 247–253、解禁 377–380） | 提示词加一条"群内一律不认领任务、不查人、不发文件"；**真硬边界靠工具面**（Hermes 侧工具面已窄，比 qq-bridge 的 `RULES.md:35-45` 更好守） |
| R9 | **每群消息会不会触发 LLM** | 取决于门控写在哪 | 设计铁律：**门控必须在适配器、进 Hermes 之前**；命中才进 → 实测单回合 `prompt_tokens ≈ 21k`，随机插话若按 jitter 每小时 12 次计，成本可算、可控 |
| R10 | **群会话键 = 每人一份**（`onebot:group:<gid>:<uid>`） | 与麦麦"整群一份"不同 | 取舍：每人一份→上下文干净但"看不到别人说的话对同一话题的进展"；整群一份→连贯但吵。**建议先保持现状**，观察后再定 |

---

## 8. 三个可选方案

| | **A 完全不开群聊** | **B 只看不说**（潜水采集） | **C 全套**（@才答 + 概率插话 + 偷表情包） |
|---|---|---|---|
| 改动面 | 0（维持 `group_enabled` 现状；只把漂移的文档对齐） | 修 #1/#2 + 入站媒体落盘(#9) + 去重/配额 + 记忆分流(#15)（**跳过** #3/4/11 出站） | B 全部 + 不@不说(#3) + 概率插话(#4) + 引用(#7/#8) + 发图(#11) + 提示词(#17/#18) |
| 风险 | 无 | 低（不发言，最坏是占点磁盘；配额兜底） | 中高：话痨/翻车/记忆污染/被人当真人来派活 |
| 回退 | 不用动 | 关 `group_enabled` 一个开关，或删采集目录 | 同上；门控参数落配置，可一键降级到 B |
| 成本 | 0 | **0 次 LLM 调用**（群消息永不过 LLM） | **每命中一次门控 = 1 个 Hermes 回合**（实测单回合 21k prompt tokens 量级）；未命中＝0 |
| 人时 | ~0.5 | 16–25 | 35–55 |

**关于"每条群消息会不会花钱"的明确结论**：
- **C 方案只要把门控写在适配器里，绝大多数群消息是 0 成本的**（qq-bridge 就是这么活的：`maxWakePerMinute 1`、`recommendedProbability 0.05`）。
- **反过来，如果门控写错地方（先过 LLM 再决定说不说），每条群消息都要烧一次 21k-token 级别的 prompt** —— 这是本方案唯一的"出血点"，必须写进实现红线。
- B 方案**结构上不可能花钱**，因为它不发言、不过 agent。

---

## 9. 推荐与第一步

**推荐：先 B，验证通后直升 C 的"@/点名才进 LLM"子集——理由一句话：群消息不过 LLM 是成本红线，而 B 能在零成本、零发言风险下把最脏的三件事（群准入白名单、图片字节落地、记忆分流）一次性验通。**

**开工第一步（最小动作，约 1 人时、完全可回退）**：**先解决 `group_enabled` 的文档/配置漂移 + 加群号白名单**（`#2`）——因为现在适配器对**任何**群都放行，白名单是群聊一切机制的前置安全闸；做完立刻能确定"群消息到底进不进得来"（R1），拿到这个事实再排 B/C。

---

## 10. 附：本次调研的操作与证据清单

**只读操作披露**：全程未修改任何生产文件/配置、未重启服务、未发送任何 QQ 消息。对 NapCat 的 OneBot HTTP API 只调用了**只读**接口：`get_status`、`get_login_info`、`get_version_info`、`get_group_list`、`get_group_info`、`get_group_member_list`、`get_group_msg_history`，以及两次**伪造 file id** 的 `get_image`/`get_file`（仅验证失败路径）。两次基准仓库克隆：`/tmp/qq-bridge`（`dea3ce8`）、`/tmp/MaiBot`（`7ca4f06`，tarball + GitHub API 取 SHA），均为临时目录，非生产路径。

**搜过的路径与关键词**（用于确认基准，避免编造）：
- 路径：`/opt/data/sessions/`、`/opt/data/profiles/*/sessions/`、`/opt/data/profiles/chat/logs/`、`/opt/data/logs/agent.log*`、`/opt/data/chat-layer/**`、`/opt/data/skills/**`、`/tmp/qq-bridge/**`、`/tmp/MaiBot/**`
- 命中基准的关键词：`github.com/Derpyu520/qq-bridge`（逐字命中 2 次 + 主人引文）、`DeepSeek Harness`、`基于dsh`、`hms代替dsh`、`麦麦`、`MaiBot`
- GitHub 搜索：`"deepseek harness"`、`deepseek harness chatbot`、`dsh deepseek`、`harness qq bot`、`MaiBot in:name`（结果即 §1.2 表）
- 结论：**两个基准均确认，无需猜测**；`dsh` 不是谐音。

**交付物**：
- 本文件：`/opt/data/chat-layer/RESEARCH-group-chat-two-benchmarks-mapping.md`
- 基准②详版（行号级）：`/opt/data/chat-layer/RESEARCH-qq-bridge-group-mechanics.md`
