# hermes_memory 自动召回（v1.1.0）落地上线证据

日期：2026-09-23 ｜ 目标：**每轮对话前按当前用户消息自动语义召回 Hindsight 记忆并注入本轮上下文**（retain 与 recall 同一个模块）。
代码：`/opt/data/chat-layer/plugin/hermes_memory/` → 部署到 `/opt/data/stack/astrbot/data/plugins/hermes_memory/`（两侧 md5 一致：`main.py b42f0c62a8968beb55b44bce8e5d015e`）。

## 一、源码级证据（AstrBot 4.28.1，全部在容器内实测 grep）

| 事实 | 位置 |
|---|---|
| 注入钩子调用点（build_main_agent 之后、真发请求之前；返回真值会拦掉请求） | `astrbot/core/pipeline/process_stage/method/agent_sub_stages/internal.py:352` `if await call_event_hook(event, EventType.OnLLMRequestEvent, req):` |
| 事件类型枚举 | `astrbot/core/star/star_handler.py:230` `OnLLMRequestEvent = enum.auto()` |
| 插件侧装饰器 | `astrbot/api/event/filter/__init__.py:23` `from astrbot.core.star.register import register_on_llm_request as on_llm_request` |
| `req.contexts` 在「当前用户消息之前」铺开 | `astrbot/core/agent/runners/tool_loop_agent_runner.py:309` `messages = bind_checkpoint_messages(request.contexts or [])` → 之后才 append 当前 prompt |
| `contexts` 的形状被官方用于「注入辅助信息」 | `astrbot/core/astr_main_agent.py:361` 官方自己往 `req.contexts` append `{"role":"system",...}`（文件抽取结果） |
| 插件 handler 的绑定方式（`partial(raw, 插件实例)`） | `astrbot/core/star/star_manager.py:1271-1276` |
| 热重载门禁（默认关） | `astrbot/core/star/star_manager.py:222` `if os.getenv("ASTRBOT_RELOAD", "0") == "1": asyncio.create_task(self._watch_plugins_changes())` |

部署文件内的关键行（`grep -n` 于容器内）：`RETAIN_PATH:41`、`RECALL_PATH:42`、`HTTP_HEADERS:44`、
`RECALL_EXTRA_KEY:49`、`inspect_round:95`、`guard:135`、`_post_json:253`、`@filter.on_agent_done():321`、
`@filter.on_llm_request():361`。

## 二、实现要点（retain / recall 同一个模块）

- **一份客户端**：`HermesMemory._post_json()` 是插件里唯一发请求的地方，retain 与 recall 都走它；
  端点/bank/超时/请求头（含 `Accept-Encoding: identity`）是模块级常量，只写一处。
- **一套守卫**：`inspect_round(event)`（umo / 文本 / 群号 / 发送者 / 是否「回执注入轮」）+
  `guard(...)`（开关、噪声、命令前缀、群聊限制、会话白名单、回执注入轮），两个方向都调，差异用参数表达
  （retain：`enforce_umos=False`、群聊看 `retain_mode`；recall：只放行主人私聊 umo + 跳过回执注入轮）。
- **一份配置**：`data/config/hermes_memory_config.json` 里 `retain_*` 与 `recall_*` 并列（23 个键已写入，见下）。
- **一行启动日志**同时打两个方向状态：
  `[hermes_memory] 就绪 bank=mianmian-history api=http://172.17.0.1:8888 timeout=30s | retain=on(scope=both,min=4,max=2000,skip_report=False) | recall=on(umo=1,budget=low,top_k=8,min_score=0.0,chars=1200,timeout=3.0s,role=user)`
- **注入形状**：`req.contexts.append({"role": "user", "content": "<块>"})`（`recall_role` 可改 `system`），
  块以 `【自动召回·相关记忆】` 开头、条目 `- <记忆>`、结尾一行用法提示（不复述、不说「我查了记忆」、要更准自己用 `recall`）。
- **一回合只注入一次**：事件上打 `_hermes_memory_recalled`（工具循环会多次触发本钩子）+ 上下文内已有块的双保险。
- **降级**：失败/超时/0 条/非白名单会话/群聊/回执轮 → 一律不注入，钩子全程 try/except，绝不影响主流程。
- **改动未回归 retain**：老的 `入队 200 doc=… len=…` 日志格式保留（新文件 main.py:288）。

配置（已部署，root 属主，原文件备份在 `chat-layer/backup/hermes_memory_config.json.orig-20260923`）：

```json
{"hindsight_url":"http://172.17.0.1:8888","bank_id":"mianmian-history","timeout":30,
 "retain_enable":true,"retain_mode":"both","retain_min_chars":4,"retain_max_chars":2000,"retain_skip_report_inbound":false,
 "recall_enable":true,"recall_umos":["aiocqhttp:FriendMessage:<OWNER_QQ>"],"recall_min_chars":2,
 "recall_budget":"low","recall_top_k":8,"recall_min_score":0.0,"recall_max_tokens":1024,"recall_max_chars":1200,
 "recall_query_max_chars":800,"recall_timeout_s":3.0,"recall_role":"user","recall_preamble":"",
 "recall_tags":"","recall_tags_match":"any","recall_types":""}
```

