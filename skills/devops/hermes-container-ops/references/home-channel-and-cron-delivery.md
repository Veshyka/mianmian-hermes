# Home Channel 机制与「No home channel is set」提示（8/26）

## 现象

平台（如 QQ）收到 cron 投递的消息时附带英文提示：
```
📬 No home channel is set for Qqbot. A home channel is where Hermes delivers cron job results and cross-platform messages.
Type /sethome to make this chat your home channel, or ignore to skip.
```
群友可见，观感差，但不影响消息本身发送。

## 成因

- Hermes 需要一个 **home channel**（主频道）作为 cron 任务结果和跨平台消息的默认投递目标
- 平台从未执行过 `/sethome` → 该平台没有 home channel → 每次 cron 投递（如心跳消息）时系统自动附带提示
- 纯提示，不报错，心跳该发照发

## 解决与权衡

- 一条 `/sethome`（在当前聊天里发）即消掉提示
- ⚠️ **在哪设很重要**：在群里 `/sethome` = 以后所有 cron 结果默认投该群（心跳本来在群里无所谓，但其他定时任务结果也会刷进群，可能扰民/暴露隐私）
- 更干净的做法：心跳等 cron 任务在创建时显式指定 `deliver` 目标（如私聊 chat_id），或在私聊设 home channel；群里保持不设

## 排查速查

- 看到「No home channel is set for <platform>」→ 该平台缺 home channel，问主人想设在哪（群 or 私聊），别自作主张在群里设

## 指定投递目标：`deliver` 的显式写法 + `attach_to_session`

「把结果投到某一条对话」这件事是**投递层**的能力，不是 A2A 的能力：

- `deliver` 语法：`local`（只存档）/ `all`（该平台所有 home channel）/ `origin`（创建它的那轮对话）/ `bot-chat[:<profile>]` / **`platform:chat_id[:thread_id]`**，可用逗号组合（`origin,all`）；省略 = 创建它的会话。
- 目标 id 从哪来：该门目录下的 **`channel_directory.json`**（平台 → `{id,name,type}` 列表，含 dm/group 与 `thread_id`）。别猜 id，也别从对话正文里抄。
- **chat_id 是平台侧身份，`/new` 不影响它**：`/new` 换的是 Hermes 的会话槽（session key），投递按平台 chat_id 走 → 「固定投到某条对话、哪怕它被 /new 过」成立。要确认「这条对话是哪个 id」，读 channel_directory，别读当前会话 id。
- **`attach_to_session: true` = 把投递变成「可续的对话」**（主人能直接回复、下一轮带上下文；线程型平台进线程，DM 侧做镜像）。默认关。**显式 `platform:chat_id` 目标只有逐 job 打开这个开关才会被附到会话**（全局 `cron.mirror_delivery` 默认 false，且刻意不许把转录写进任意显式地址的会话）；`deliver: local` 时无效果。想要「接着聊」就必须显式打开。
- **谁来投**：投递必须由**持有该平台适配器的网关**发。跨门协作里，派活方只有自己的平台（例：聊天门只有 onebot 小号 + api_server + a2a）→ 它投不到另一道门接的平台 → 「投到某条对话」只能是**收活那道门**在任务书里读到目标后自己投。所以派活时把目标写进任务书（形如 `投递：qqbot:<chat_id>（attach_to_session=true）`），别指望对门自己猜。
- 与 A2A 的分界：`a2a_call` **没有投递目标参数**（只有 `agent`/`message`/`context_id`）；`context_id` 只决定 A2A 自己的会话槽（`…:a2a:dm:<ctx>`），与平台对话无关、也不受 `/new` 影响。
- **端到端验证配方**（60~90 秒一圈；别只读代码就下结论）：建一次性 job（`schedule: "in 1m"`、`deliver` 填目标、需要就 `attach_to_session: true`、prompt 自包含且只需输出一行自检文案）→ 等一轮 → 读 `cron/output/<job_id>/*.md` 确认跑过 + `action=list` 看 `last_status: ok` 且 `last_delivery_error: null`（`last_delivery_unverified` 有值 = 目标地址可达但未确认送达）→ 收尾 **`action=remove` 删掉自检 job**，别留垃圾。注意 `ok` 只代表调度器没报错，**是否真到要看接收端**（主人那边收到没有；判定「发出去了」不看发送方返回值，见 `multi-profile-a2a-dispatch.md` §8 的同一条规矩）。

## 出站推送：`hermes send` 与会话镜像的角色语义

**轻量出站通道**（比建一次性 cron 还轻，适合推短卡/单条回执）：

```bash
/opt/hermes/.venv/bin/hermes send -t qqbot:<chat_id> "<正文>"   # 也可 --file <path> / -s <标题> / --json
hermes send --list [<平台>]                                      # 看该平台已知目标
```

无 LLM、无 agent 轮、无 cron job，只发一条消息。`--json` 返回 `{success, platform, chat_id, message_id, mirrored}`；**`mirrored: true` = 同时往目标会话写了一条 delivery-mirror**（`gateway/mirror.py::mirror_to_session` → `state.db` 的 `messages` 表）。

**镜像行的角色是 `assistant`，不是 `user`**（`mirror_to_session(role=...)` 默认 `"assistant"`，就是为「机器人自己说出去的话」设计的；cron 简报这类「不是 agent 在说话」的文本要显式传 `role="user"`，否则制造 assistant→assistant 相邻对，strict-alternation 的 provider 会报错）。

→ 因此被问「对方推过来我能不能收到」时，**两件事必须分开答**：

1. **看得到**：它进了会话记录，下一轮读历史就能看到（主人不必引用那条消息）。
2. **但它不被当作「主人/对门的指令」，也不会叫醒你** —— 推送是**出站**动作，不触发一轮。想让对方**动起来**，只能走能触发一轮的**入站**路径（平台上来一条真实消息 / A2A `a2a_call` / api_server / webhook）。

**三个方向各管一段，别混**：

| 方向 | 手段 | 对方看得到 | 会让对方动 |
|---|---|---|---|
| 门 → 主人（任务卡/回执） | `hermes send -t platform:chat_id` 或 cron `deliver` | 会（mirror 行） | 不需要 |
| 主人 → 门（意见/审批） | 在该对话里**直接打字** | 会（真 `user` 行） | **会** |
| 门 → 门（派活） | A2A `a2a_call` | 会 | 会（对端一轮） |

配套口径：**别用推送去「给某个 agent 下指令」**（它只会变成对方自己的历史行），要它动就走入站；反过来，主人要提意见最直的路就是在那条对话里说话，不需要任何推送机制。

**验证镜像真的落进会话（别被自己的回复混淆）**：推一条带**唯一记号**的消息，且该记号**不写进你当轮的可见回复正文**；推完立即查会话库——`sqlite3 "file:<HERMES_HOME>/state.db?mode=ro"`，`select id, role, session_id from messages where content like '%<记号>%'`，期望命中一条 `role=assistant` 且 `session_id` 是目标会话。若把记号也写进了自己的回复正文，助手/工具行会一起命中，这条测试就废了。
