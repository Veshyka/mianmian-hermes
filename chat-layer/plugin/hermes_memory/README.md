# hermes_memory —— AstrBot ⇄ Hindsight 记忆桥（一个模块，两个方向）

同一份代码里管两件事，**共用同一套客户端 / 常量 / 守卫 / 配置**（别在任何一边复制粘贴出第二套）：

| 方向 | 时机 | 钩子 | 干什么 |
|---|---|---|---|
| **retain（自动回填）** | 一轮对话**结束**后 | `@filter.on_agent_done()` | 把「主人/群友说的 + 棉棉回的」写成一条记忆 |
| **recall（自动召回）** | 一轮请求**发出前** | `@filter.on_llm_request()` | 按**当前这条消息**语义检索 → 注入本轮上下文 |

## 数据流

```
                        Hindsight REST（宿主 172.17.0.1:8888）
                          bank = mianmian-history
  写入 POST /v1/default/banks/<bank>/memories            ← retain（async=true，不等抽取）
  读取 POST /v1/default/banks/<bank>/memories/recall     → recall（budget/top_k/min_score）
                    ▲                                        │
                    │                                        ▼
   一轮结束 on_agent_done                       一轮开始前的 on_llm_request
   inspect_round() → guard() ──放行──► _retain()      inspect_round() → guard() ──放行──► _recall()
                                                        └─► render_recall_block()
                                                            └─► req.contexts.append(...)  ← 就地生效
```

注入点为什么有效（源码级依据，AstrBot 4.28.1）：
`astrbot/core/pipeline/process_stage/method/agent_sub_stages/internal.py` 里
`if await call_event_hook(event, EventType.OnLLMRequestEvent, req): return` —— 这行在
`build_main_agent` 之后、真发请求之前；且 `contexts` 在 runner 里是
`bind_checkpoint_messages(request.contexts or [])` 先铺、当前用户消息再 append
（`astrbot/core/agent/runners/tool_loop_agent_runner.py`），所以注入块正好落在**当前消息之前**。
钩子返回 `None` = 不拦截（返回真值会让 AstrBot 直接 return 掉这轮请求）。

## 共享的那一份东西（改一处即可）

- 常量：`HINDSIGHT_URL_DEFAULT` / `BANK_ID_DEFAULT` / `OWNER_UMO_DEFAULT` / `RETAIN_PATH` /
  `RECALL_PATH` / `HTTP_HEADERS`（`Accept-Encoding: identity` 只写这里）
- HTTP：`HermesMemory._post_json()` —— **插件里唯一发请求的地方**，retain 与 recall 都走它
- 守卫：`inspect_round(event)`（一轮的 umo/文本/群号/发送者/是否回执注入轮）+ `guard(...)`
  （开关、噪声、命令、群聊限制、会话白名单、回执注入轮）—— 两个方向都调，差异用参数表达
- 配置：`DEFAULTS`（`retain_*` 与 `recall_*` 并列）
- 日志：一行启动状态 + 每条方向各一行结果（只打条数/字数/耗时，**不打正文**）

## 配置（`/AstrBot/data/config/hermes_memory_config.json`，schema 见 `_conf_schema.json`）

共享：`hindsight_url`、`bank_id`、`timeout`

回填 retain：`retain_enable`(true)、`retain_mode`(both|private_only)、`retain_min_chars`(4)、
`retain_max_chars`(2000)、`retain_skip_report_inbound`(false)

召回 recall：`recall_enable`(true)、`recall_umos`([主人私聊])、`recall_min_chars`(2)、
`recall_budget`(low)、`recall_top_k`(8)、`recall_min_score`(0.0)、`recall_max_tokens`(1024)、
`recall_max_chars`(1200)、`recall_query_max_chars`(800)、`recall_timeout_s`(3.0)、
`recall_role`(user)、`recall_preamble`(空)、`recall_tags`(空)、`recall_tags_match`(any)、
`recall_types`(空)

策略参照 Hermes 原生 Hindsight 插件（`/opt/hermes/plugins/memory/hindsight/__init__.py`，
只读参照、**不跨运行时 import**）：查询截断 800 字符、`budget`+`max_tokens` 入参、
结果按分降序拼 `- <text>` 条目、失败静默降级。差异：Hermes 默认是「后台预取、下一轮才用」，
主人要的是「按当前这条消息」→ 本插件用**同步召回 + 硬超时**。

实测召回延迟（本机 `budget=low`，同一查询多轮）：正常 **0.24~0.73s**；
本地抽取/嵌入共用 GPU，忙碌时会冒**秒级尖峰**（实测见过 2.5~3.0s）→ 尖峰超时就按上表降级
（这轮没记忆，下轮自然恢复）。嫌降级太频繁就把 `recall_timeout_s` 调大（代价是偶发多等）。

## 降级行为（任一情况都不注入，且绝不影响主流程）