## 三、自测（三套，都对**部署路径**的文件跑）

| 套件 | 命令 | 结果 |
|---|---|---|
| 纯逻辑 + 真 HTTP 自测（本地） | `python3 /opt/data/scripts/hermes_memory_selftest.py /opt/data/stack/astrbot/data/plugins/hermes_memory/main.py` | **53 PASS / 0 FAIL**，exit 0 |
| 同上，在 astrbot 容器内（同 Python 3.12/同网络） | `/opt/data/scripts/astr_run.sh hermes_memory_selftest.py /AstrBot/data/plugins/hermes_memory/main.py` | **53 PASS / 0 FAIL**，exit 0 |
| 「真框架」探针（真 AstrBot 装饰器/注册表/`call_event_hook`/真 `AstrMessageEvent`） | `/opt/data/scripts/astr_run.sh hermes_memory_live_probe.py` | **11 PASS / 0 FAIL**，exit 0 |

探针关键读数（真派发，等同 internal.py:352 那行）：

```
部署配置键 23 个；按 AstrBot 的方式绑定 handler 2 个
PASS | H1 on_llm_request 钩子已注册进 star_handlers_registry | [('inject_recalled', 'hermes_memory')]
PASS | H3 真事件 umo 与线上一致 | umo=aiocqhttp:FriendMessage:<OWNER_QQ>
[hermes_memory] 召回注入 n=8 chars=661 top=1.60 types={'world':3,'experience':4,'observation':1} 用时=0.25s
派发记录 (第几次, stopped, 新增上下文数, 注入块数, 用时s)：[(1, False, 1, 1, 0.251)]
PASS | H4 真 call_event_hook 未终止请求（返回 False）
PASS | H5/H6/H7/H8/H9 注入块/角色/延迟/本回合标记
PASS | H10 同回合后续请求不重复注入     PASS | H11 真群聊事件不注入
结论：真框架全链路通过
```

真注入块样例（测试脚本打印，生产日志**只打条数/字数/耗时**）：

```
【自动召回·相关记忆】
以下是系统按你这条消息从长期记忆库里语义检索到的片段（不是主人这次说的话）。
- 棉棉承诺以后主人饿了会先推荐这个食物 | When: 2026-09-23T05:01:32+00:00 | Involving: <OWNER> | 基于主人觉得好吃
- 主人表示某食物还挺好吃的 | When: …
- 主人发送的图片包含多个主题 | …
（共 8 条，661 字）
用法：自然融进回复，不要复述本段、不要说「我查了记忆」；要更精确的自己用 recall 工具查。
```

召回延迟实测（budget=low，同一批查询多轮）：正常 **0.24~0.73s**；本地抽取/嵌入抢 GPU 时冒过 **2.5~3.0s** 尖峰 →
超 `recall_timeout_s`(3s) 时按设计降级为「这轮不注入」。

## 四、当前线上状态（重启才生效）

- 运行中的 astrbot（started 2026-09-23T05:31:47Z，重启次数 0）**仍在跑 v1.0.0**：
  日志为 `hermes_memory.main:55 就绪 … mode=both`（旧格式）与 `hermes_memory.main:82 入队 200 …`（新文件对应行是 242 / 288），
  且 `[Detected file changes …]` 一条都没有 —— 说明 watchfiles 没跑（容器 env 里**没有** `ASTRBOT_RELOAD`）。
- 部署文件与配置**已就位**；缺少的动作只有「让 AstrBot 重新 import 插件」：
  ① 重启容器：`docker restart astrbot`（插件随之加载新代码，但不带新 env）；
  ② 或 `docker compose up -d`（顺带带上 compose 里新加的 `ASTRBOT_RELOAD=1`，以后改插件自动热重载）。
  重启聊天门属于主人决定的事，本次未执行。
- 生效后的复核三连（`SUDO_ASKPASS=/vol1/1000/<USER>` + `sudo -A docker logs astrbot`）：
  1. `grep '就绪 bank='` 应出现新的单行双状态日志；
  2. `grep '入队 200'` 继续出现（retain 未回归）；
  3. 主人发一条消息后 `grep '召回注入'` 出现 `n=… chars=… 用时=…s umo=aiocqhttp:FriendMessage:<OWNER_QQ>`。

## 五、遗留 / 风险

- 召回是**同步**的（按当前消息，符合主人意图）：正常给每轮加 0.25~0.7s；尖峰时最多等到 `recall_timeout_s`（3s）再放弃。
- Hindsight 的 `tags` 过滤偏软（`any` 模式下未知标签也会返回条目），故 `recall_tags` 默认留空。
- `_conf_schema.json` 里 `recall_umos` 是 list；AstrBot 只在「按 schema 补默认值」时写回配置文件，手工改配置后要重启才读。
