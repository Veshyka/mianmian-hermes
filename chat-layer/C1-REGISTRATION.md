C1「@ 必答」落地登记（2026-09-23）—— 追加，不改上文
=================================================

## 1. 一句话

三态唤醒（默认 `collect-only`，本次启用 `mention-only`）：**被 @ 小号 / 被回复她 / 正文点名**才唤醒一轮真实
agent 回合，上下文 = 该群滚动窗口（带昵称），回复发回该群；其余群消息与 B 阶段**逐字一致**（进窗口、0 LLM）。
**记忆隔离先取证后开启**：取证证明原版 provider 下群回合会写主库 `mianmian-history`，故先加闸门
`hindsight_guard`（群回合不写主库/不召回/不放行记忆工具），再开唤醒 —— fail-closed 写进了代码。

## 2. 取证（硬前置，先做后开）

`tests/evidence_group_retain.py`（把 provider 的 `_retain_batch` 换成捕获器；不发消息、不写库、不改配置）：

| provider | 群会话回合 | 私聊回合 |
|---|---|---|
| 原版 `hindsight` | **retain×1 → bank `mianmian-history`** | retain×1 |
| `hindsight_guard` | **retain×0** | retain×1 → `mianmian-history` |

→ 洞是真的（群回合会进主库）；闸门堵住群、不动私聊。闸门是官方 provider 的透明包装
（只改 `initialize`/`on_turn_start`/`prefetch`/`sync_turn`/`handle_tool_call`），判据 =
`chat_type∈{group,channel,supergroup}`（主）+ 正文 `source=qq-group`（兜底）。
运行证据落 `profiles/chat/hindsight_guard/state.json`，**只有计数，无正文**。

## 3. 改了哪些文件

| 文件 | 改动 |
|---|---|
| `plugin/hermes_onebot/group_wake.py` | **新增**：三态解析 / 命中判定（@·回复·点名）/ 限流 / 回合文本 / 记忆隔离体检（纯函数，可离线单测） |
| `plugin/hermes_onebot/adapter.py` | 三态 `group_wake_mode`；`_group_wake_trigger()` 三道闸；群回合正文 = 标记行 + 窗口 + 生人档规矩；她自己的群回复也进窗口；心跳加 7 个新字段 |
| `plugin/hermes_onebot/doctor.py` | 群配置真值改看 `group_wake_mode`；新增记忆隔离判据项（闸门未就位 = FAIL）；心跳字段清单扩容 |
| `plugins/hindsight_guard/`（chat profile） | **新增**：记忆隔离闸 provider（`__init__.py` / `plugin.yaml` / `config.json`） |
| `plugin/hermes_onebot/tests/` | 新增 `check_group_wake.py`（判据层）、`check_group_memory_guard.py`（功能级：真跑取证）；`check_adapter_e2e.py` 加 `TestGroupMentionWake`（9 例）；旧 B 阶段用例改述 |
| `scripts/health_all.py` | 官方 doctor 的 provider 行正则放宽为 `hindsight(_guard)?`（否则切闸门后该项会假红） |
| `profiles/chat/config.yaml` | `memory.provider: hindsight_guard`；onebot extra 加 `group_wake_mode: mention-only` + `group_memory_guard_required: true` + 限流键 |
| `plugin/hermes_onebot/RUNBOOK.md` | 新增「七、群回合记忆隔离闸」（装/验/回退 + fail-closed 三道闸） |

## 4. 配置键（`platforms.onebot.extra`）

| 键 | 默认 | 本次值 | 说明 |
|---|---|---|---|
| `group_wake_mode` | `collect-only` | `mention-only` | `collect-only` / `mention-only` / `full`；**写坏回落 collect-only**（fail-closed）；老键 `group_wake_enabled: true` → `full` |
| `group_memory_guard_required` | `true` | `true` | 闸门未就位 → **拒绝唤醒**（不是静默降级） |
| `group_alias_names` | `棉棉,小棉` | 默认 | 正文点名的名字（整串匹配，防话痨） |
| `group_reply_to_her_wakes` | `true` | 默认 | 被回复她说过的话 = 唤醒 |
| `group_context_inject_msgs` | `30` | 默认 | 注入窗口最近 N 条（窗口保留 200 条/256KB 不变） |
| `group_at_max_per_minute` / `_per_hour` | `2` / `30` | 同默认 | 每群上限；命中就 WARN + `group_wake_limited` |

## 5. 观测字段（`profiles/chat/onebot-state.json` / `doctor.py`）

`group_mode=mention-wake(付费回合，仅@/回复/点名)`、`group_wake_mode=mention-only`、
`group_llm_calls`（**只在唤醒时增长**）、`group_mention_count` / `group_reply_count` / `group_name_count`、
`group_wake_limited`（限流留痕）、`group_wake_blocked`（闸门未就位被拒）、`group_last_trigger`、
`group_memory_isolated`。

## 6. 验收（实跑）

- `bash tests/run_tests.sh` → **221 tests OK**（B 阶段基线 185 → +36）
- `doctor.py` → **exit 0**（唯一提醒是历史项 `alert_channel`：172.17.0.1:8098/health 不可达）
- 取证实验两条 → `VERDICT: PASS`（群 write×1 原版 / 群 skip、私聊 write 闸门版）
- `hermes -p chat doctor` → `✓ hindsight_guard provider active`
- **终验（留给主人）**：见 §7 —— 运行进程还是 B 阶段代码，需一次网关重启才生效

## 7. 未完成的一步 + 回退阶梯

**未完成（唯一）**：聊天门网关重启。写这份登记的时刻主人**正在私聊**（09:17 一条 DM、09:18 她刚回完），
按硬约束「不确定就先停手报告」**没有重启**。重启命令（约定俗成，与 B 阶段同款）：

```bash
docker restart hermes-chat          # 宿主视图；容器内 = /command/s6-svc -r /run/service/gateway-chat
```

重启后确认三件事（`onebot-state.json` + `logs/gateway.log` 的监听行）：

1. 监听行是 `group=mention-wake(付费回合，仅@/回复/点名)`（而不是 `collect-only(0 LLM)`）
2. 心跳 `group_memory_isolated=true`
3. `doctor.py` 里 `group_memory_guard` 显示 provider=hindsight_guard、闸门插件在位

**终验（主人做，一条就够）**：在群里 @ 她一句 → 她回话；同时
`group_mention_count` +1、`group_llm_calls` +1；随后在群里聊两句不带 @ → 两个计数都**不动**。
再验一次记忆隔离：私聊问她一件**只在群里说过**的事，她**不该**知道（正面：`group_llm_calls` 动了但
`hindsight_guard/state.json` 的 `retain_skipped` 同步 +1）。

**回退阶梯（一行一步，按软到硬）**：

| 想回到 | 改一个键 |
|---|---|
| B 阶段（只采集） | `group_wake_mode: collect-only`（**推荐**，记忆闸门留着无害） |
| 群都不看 | `group_collect_enabled: false` |
| 群消息直接丢 | `group_enabled: false` |
| 记忆回到原版 | `memory.provider: hindsight`（**连带**：群唤醒会被拒 → 等于同时关掉 C1） |
| 关掉闸门（排障） | `group_memory_guard_required: false`（**别长期开**） |

以上除 `group_enabled` 外都需重启网关生效。
