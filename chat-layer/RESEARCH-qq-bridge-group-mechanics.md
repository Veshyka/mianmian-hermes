# 调研：qq-bridge（群里像真人一样说话 / 潜水 / 偷表情包）机制 → 新架构映射

> **性质：只读调研，未改任何配置/代码/服务，未启用群聊，未重启，未给主人发消息。**
> 写于 2026-09-23 08:xx（UTC）。所有行号都是当天实读源码记得的。
> 基准通道：`napcat`(NapCat 4.18.28) → 反向 WS `ws://127.0.0.1:6700/ws` → 聊天门 profile `chat` 的 `hermes_onebot` 适配器。

---

## 0. 一句话结论

**主人指的那个项目 = `Derpyu520/qq-bridge`（QQ ↔ DeepSeek Harness 桥接，「AI 仿真群友」）。**
它的「潜水」与「偷表情包」都是**桥接层（进程内）的硬机制**，不依赖模型自觉；搬到新架构上，
**「潜水/频率/退避/采集」要在 `hermes_onebot` 适配器里重写（约 15~25 人时）**，
**「表情包/引用/认人/分段」可以小改复用（约 10~15 人时）**，
**「[SILENT]、DSH 会话映射、空格分句、控制台」不做**（新架构已有等价物或不需要）。
成本红线只有一条：**任何进 agent 的群消息都是付费回合——闸门必须在适配器层判，不能在提示词里判。**

---

## 1. 先把「那个 GitHub 项目」钉死（候选 + 匹配度）

| 候选 | 出处/证据 | 特征匹配度 | 判断 |
|---|---|---|---|
| **Derpyu520/qq-bridge** | 主人会话原文（`/opt/data/profiles/chat/sessions/request_dump_api-…json`）：**「GitHub项目地址：https://github.com/Derpyu520/qq-bridge 我之前给你发过这个」**（9/19 05:27 / 9/20 07:18），并说**「从效果上看感觉仿真效果比 astrbot 更好」**；本仓 `chat-layer/PLAN.md:177`「P2 群聊增强（可复用 **qq-bridge** 的设计）」、「引用解析、`[SILENT]` 潜水、发送白名单」 | ★★★★★ 潜水（`reserved`/`reserved2` 两代）+ 偷表情包（`qq_collect_sticker`）+ 像真人（整套 `qq-chat-v2` 人格与节奏规矩）+ 群聊记忆（三类轻量记忆，按会话隔离） | **就是它** |
| Mai-with-u/MaiBot | 会话里出现 1 次；我们的技能 references（`proactive-messaging-cron/references/human-like-chat-mechanics.md`）是**它的二手消化稿** | ★★ 有频率/退避/表情包机制，但**没有**[SILENT] 之外的「潜水」语义，也没「偷表情包」这套 | 旁证（机制参考），不是主人指的项目 |
| astrbot_plugin_group_chat_plus | 今天查过（115★） | ★★ 群聊重型套件，与主动回复/分段冲突 | 已排除 |

**复现方式**（本次已克隆到 `/tmp/qq-bridge`，commit `dea3ce8`，浅克隆约 1.3 MB）：
```bash
git clone --depth 1 https://github.com/Derpyu520/qq-bridge /tmp/qq-bridge
```
下文所有 `文件:行号` 均指这个 commit。

---

## 2. 素材盘点：qq-bridge 真正实现了什么（逐条 + 出处）

### 2.1 模式与「潜水」的两代实现