| 情况 | 行为 |
|---|---|
| 不走白名单的会话 / 群聊 / 非主人 | 直接跳过（`umo_not_allowed` / `group_not_allowed`） |
| 文本是噪声（空、太短、`/`、`#` 开头） | 跳过（`noise`） |
| 这一轮是干活门回执注入轮 | 跳过（`report_inbound`，不给回执本身做召回） |
| Hindsight 报错（HTTP/网络） | 记 `warning`，本轮不注入 |
| 召回超过 `recall_timeout_s` | 记 `warning`，本轮不注入（不拖回复） |
| 召回 0 条 | 记 `info`，不注入 |
| 一个回合内的后续 LLM 请求（工具循环） | 事件上已有 `_hermes_memory_recalled` 标记 → 不重复注入 |
| 上下文里已有注入块 | 直接返回（双保险，防重复） |
| `req.contexts` 不是 list | 记 `warning`，跳过 |
| 钩子自身抛异常 | `try/except` 兜住，记 `warning` |
| retain 写入失败 | 记 `warning`（写入是异步的，不影响本轮回复） |

## 验证命令

```bash
# 1a) 纯逻辑 + 真 HTTP 自测（53 项，真连 Hindsight；本地直跑）
python3 /opt/data/scripts/hermes_memory_selftest.py
# 1b) 同上，但在 astrbot 容器里跑（与线上同 Python 3.12 / 同网络；主 .py 用部署路径）
/opt/data/scripts/astr_run.sh hermes_memory_selftest.py /AstrBot/data/plugins/hermes_memory/main.py
# 1c) 「真框架」探针：真 AstrBot 4.28.1 的装饰器/注册表/call_event_hook/真事件 + 真召回（11 项）
#     （脚本先落到 stack/astrbot/data/mianmian-tmp/，再 astr_run.sh 跑）
/opt/data/scripts/astr_run.sh hermes_memory_live_probe.py

# 2) 插件是否被 AstrBot 加载/热重载
sudo -A docker logs astrbot --since 10m | grep hermes_memory
#    期望看到一行： [hermes_memory] 就绪 bank=mianmian-history … | retain=on(...) | recall=on(...)

# 3) 线上真实回流：召回注入会留这条日志（条数/字数/耗时，无正文）
sudo -A docker logs astrbot --since 30m | grep '召回注入'

# 4) 写入是否真落库（下游证据，不看请求返回码）
curl -s --noproxy '*' "http://127.0.0.1:8888/v1/default/banks/mianmian-history/documents?limit=3"

# 5) 手动复现一次召回（改 query 看相关度）
curl -s --noproxy '*' -X POST \
  "http://127.0.0.1:8888/v1/default/banks/mianmian-history/memories/recall" \
  -H 'Content-Type: application/json' -H 'Accept-Encoding: identity' \
  -d '{"query":"主人喜欢吃什么","budget":"low","max_tokens":1024}'
```

## 部署与生效（重要）

| 步骤 | 命令 / 位置 |
|---|---|
| 1. 改代码 | 改 `/opt/data/chat-layer/plugin/hermes_memory/`（代码层） |
| 2. 同步到容器加载目录 | `cp main.py _conf_schema.json metadata.yaml README.md /opt/data/stack/astrbot/data/plugins/hermes_memory/` |
| 3. 核对两边一致 | `md5sum` 双向比对（容器内再看一遍 `/AstrBot/data/plugins/hermes_memory/main.py`） |
| 4. 配置（想改默认值） | 写 `/AstrBot/data/config/hermes_memory_config.json`（= 宿主 `stack/astrbot/data/config/`，**root 属主，要 `sudo -A tee`**）；只加键不用写，AstrBot 会按 `_conf_schema.json` 自动补默认值 |
| 5. **让 AstrBot 生效** | ①`ASTRBOT_RELOAD=1` 时**自动热重载**（compose 已加该 env，需容器重建过一次才带）；②否则**重启 astrbot 容器**（插件在启动时 import，不重启跑的还是旧代码）—— 重启聊天门属于主人决定的事，别自己动 |

改了插件**没生效**的典型症状：日志里找不到新那行启动信息、`召回注入` 一条都没有，而旧行为还在跑。

部署位置（两个路径必须 md5 一致）：
`/opt/data/chat-layer/plugin/hermes_memory/`（代码层，编辑这里）
→ `/opt/data/stack/astrbot/data/plugins/hermes_memory/`（容器实际加载的目录）

## 别踩

- 改完 AstrBot 会**热重载**（watchfiles 已装），日志里会出现
  `Detected file changes for plugin hermes_memory; reloading.`；配置改动则要看那一行启动日志。
- `req.contexts` 的注入角色默认 `user`（与同仓 `hermes_report` 生产验证过的注入一致）；
  想更强调「这不是主人说的话」可改 `recall_role=system`。
- 不要在这个插件里 import Hermes（`/opt/hermes/...`）的任何代码：运行时不同（AstrBot 插件基类 vs Hermes 插件），
  只做策略/协议层面的参照。
- 隐私：日志**只打条数/字数/耗时/umo**。要调召回质量请用上面的 curl 手工复现，别把正文打进日志。
- ⚠️ Hindsight 的 `tags` 过滤**偏软**：本机实测 `tags_match=any` 时，给一个不存在的标签**仍会返回条目**
  （连无标签条目一起返回）——所以 `recall_tags` 默认留空而不是收窄；真要硬过滤得用 `*_strict`。
