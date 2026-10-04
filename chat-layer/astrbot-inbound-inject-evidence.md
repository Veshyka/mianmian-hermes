# 回执以「入站消息」进 pipeline —— 实现与实测证据（2026-09-23）

主角：`/AstrBot/data/plugins/hermes_report/main.py`（宿主 bind mount
`/vol1/1000/<USER>`，即本仓 `chat-layer/plugin/hermes_report/`）
AstrBot 版本：**4.28.1**（`astrbot/__init__.py:3`）

## 走的路（推荐项，源码级依据）

合成一条 `AstrMessageEvent` → 交给平台适配器的 `create_event()` + `commit_event()`，
即与「NapCat 收到消息」完全相同的入口。

| 依据 | 位置 |
|---|---|
| `Platform.commit_event()` → `self._event_queue.put_nowait(event)` | `astrbot/core/platform/platform.py:147-149` |
| 所有适配器收消息都是这条路 | `.../sources/aiocqhttp/aiocqhttp_platform_adapter.py:510`（`self.commit_event(self.create_event(message))`） |
| aiocqhttp 的 `create_event()` 会挂上 `self.bot`（回复能真发出去） | 同上 `:492-506` |
| 事件队列消费端 | `astrbot/core/event_bus.py:36-49`（`dispatch()` → `scheduler.execute(event)`） |
| **官方给插件的同款封装＝公开 API** | `astrbot/core/star/star_tools.py:104-134` `StarTools.create_event()`；由 `astrbot/api/star/__init__.py:1,7` 导出（`__all__` 含 `StarTools`） |
| 私聊不需要唤醒前缀 | `pipeline/waking_check/stage.py:149-157` + `cmd_config.json:41 friend_message_needs_wake_prefix=false` |
| `is_at_or_wake_command` → 走完整 agent（带工具） | `pipeline/process_stage/stage.py:60-69` |
| 工具集来源（与事件无关） | `astrbot/core/astr_main_agent.py:599-617`（`req.func_tool = tmgr.get_full_tool_set()`） |
| 官方「合成事件」先例 | `astrbot/core/cron/events.py:14` `CronMessageEvent`（docstring: *Synthetic event used when a cron job triggers the main agent loop*） |
| `StarTools` 初始化时机 | `astrbot/core/star/star_manager.py:201` |

### 调研过的其它路（没走）
- `provider_ltm_settings.active_reply`（`builtin_stars/astrbot/group_chat_context.py:112` `need_active_reply`）：
  **只在已有群消息上决定「要不要搭话」**，没有入站消息就不会触发 → 不满足需求。
- `Context.get_event_queue()`（`astrbot/core/star/context.py:733`）：能拿到队列，但同一代码块
  被标注「以下的方法已经不推荐使用」→ 不用它，改走 `get_platform_inst()` + 适配器方法。
- 直接用 `context.send_message()`：**出站**，只往主人 QQ 发，不触发 agent → 不是主人要的。

## 开关语义（主人 2026-09-23 决定）

- **①`inject_enable` = false（已显式写进 `data/config/hermes_report_config.json`）**
  回执**不排队、不注入下一轮 LLM 请求**；只「推主人 QQ + 走②」。
- **②`proactive_inject_enable` = true**
  回执以入站消息推进 pipeline，跑一轮完整 agent。

启动时各打一行就绪状态（便于复核）：
```
[hermes_report] 开关① inject_enable=关（不排队、不注入下一轮，只推主人 QQ + 走②）
[hermes_report] 开关② proactive_inject_enable=开（回执以入站消息推进 pipeline，跑一轮完整 agent） 间隔=60s 上限=12次/小时
```

## 防循环
1. 入站正文带死标记 `（这是自动回执，不是主人发的）`（`INBOUND_MARK`），纯函数 `is_injected_text()` 可认。
2. 合成事件上打 `set_extra("_hermes_report_injected", True)`。
3. `on_llm_request` 钩子双保险判定 `_is_injected_round(event)`（extra **或** 正文标记）→ 直接返回。
   加正文判定是因为 **`hermes_debounce` 会把私聊里这批消息合并成一条新事件，extra 会丢**（实测）。
4. 闸门 `proactive_gate()`：两次注入最小间隔 60s + 每小时上限 12 次。

## 实测（2026-09-23 13:32，开关①关 / 开关②开）

POST 三条测试回执（`/health` 同步读数）：
```
#1 → {"ok":true,"sent":true,"injected":true,"inject_reason":"ok"}          health: pending_total=0
#2 → {"ok":true,"sent":true,"injected":false,"inject_reason":"too_soon(0s<60s)"}  health: pending_total=0
#3 → {"ok":true,"sent":true,"injected":false,"inject_reason":"too_soon(1s<60s)"}  health: pending_total=0
```

日志原文（AstrBot 容器日志）：
```
[13:32:44.736] [hermes_report] 开关①关着 → 回执不进队列（259 字），只推主人 QQ + 走②
[13:32:44.997] [hermes_report] 回执已作为入站消息推进 pipeline：275 字（umo=aiocqhttp:FriendMessage:<OWNER_QQ>），等她跑完这一轮 agent
[13:32:44.998] [Core] [INFO] [core.event_bus:74]: [default] [aiocqhttp(aiocqhttp)] 干活门回执(自动)/<OWNER_QQ>: 【干活门回执】✅ [AUTOTEST] switch1 off self test 1 - ignore
[13:32:55.006] [hermes_debounce] 放行一轮 原始1条 合并后len=275 等待10.0s
[13:32:55.159] [hermes_report] 入站注入那一轮的 agent 带了 41 个函数工具：['delegate_to_hermes', 'llm_view_feed',
   'llm_publish_feed', 'retain', 'sync_retain', 'recall', 'reflect', ..., 'astrbot_execute_shell',
   'astrbot_execute_python', 'astrbot_file_read_tool', ..., 'future_task', 'send_message_to_user']
[13:32:57.413] [Core] [INFO] [respond.stage:212]: Prepare to send - 干活门回执(自动)/<OWNER_QQ>: [引用消息] 又一条自动测试的 标得挺明白，忽略 你那边在调开关吧
```
`event_bus:74` 那行 = 它真的成了「一条入站消息」；`respond.stage` 那行 = 她跑完一轮并回了话；
中间那行 = **这一轮 LLM 请求带着 41 个函数工具**（直接测出来的，不是推断）。

## 自测脚本
- 纯单元（81 项）：`scripts/hermes_report_inbound_selftest.py` → `结论：全部通过`
- 旧兜底路回归（22 项）：`scripts/hermes_report_inject_selftest.py` → `结论：全部通过`
- 容器内真包探针：`scripts/hermes_report_inbound_probe.py` → `PROBE_ALL_PASS`
- 端到端发回执：`scripts/e2e_switch_test.sh`

## 升级 AstrBot 后可能坏在哪
- 用到的都是「平台适配器自身收消息用的方法」（`Platform.create_event`/`commit_event`）+
  `Context.get_platform_inst()` + `api/star` 导出的 `StarTools`，**没有 `_` 私有属性**，
  升级风险低。真正脆的是 `req.func_tool.names()`（自证日志用，坏了只丢日志）与
  `hermes_debounce` 的事件重建行为。
- 若某天 `waking_check` 改成「私聊也需要唤醒前缀」，入站注入会不唤醒 → 需给合成正文加 `wake_prefix`。