| # | 机制 | 事实（文件:行号） |
|---|---|---|
| M1 | **四运行模式**：`chat` / `closed-agent` / `reserved`（一代仿真）/ `reserved2`（二代仿真，装完默认） | `RULES.md:11-16`；`docs/PROJECT_GUIDE.md:129` |
| M2 | **一代仿真 = 状态机**：`观望(idle) → 活跃(active) → 试探(probing) → 退场(exiting) → 观望`，每 5 秒 tick 一次 | 状态图与说明 `docs/PROJECT_GUIDE.md:174-190`；`socialLoopTick` `bridge.js:6200`（tick 注册 `PROJECT_GUIDE.md:166`） |
| M3 | **一代的「不@不说话」= 概率触发**：普通消息进入活跃的概率 0.1；`@`/昵称/必回关键词/「针对 AI 的提问或挑战」= 必回通道 | 默认值 `bridge.js:277-279`；触发判定 `bridge.js:7595`（`Math.random() < triggerProbability`）；`isDirectAddress` / `isDirectedAtAi` `bridge.js:5901-5930` |
| M4 | **选择性沉默 + 「看到了但没回」**：活跃期普通闲聊按 `skipProbability`(0.15，刚发过言 60s 内不沉默，消息越多沉默率越低） 跳过；被跳过的消息存进 `silentContext`，**下次开口时一并投喂**给模型（真人视角：「我看到过，只是当时没接」） | `bridge.js:6240-6278`（skip 与 silentContext 落袋）、`bridge.js:6279-6290`（投递时 `seenMsgs + newMsgs`） |
| M5 | **冷场试探**：6 分钟没人说话 → 大概率回观望；25% 概率主动说一句试探，2 分钟无人接 → 100% 回观望 | 默认值 `bridge.js:286-292`；实现 `bridge.js:6290-6310` |
| M6 | **活跃超时主动退场**：进入活跃 15~30 分钟后主动收尾（防止在热闹群里「一直挂机说话」） | `bridge.js:286-288`；`triggerActiveDurationExit` `bridge.js:7563` |
| M7 | **观望期主动开话题**：群安静 30 分钟后进入可判定；每 45~90 分钟判一次，成功率 20% | `bridge.js:293-299` |
| M8 | **二代仿真 = 工具驱动**：文本**不自动转发**，一切发言靠工具；AI 用 `qq_set_wake_config` 自己决定「下次什么时候被叫醒 / 潜水多久」 | `agent.cordis.yml:1-5,23-31`；`RULES.md:16` |
| M9 | **唤醒条件白名单（这是「潜水」的正解）**：`mode: diving|active` + `infinite` + `triggers{atMention, nameMention, keywords[], question, poke, anyMessage, probability(0~1), speakerIds[]≤20}` | schema 与说明 `src/mcp-snowluma-safe.js:401-418`；判定 `bridge.js:5510,5532,5554` |
| M10 | **防「聊两句就潜水」= 沉睡前观察窗口**：设潜水前必须先 `qq_wait_for_messages(timeoutMs=300000)`，5 分钟内没人说话才能睡；期间有人说话要判断是否参与，参与了则重新等 5 分钟 | 默认值 `bridge.js:350-352`；拦截逻辑 `bridge.js:2891-2898`、`bridge.js:2756-2764` |
| M11 | **防「永眠」**：无限期潜水必须至少保留一个触发条件（@/名字/拍一拍/关键词/提问/anyMessage/概率>0），否则 400 拒绝 | `bridge.js:2882-2887` |
| M12 | **唤醒频率限流与「回合必须收尾」**：`maxWakePerMinute 1` / `maxWakePerHour 12` / `noActionLimit 3`（连续 N 个唤醒回合没动作就提醒）/ `maxWakeConfigReminders 2`；唤醒有租约防卡死（30 分钟强制解除） | `bridge.js:355-360`；`bridge.js:1223-1238` |
| M13 | **私聊不适用 speakerIds**（会被清掉），「频繁拍一拍」也不作为唤醒条件 | `bridge.js:2878`、`bridge.js:2874-2880` |
| M14 | **一代的 `[SILENT]` 出口**：模型输出 `[SILENT]` 表示「潜水/不接话」，桥接静默不发 | `bridge.js:124-125`（含「给 AI 合法沉默出口，而不是写内心戏被当消息发出去」的注释）；`PROJECT_GUIDE.md:129` |

### 2.2 「偷表情包」的完整实现（这是最值钱的一条）

| # | 机制 | 事实（文件:行号） |
|---|---|---|
| S1 | **两条路线**：①**同步 QQ 账号自带的收藏表情库**（源）②**AI 主动收藏群友发的表情**（偷图） | `bridge.js:729`（`syncStickerLibrary`）、`bridge.js:890`（`collectStickerV2`） |
| S2 | **同步收藏库**：`fetch_custom_face_detail{count≤500}`（OneBot）→ 合并进本地库；TTL 60s 缓存 | `bridge.js:729-758`；TTL/条数默认 `bridge.js:392-403` |
| S3 | **本地库 = 「AI 认知层」，QQ 收藏 = 源**：本地库只增补 `localNote/tags/usage/useCount/lastUsedAt/lastContext`，**不覆盖 QQ 的 desc**；QQ 侧 `desc` 为空时 AI 可以看图后自己写认知 | `src/sticker-lib.js:1-11`（设计原则）、`:24-56`（字段归一化） |
| S4 | **合并/去重规则**：以 `emoji_id`/`resId` 为唯一键合并；**URL 归一化只忽略协议、尾斜杠、query，不做任意子串匹配**；查找顺序 = `id/resId` 精确 → `md5` 精确 → URL 归一化 | `sticker-lib.js:69-113`（merge + **只在 `complete`（即返回条数 < 请求条数）时才允许清理 QQ 侧已删除项**，避免「分页截断被当成删除」）、`:114-142`（`stickerUrlKey` + `findSticker`） |
| S5 | **偷图触发条件（AI 自主，软触发）**：看到群友发的表情/图片「真的有意思、很戳你、或以后想用来回怼/接梗」时调 `qq_collect_sticker(messageId, remark)`；人格里明写「偶尔，别手贱」 | 人格 `agent.cordis.yml:57`；工具说明 `mcp-snowluma-safe.js`（`qq_collect_sticker`）；实现 `bridge.js:890` |
| S6 | **偷图的硬约束**（全在桥接层，不靠模型）：只能收藏**当前会话 recentMessages 里、且 `messageId/seq` 对得上**的消息；**不能收藏自己的**（`found.isSelf` → 报错）；这条消息必须有 media；**只取第一张** | `bridge.js:891-899` |
| S7 | **取字节而不是传 URL（关键安全设计）**：图片走 `fetchOneBotImage`（拿字节 → `base64://`），人脸/系统表情走 `fetchFaceMedia`；**严禁把消息里的原始 URL 交给 OneBot 去下载**（防 SSRF/防盗链），拿不到字节就「拒绝收藏」 | `bridge.js:900-916` |
| S8 | **写入 QQ 收藏**：`add_custom_face{file: base64://…}` → 拿 `emoji_id` → （可选）`modify_custom_face{emoji_id, desc}` 写备注，**备注 ≤20 字**（`maxRemarkChars`）；备注失败只记日志不算失败 | `bridge.js:917-940` |
| S9 | **偷图限流**：`collect.maxPerMinute 2` / `maxPerHour 10` | 默认值 `bridge.js:399-402`（深合并 `:461-483`）；HTTP 入口处校验 `bridge.js:3537-3553` |
| S10 | **发图**：`qq_send_sticker(stickerRef, replyToMessageId?, atUserId?)` → 先**强制 sync 一次**（保证「刚收藏的能用、刚删的不会继续发」）→ `findSticker` 解析 id/md5/url → **桥接内 `safeFetchBuffer(url)` 下载（DNS 固定 + 逐跳校验 + 大小上限）** → 组装 `[{reply?},{at?},{type:'image',data:{file:'base64://…'}}]` → OneBot HTTP `send_group_msg`/`send_private_msg`；**发送前 `sleep(randInt(800,2000))`**（真人发表情前有停顿）；与文本共用 `sendChain`（保证「先文字后表情」顺序不乱） | `bridge.js:781-855` |
| S11 | **一条消息只能一张表情、不能带文字**（硬规矩 + 人格都写了） | `bridge.js:800-812`（注释与组装）、`agent.cordis.yml:56` |
| S12 | **用图策略（软提示）**：每 3~5 轮来一张；不确定含义先 `qq_get_sticker_image` 看图再决定；**不暴露完整 URL 进上下文**（只注入「常用/有备注的 8 个」） | `sticker-lib.js:192-203`（`buildStickerStrategyHint`）、`:175-190`（`buildStickerContext`，`promptMaxStickers 8`） |
| S13 | **使用统计**：每次发送 `useCount++ / lastUsedAt / lastContext(≤200字)`，用于排序与选图 | `sticker-lib.js:224-239`（`markStickerUsed`） |
| S14 | **媒体暂存有上限**：收到的每张图按 `messageRef/seq` 存进内存 Map（`MAX_MEDIA_STORE_PER_KEY`），超限淘汰最旧 | `bridge.js:648-670`（`extractMediaFromSegments`）、`bridge.js:7350-7375`（`handleIncoming` 里落 media + 淘汰） |

### 2.3 说话节奏、记忆、身份、边界、黑话

| # | 机制 | 事实 |
|---|---|---|
| C1 | **分条与错落**：AI 用**空格**分句（一代）/ 数组（二代）；发送按随机间隔、有概率用长间隔；`maxMessageChars 500`；**burst 上限 8 条、1~3s 间隔、每分钟最多 8 条、每小时最多 60 条** | `PROJECT_GUIDE.md:191-199`；`bridge.js:365-374` |
| C2 | **抢话防护**：`wait.defaultQuietMs 8000` + `minQuietAfterNewMs 10000`（收到新消息后至少再等 10 秒）；人格里写「半句话结尾（你知道/等一下/我跟你讲）不要抢答」 | `bridge.js:379-386`；`agent.cordis.yml:36-37,124-133` |
| C3 | **群聊记忆（三类，按会话隔离）**：`activeTopic`（进行中的话题）/`pendingThought`（想说没说的）/`memberImpression`（对某人的印象，键=昵称）；存 `state/social-v2.json` 的 per-conversation 节点；上限各 20 条；`pendingThoughts` 有 `expiresAt` 到期剪枝 | 文件名 `bridge.js:52`；三类与剪枝 `bridge.js:4127-4139`；增/改/删/清 HTTP `bridge.js:4048-4180` |
| C4 | **「静默 turn」摘要投喂**：观望期未参与的消息攒成摘要，作为**不发到 QQ 的 turn** 交给模型 → 变成记忆 | `PROJECT_GUIDE.md:136`、`:166`（`flushSummaries`） |
| C5 | **黑话/网络用语学习**：滚动窗口攒够 10 条 → 独立学习会话提取候选 → **管理员确认才转正** → 只有 `confirmed` 的按出现次数注入 `【群聊黑话表】`（最多 8 条）；学习会话与 QQ 会话隔离 | `RULES.md:47-55`；默认值 `bridge.js:265-273`；实现 `src/slang-learner.js`（249 行） |
| C6 | **认人与引用**：群聊消息把 `@QQ号` **解析成群名片/昵称**；引用解析成「被引用人 + 原文」；「被引用的是机器人自己」→ 视为直接对 AI 说（即使没 @）；命令/指向判断只用「本条自己的文字」，避免被引用原文干扰 | `bridge.js:7357-7366`（`resolveAtName`/`resolveReply`）、`bridge.js:7376-7380`（`quoteTargetIsSelf` 与空文本过滤顺序） |
| C7 | **安全硬边界（桥接层，不经模型）**：无本地工具；QQ 动作只有白名单命名空间；发送强制命中 `allow.groups/allow.private`；**回复文本含本机路径/凭据特征 → 整条拦截不发**；角色由桥接注入，群友口头改角色无效；`silent` 模式下群友消息**根本不投递给 agent** | `RULES.md:35-45` |
| C8 | **管理员通道**：`/role`、`/silent`、`/active`、`/reset`、`/status` 由桥接**硬执行**，消息带【管理员】标记 | `RULES.md:30-32` |

---

## 3. 新架构现状：适配器实测能力矩阵（硬事实，不是推断）

### 3.1 已上线在跑（与任务书给的「只私聊」前提**不一致**，见下）

| 事实 | 证据 |
|---|---|
| 插件与平台都开着，NapCat 已连上 | `profiles/chat/config.yaml:10-12`（`plugins.enabled: [onebot]`）、`:28-29`（`platforms.onebot.enabled: true`）；`logs/gateway.log` 08:04:12 `[onebot] reverse-WS listener on 127.0.0.1:6700 (auth=on, read_only=False, debounce=10.0s/45.0s scope=private)` + 08:04:13 `client connected from 127.0.0.1` |
| 真在收发（私聊） | `gateway.log` 08:06:02 `inbound message: … chat=<OWNER_QQ> msg='能看到吗'` → 08:06:34 `response ready … session=agent:main:onebot:dm:<OWNER_QQ>` → 08:06:43 `sent 4 segment(s)` |
| `read_only: false`（会真发） | `config.yaml:34`；`onebot-state.json` → `"read_only": false` |
| **⚠️ `group_enabled: true`（与 RUNBOOK 记的 false 不一致）** | `config.yaml:38` = `group_enabled: true`；而 `plugin/hermes_onebot/RUNBOOK.md:8` 写的是 `group_enabled: false` → **文档漂移** |
| **⚠️ 群聊没有群号白名单**（只有总开关） | `adapter.py:511-525 _authorized_sender()`：群聊分支**只判 `group_enabled`**，不判 `gid`；`self.group_ids`（`adapter.py:96`）只用于出站判「群/私」，**不是准入名单** → 开关一开 = 小号所在的**任何群**都放行 |
| 小号实际在 3 个群 | 只读 API 实测 `get_group_list` → `1095283483`（大号、陆昭仪、棉棉）/ `<GROUP_ID>`（大号、jjj、棉棉）/ `<GROUP_ID4>`（大号、棉棉、棉棉），成员各 3 人 |
| 群会话键：**每成员独立** | `adapter.py:527-533`（`onebot:group:<群号>:<发送者>`）+ `FEASIBILITY.md:70-84`；Hermes `gateway/config.py:548 group_sessions_per_user: True` |
| **认人已经免费**：每轮把发信人昵称/群名片注入提示词 | Hermes 核心 `gateway/session.py:389-420`（`**Source:** Onebot (…)` + `**User:** <昵称>`；多人会话走 `[sender name]` 前缀） |
| `@` 能被识别成文本标记 | `onebot_proto.py:52-54` → `[@<BOT_QQ>]` |
| 防抖默认**只管私聊** | `debounce.py:29-47`（`scope=private` 时群聊直接返回 `None` = 不拦）；适配器默认 `debounce_scope=private`（`adapter.py:103-108`，`onebot-state.json` 里 `scope=private`） |
| 出站分段/节奏已在 | `segmentation.py`（`⁂` + 1.5~3.5s 间隔），`adapter.py:569-620` |

### 3.2 适配器**做不到**的三件事（决定了哪些要重写）

| 缺口 | 证据 | 后果 |
|---|---|---|
| **入站收不到图**：图片段只变成字面量 `[图片]`，没有 URL / file / md5 | `onebot_proto.py:41-42` | 偷表情包**当前拿不到图源**，必须改 `extract_text` |
| **纯图/纯表情消息被整条丢掉** | `onebot_proto.py:99-109`（`if not extract_text(event): return "no_text"`）；`face`/`mface` 段在 `:50-51` 被 continue 丢弃 | 群友只发表情、不说话 → 连事件都不算，采集无从谈起 |
| **出站只能发文字**：`build_action` 只组装 `[{type:'text'}]` | `onebot_proto.py:112-129` | 发表情包/图片必须新增图片段发送路径 |

### 3.3 NapCat 侧实测（只读 API 探测，未发任何消息）

| 探测 | 结果 |
|---|---|
| `get_version_info` | `NapCat.Onebot` / `protocol_version: v11` / **`app_version: 4.18.28`** |
| `get_image{file:"not-a-real-image.jpg"}` | `retcode 200, message "file not found"` → **接口存在**（与未知 action 的 `不支持的Api` 明显不同） |
| `get_file{file_id:"nope"}` | 同上 → **接口存在** |
| `get_group_list` / `get_group_info` / `get_group_member_list` | 全部可用；成员表带 `nickname/card/role`（认人手上有料） |
| `get_group_msg_history{group_id,count:3}` | 可用，返回完整 message array（含 `sender.nickname`、`message_id`、`time`）→ **潜水采集的历史兜底路线可行** |
| 配置里的关键开关 | `stack/napcat/config/onebot11_<BOT_QQ>.json:35` = **`enableLocalFile2Url: false`**（本地文件路径**不会**被自动转成 http URL）；`:37` `imageDownloadProxy: ""`；`:41` `downloadSpeedKBps: 256` |
| 磁盘 | `/opt/data` 82G 可用（64% 已用） |
| NapCat 的卷 | `docker-compose.yml:32-34` 只挂了 `config` 与 `ntqq` 两个目录 → **NapCat 自身 cache 目录不在卷里**，重建容器即丢 |

### 3.4 记忆链路（决定了群聊会不会污染私聊）

`profiles/chat/hindsight/config.json`（19 行，全读）：
```
bank_id: mianmian-history      ← 与干活门(default profile) 共用同一个 bank
auto_recall: true              ← 每轮都召回
auto_retain: true              ← 每轮都落库
retain_tags: "hermes,chat"     ← 静态标签，不区分群/私聊
retain_context: "棉棉与主人的 QQ 聊天（聊天门）"
```
→ **群聊一旦进 agent，群消息会以同样标签落进 `mianmian-history`，且干活门能召回。**
（Hermes 的会话库本身是分开的：群会话键 `agent:main:onebot:group:<gid>:<uid>`，所以「上下文串台」不会发生；**串的是 Hindsight 这一层**。）

---

## 4. 映射表：直接复用 / 小改 / 重写 / 不做（含人时估算）

> 估算口径：**人时**，含单测与自检，不含等主人拍板的时间；假设由熟悉本适配器的人来做。

| # | qq-bridge 机制 | 新架构落点 | 判定 | 人时 | 依据/理由 |
|---|---|---|---|---|---|
| 1 | 群准入白名单（`allow.groups`，发送强制命中） | 适配器 `extra.group_ids` 变成**准入名单** + 出站校验 | **小改** | 1.5 | 现在只有总开关（`adapter.py:511-525`）；`group_ids` 字段已存在（`:96`）只是没当闸门用 |
| 2 | 潜水 = 不@不说话（`atMention/nameMention/keywords/question/probability`） | **适配器内**判定：解析 `at` 段 QQ==`self_id`、昵称包含、关键词、概率 → 不命中就**只记录不投递**（0 次 LLM） | **重写**（逻辑可照搬，代码是 Python 新写） | 4~6 | M3/M9 的语义要移植；`onebot_proto.py:52-54` 已有 `at` 信息，缺的是 gate |
| 3 | 概率插话 + 退避 + 时段权重 | 适配器内小状态机（每会话：上次发言时间、沉默次数、指数退避、时段权重表） | **重写** | 6~8 | 一代 `skipProbability/退避/idle` 语义（`bridge.js:286-299,6240-6290`）；**必须留在适配器**，否则每条群消息都付费 |
| 4 | 潜水采集（只看不说、不触发 LLM） | 适配器：入站事件直接 append 到本地 NDJSON/SQLite 缓冲，**不 `handle_message`** | **小改** | 2 | `_ingest()`（`adapter.py:453-510`）已经在这里，「只看不说」= 提前 return |
| 5 | 偷表情包（采集群友表情） | 适配器新增：从 `image`/`face` 段取 `url`/`file` → `get_image`/`get_file` 拿字节 → 落盘 + md5 去重 → 索引 | **重写**（新能力） | 6~8 | 现在拿不到图源（§3.2）；去重/落盘/上限要新写（S4/S9 规则可直接照搬） |
| 6 | 发表情包 | 适配器新增图片段发送（`base64://` 或 data URL） | **小改** | 3~4 | `build_action` 只发 text（`onebot_proto.py:112-129`）；OneBot v11 的 `image` 段即可 |
| 7 | 「一条消息一张表情、不能带文字」 | 出站组装时保证；人格写一句 | **直接复用**（规则） | 0.5 | S11；`segmentation.py` 的分段逻辑不冲突 |
| 8 | 用图策略（每 3~5 轮一张、不确定先看图） | 人格（SOUL 群聊段）+ 可选：把「可用表情清单」注入 | **小改** | 1~1.5 | S12；注入需要适配器/工具提供清单，先只写人格也行 |
| 9 | 表情库同步 QQ 收藏（`fetch_custom_face_detail`） | NapCat 侧有同类接口？**需实测**（`fetch_custom_face_detail` 是 SnowLuma 扩展，NapCat 未必有） | **待验证→可能不做** | 0 | 见 §5 未知项 U3；拿不到就用本地库自建 |
| 10 | 引用回复（出站 `reply` 段） | 适配器出站加 `[{type:'reply',data:{id}}]`；Hermes 侧已有 `reply_to` 通道 | **小改** | 2~3 | `send()` 已收 `reply_to` 参数（`adapter.py:568`）但没用上；`reply_message_id`（`onebot_proto.py:87-96`）已解析入站引用 id |
| 11 | 引用解析成「被引用人 + 原文」注入 | 适配器用 `get_msg`/`get_group_msg_history` 取回被引消息文本，拼进 text | **小改** | 2~3 | C6；`onebot_proto.py:55-56` 现在直接 continue 丢掉引用段 |
| 12 | 认人（`@`→昵称、身份注入） | **直接复用**：Hermes 已注入 `**User:** 昵称`；`@` 已渲染成 `[@QQ]` | **直接复用** | 0 | `gateway/session.py:389-420`；`onebot_proto.py:52-54`。要做 `@QQ→昵称` 映射则 +1h |
| 13 | 分条 + 错落感（随机间隔、长短间隔） | **直接复用**：`segmentation.py`（`⁂` 切分 + 1.5~3.5s） | **直接复用** | 0 | `segmentation.py`、`adapter.py:569-620` |
| 14 | 抢话防护（`minQuietAfterNewMs 10s`） | **直接复用（私聊）** / 群聊要另配：防抖 `scope=both` 或群内自定义等待 | **小改** | 1~2 | `debounce.py` 群聊按「会话+发送者」分桶已实现（`:29-47`），只是默认没开 |
| 15 | 发送/唤醒限流（每分钟/小时上限） | 适配器加计数器（防刷屏 + 防封号） | **小改** | 2 | C1/M12 |
| 16 | 三类轻量群记忆（activeTopic/pendingThought/memberImpression） | 人格已有 MEMORY.md/USER.md（`SOUL.md:152-159`）可用；群聊**要按群隔离** | **小改**（隔离那步另算） | 2~3 | 与 #17 合并做更省 |
| 17 | 流隔离（群记忆≠私聊记忆） | **必须做**：Hindsight 同 bank + 静态 tag（§3.4） | **小改↔重写（取决于 Hermes 是否支持按会话分流）** | 2~6 | 未知项 U4；最坏情况=群回合关掉 `auto_retain` |
| 18 | 沉睡前观察窗口 / 防永眠 / 唤醒租约 | 只在「AI 自主决定潜水的工具化形态」下才需要 | **不做（现阶段）** | 0 | 那是「AI 自己管唤醒」的形态；新架构是适配器管节奏（#2/#3），不需要这套 |
| 19 | `[SILENT]` 标记 | — | **不做** | 0 | 新架构的「不说」是**不发**，由适配器 gate 决定；人格里没有「回一条 [SILENT]」的必要 |
| 20 | 空格分句 | — | **不做** | 0 | 新架构用 `⁂`（`SOUL.md` 已硬规矩） |
| 21 | 黑话/网络用语学习（独立学习会话 + 管理员确认） | 可选：Hermes 技能 + cron 完成 | **不做（本期）** | 3~5（若做） | C5；价值真实但属第二阶段 |
| 22 | 冷场主动开话题（30 分钟静默后小概率） | 已有心跳 cron（`proactive-messaging-cron`，`references/human-like-chat-mechanics.md` 的「时段权重」正是它的改进方向） | **小改（复用现有 cron）** | 2~3 | 现成链路，补「时段权重 + 概率」即可 |
| 23 | 回复审计硬拦截（路径/凭据 → 整条不发） | Hermes 工具面已窄（无 shell）；可在适配器出站加同一层正则兜底 | **可选小改** | 1 | C7；已有 `SOUL.md:178-192` 的「碰文件的规矩」在提示词层 |

**合计**：方案口径见 §6。

---

## 5. 技术风险与未知（每条都给**验证方法**，不许猜）

### U1 ⚠️【最高优先，已存在的漂移】`group_enabled: true` 到底生效了没有？
- 事实：`config.yaml:38` = `true`，但 RUNBOOK 记的是 `false`；网关 08:04:12 重启后 `read_only=False` 生效了，**说明 extra 里的自定义键确实能到达适配器**（`read_only` 不是 env 桥接键，`adapter.py:82`）。
- 反证：`onebot-state.json` 里 `rx_count: 0 / dropped_count: 3`，而同一时段群里有 3 条真实消息（`get_group_msg_history` 实测：08:04:36「哦哦我看看」、08:04:38 一条 @棉棉、08:05:06「1」）。我连续 4 次采样（31s 间隔）看到 `dropped_count` **恰好按 30s 心跳节奏 +1**（4→5→6→7），`rx_count` 只在我看到主人私聊那句时 +1。
- **结论：无法从外部证据判定群事件是否到达适配器**（心跳也计入 `dropped_count`）。**这是本次调研唯一没结清的项**，必须先验，否则上面所有群聊设计都建在沙地上。
- **验证方法（10 秒，只读）**：在群里发一条（需主人做），同时 `tail -f` 心跳文件看 `dropped_count` 是否**在心跳节奏之外**跳变 +1；或把那行 `logger.debug("[onebot] group message ignored (group_enabled=false)")` 临时提到 INFO 级看原因。**替代**：`python3 - <<` 直接用 `_proto.should_ignore` + `_authorized_sender` 逻辑跑一个真事件（把 `get_group_msg_history` 返回的原始事件喂进去）。

### U2 群消息是「全量推送」还是「只推 @」？
- qq-bridge 的 `bot.onGroupMessage`（`bridge.js:8285-8288`）**不过滤**，全量收，过滤在社交引擎里 → OneBot v11 + NapCat 是**全量推送**语义。新架构同理：适配器会收到群里每个人的每句话。
- **风险**：若闸门（#1/#2）没先做，主人的三个群（各 3 人，含主人自己的大号）一有动静就是付费回合。实测一轮群消息的代价参见：`gateway.log` 07:59 那轮私聊 `api_calls=11 / 62.2s`、08:06 那轮 `api_calls=7 / 32.4s` → **一轮 = 3~11 次 API 调用**，不是一次。
- **验证方法**：`onebot-state.json` 的 `rx_count` 增量配合 `gateway.log` 里 `inbound message: … chat=<群号>` 的条数（私聊是 chat=<QQ>，群聊会出现 9~10 位群号）。

### U3 NapCat 有没有 `fetch_custom_face_detail` / `add_custom_face` / `modify_custom_face`？
- 这三个是 SnowLuma 的**扩展 API**，OneBot v11 标准里没有。`get_data` 探测法：对未知 action NapCat 返回 `不支持的Api <name>`。
- 我**没有**探测这三个（避免触碰「写操作」类 API）。**验证方法（只读、零副作用）**：`fetch_custom_face_detail` 属读接口，可直接 POST 一次；`add_custom_face/modify_custom_face` 是**写操作（改主人 QQ 账号的收藏）**，必须主人拍板后再试。
- 影响：若没有 `fetch_custom_face_detail`，表情库就只能**自建**（本地目录 + 索引），这也完全可以（#5 的方案不依赖它）。

### U4 群聊记忆入库会不会弄脏主人私聊的记忆？
- **会**（配置级证据，见 §3.4）：`auto_retain: true` + `bank_id: mianmian-history` + 静态 `retain_tags`。
- **验证方法**：让一次群回合发生，然后 `GET /v1/memories?bank=mianmian-history`（Hindsight `172.17.0.1:8888`）搜群里的原文，看是否落库、`source/tags` 是什么；再在**干活门**（default profile）里搜同一句，能不能召回。
- 处置预案（按代价从小到大）：① 群会话回合**关 auto_retain**（若 Hermes 支持按 platform/session 条件化）② 另开一个 bank（`mianmian-group`）③ 保留同 bank 但把 `retain_tags` 从静态改成含 channel 变量。**三项都需要先实测确定可行性，别拍脑袋。**

### U5 「偷表情包」的图源过期问题
- qq-bridge 的解法（S7/S10）：采集时**立刻取字节**（`fetchOneBotImage`）→ `base64://` 交给 OneBot；发送时**每次强制 sync** 拿新 URL，再在桥接内下载（不走 URL 直传）。
- 新架构对应风险：`image` 段的 `url` 带签名/时效（AstrBot 为此写了 6 种参数名的 fallback：`/opt/data/abr/AstrBotDevs-AstrBot-b065505/astrbot/core/utils/quoted_message/image_resolver.py:30-61`，并在失败时明确 warn「failed to resolve … after N actions」）→ **不能把 URL 当长期资产存**。
- **落盘位置**：`/opt/data/chat-layer/stickers/`（现状存在但空）或 `profiles/chat/stickers/`——**必须在 `/opt/data` 卷内**（NapCat 自己的 cache 不在卷里，`docker-compose.yml:32-34`）。
- **容量**：单张表情按 100~300 KB 估，上限 500 张 ≈ 50~150 MB；`/opt/data` 现有 82G 可用 → 不构成风险，但**必须设上限 + LRU + md5 去重**（照搬 S4 的「id/md5 精确优先，URL 只做归一化，不做任意子串」）。
- **验证方法**：取一条真实含图群消息 → 调 `get_image` 拿到 `file`/`url` → 立刻 `curl` 该 URL 下载 → 记录 1 小时后是否仍可下载（判 URL 时效）；`file` 字段若指向 NapCat 容器内路径，**宿主可能读不到**（NapCat 的 `/app` 不在挂载里）。

### U6 防抖与「不@不说话」怎么共存？
- 事实：群聊默认**不进防抖**（`scope=private`，`debounce.py:44-46`）→ 群消息现在是一条一条即时投递，不会被攒 10 秒。
- **设计结论**：**群聊不要用「10s 静默窗合并」**。群里消息密，攒 10 秒既违反「人被 @ 要立刻反应」也浪费（等窗口期间每条都在攒）。可行组合：
  - `@`/点名/关键词 → **立即放行**（`debounce.is_command` 已有「立即执行」通道，`debounce.py:50-60`，可用 `debounce_wake_prefixes` 或扩展它）
  - 概率插话/潜水 → **适配器内决策**，命中才投递
  - 真的需要合并时用**短的**（1.5~3s）+ 硬上限，且只对「非 @ 的连续刷屏」生效
- **验证方法**：开群后看 `gateway.log` 的 `debounce released: N msgs / waited Xs` 行——群聊不该出现 `waited 10.0s`。

### U7 群里有人让她查身份 / 不良请求时的边界
- 现状已有：`SOUL.md:214`「**群里一律不接活**：派活只由主人在私聊触发」；`SOUL.md:247-253` 群聊段；`:377-380`「群聊解禁」（不主动瑟瑟、被带就用二次元调侃挡回去）；`:178-192` 碰文件的规矩（**路径保险已否，只靠提示词**）。
- 缺口：**「查人/查身份」没有明写**（qq-bridge 的对应物是 `RULES.md:19,38`：群友要求管理/查证一律拒绝 + 只允许白名单命名空间工具）。建议在 SOUL 群聊段补 2~3 句（**1 人时**）。
- **验证方法**：用一个非主人小号在群里发「帮我查下 XX 的 QQ/手机号」「你主人是谁」，看回复；判据是「不提供任何具体信息 + 不假装能查」。

### U8 Hermes 会不会因为群会话把主人的私聊上下文串了？
- **不会**：群/私聊会话键不同（`agent:main:onebot:group:<gid>:<uid>` vs `…:dm:<qq>`），且群内还按成员分桶（`group_sessions_per_user: True`）。**要串只可能串记忆**（U4）。

---

## 6. 三个可选方案

| | A. 完全不开群聊 | B. 只看不说（潜水采集 + 表情库存，不发言） | C. 全套（@才答 + 概率插话 + 偷表情包 + 发表情） |
|---|---|---|---|
| 改动面 | 0（但要**修** `group_enabled` 漂移：要么确认 true 要么改回 false，否则群一直在裸奔） | 适配器：群准入白名单(#1) + 潜水采集(#4) + 图片采集与落盘(#5) + 出站仍关（`read_only=true` 或群出站拦） | B + @/点名 gate(#2) + 概率/退避(#3) + 引用(#10/#11) + 发表情(#6) + 限流(#15) + 记忆隔离(#17) |
| 人时 | 0.5（修漂移 + 群白名单） | **4~8** | **25~40**（分两批：先 10~14 做「只回 @」，再 12~20 做「概率插话 + 表情包」） |
| 风险 | 低 | 低（**零 LLM 成本**、不发一言 → 不会被群友发现、不触风控） | 中（群里出声=暴露小号/被举报面变大；情绪化误发；记忆污染） |
| 每次群消息的 LLM 成本 | 0 | **0**（采集在适配器内完成） | **0 或 1 次回合**——取决于 gate：@/关键词命中=1 回合（实测 3~11 次 API 调用），概率插话=命中才 1 回合（建议概率 ≤0.05） |
| 回退方式 | — | `extra.group_enabled: false` + 保留已采集的表情库（或 `platforms.onebot.extra.read_only: true`） | 同上；`group_enabled: false` 一个键即可回到「群里不响应」 |
| 适合什么阶段 | 现在（先止血） | 想先攒素材、练手感、验证采集链路 | 主人确认「要她在群里说话」之后 |

**成本关键点（重要）**：不设 gate 的「群聊打开」= 群里每条消息都触发一轮对话。
以实测一轮 3~11 次 API 调用、单次结构成本约 9.3k token（`CHAT-AGENT-DIRECTION.md:27,36`）计，
**一个 3 人小群闲聊 50 句/小时，最坏情况≈ 每小时几十万输入 token**。
所以 **gate 必须在适配器层（进 agent 之前）**，写进提示词的「你不一定要回」不算成本控制。

---

## 7. 推荐与第一步

**推荐：先 A（止血）→ 再 B（只看不说）→ 验证 OK 后上 C 的「只回 @」子集，最后才给概率插话与表情包。**
一句话理由：**群聊的成本与风险都在「每条消息都进 agent」这一个开关上；先用零 LLM 的 B 把采集、隔离、白名单三条地基建好，再逐个打开出声的口子，才是可回退的路。**

**开工第一步（最小动作，约 1 人时，可回退）**：
1. **先去漂移**：确认 `profiles/chat/config.yaml:38` 的 `group_enabled` 到底该是 true 还是 false（配合 U1 的 10 秒验证），并把 `RUNBOOK.md:8` 同步成事实。
2. **补群准入白名单**：`adapter.py:511-525` 的群聊分支加 `gid in self.group_ids` 判定（`group_ids` 字段已在，`:96`），默认空 = 一个群都不放。**先写测试再改代码**（`tests/run_tests.sh` 现有 135 个用例）。
3. 这一步做完再谈 B/C。

> **未结清项（写在最前，别当已完成）**：U1（`group_enabled` 是否真的生效）；U3（NapCat 是否有收藏表情扩展 API）。

---

## 附录 A：本次搜索路径与关键词（供复核）

| 路径 | 关键词 | 结果 |
|---|---|---|
| `/opt/data/profiles/*/sessions/request_dump_*.json`、`/opt/data/sessions/`、`/opt/data/logs/*.log` | `github.com` + `群聊\|表情包\|潜水\|群` | 命中主人原话含 **`github.com/Derpyu520/qq-bridge`**（7 处），并明确「我之前给你发过这个」 |
| `/opt/data/cache/web/*.cache.md` | `群聊\|潜水\|表情包` | `github.com-1d41d004d882bc87.cache.md` = qq-bridge 仓库页（66 KB）；`github.com-53db6c769f28e8e7.cache.md` = AstrBot 插件清单（含「表情包偷取」「表情包管理器向量版」等邻近参考） |
| `/opt/data/chat-layer/**.md` | `qq-bridge` | `PLAN.md:177`「P2 群聊增强（可复用 qq-bridge 的设计）…`[SILENT]` 潜水、发送白名单」 |
| 技能库 | `潜水\|表情包` | 只有二手消化稿：`skills/communication/proactive-messaging-cron/references/{human-like-chat-mechanics.md, qq-group-interaction--human-like-chat-mechanics.md}`（讲的是 MaiBot）、`skills/.archive/qq-group-interaction/SKILL.md`（讲引用解读与整活） |
| GitHub | 克隆 `Derpyu520/qq-bridge`（commit `dea3ce8`，2026-09-18） | 拿到全部源码：`src/bridge.js`(8338 行) / `src/sticker-lib.js`(239) / `src/slang-learner.js`(249) / `src/mcp-snowluma-safe.js`(1092) / `RULES.md`(93) / `docs/PROJECT_GUIDE.md`(364) / `dsh/agent-presets/qq-chat-v2/agent.cordis.yml`(227) |

## 附录 B：本次做过的只读操作（披露，便于复核）

- 读文件：适配器源码/部署副本、`profiles/chat/config.yaml`、`hindsight/config.json`、`onebot-state.json`、`gateway.log`/`errors.log`、RUNBOOK/FEASIBILITY/LESSONS/PLAN/CHAT-AGENT-DIRECTION、NapCat 的 `stack/napcat/config/*.json`（含 token，未外传）。
- 跑 `doctor.py`（RUNBOOK 定义的只读自检）→ 结论「正常（1 项提醒）：alert_channel 不可达」。
- 对 NapCat 的 OneBot **HTTP API（127.0.0.1:3000）**发了 8 个**只读**请求：`get_status` / `get_login_info` / `get_version_info` / `get_group_list` / `get_group_info` / `get_group_member_list` / `get_group_msg_history` / 以及 `get_image`、`get_file`、一个不存在的 action（探接口存在性，参数是假值）。
- **没有**：发任何 QQ 消息、改任何配置、重启任何服务、装任何东西、碰 `add_custom_face` 之类写接口。
- 克隆仓库到 `/tmp/qq-bridge`（临时目录，不影响 `/opt/data`）。
