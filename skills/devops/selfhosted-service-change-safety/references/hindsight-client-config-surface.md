# Hindsight：客户端（Hermes 插件）配置面 vs 服务端

自建服务的「参数」常常分属两层，改错层 = 静默无效。这里是 Hindsight 两层的边界与核对法（铁律 7 的具体落地）。

## 三处核对源

| 目的 | 位置 | 怎么看 |
|---|---|---|
| **客户端**键名 + 默认值（权威） | `/opt/hermes/plugins/memory/hindsight/__init__.py` 的 `get_config_schema()` | `search_files(pattern="键名", path="/opt/hermes/plugins/memory/hindsight")` |
| 客户端参数人读表 | 同目录 `README.md` | 同上 |
| **服务端**版本与能力 | `GET /version`、`GET /openapi.json` | `curl -s http://172.17.0.1:8888/version`；openapi 存盘后在 `components.schemas` 里查字段与枚举 |
| **现场生效值** | 调用方各自的 config（每个 profile 一份） | 干活门 `/opt/data/hindsight/config.json`；聊天门 `/opt/data/profiles/<profile>/hindsight/config.json` |
| 库里实际有哪些标签 | `GET /v1/default/banks/<bank>/tags` | 分页拉全再筛，别只看首页 |

响应加 `-H 'Accept-Encoding: identity'`（大 JSON gzip 会崩）；落盘再解析（`-o /tmp/x.json`），别 curl 进解释器管道。

## 客户端键（核对用；改前先读现场文件）

| 键 | 默认 | 要点 |
|---|---|---|
| `mode` | `cloud` | 本机 `local_external` → 走 8888 |
| `bank_id` / `bank_id_template` | `hermes` | 本机两门共用 `mianmian-history` |
| `recall_budget` | `mid` | low/mid/high；现场值以 config 为准（长期记为 high 的笔记会漂移） |
| `recall_max_tokens` | 4096 | 不同门可不同（本机干活门 4096 / 聊天门 1024） |
| `recall_types` | `observation` | 要加 `world,experience`，否则漏原始事实 |
| `recall_tags` / `recall_tags_match` | 空 / `any` | **唯一的标签过滤入口**，静态 |
| `recall_sync` | false | true = 同步召回，加本回合延迟 |
| `auto_recall` / `auto_retain` | true | — |
| `retain_every_n_turns` | **1** | 1 = 每轮都 retain；**没有 overlap 类键** |
| `retain_async` | true | false 会同步等整篇抽取（本地抽取可达 20+ 分钟 → 超时失败） |
| `retain_source` / `retain_tags` | hermes / 空 | 写入侧身份与标签，软分区靠它 |
| `retain_context` | conversation between Hermes Agent and the User | 抽取上下文标签 |
| `prefetch_waits_for_retain` / `prefetch_retain_drain_timeout` | true / 10.0 | 预取等 retain 可见，不占回复路径 |
| `recall_indicator` / `retain_indicator` | true | 面向主人的会话要关 |
| `memory_mode` | `hybrid` | `context` = 只注入不给工具 |
| `timeout` | — | 本地抽取慢时给足 |

**不存在**的键（外部清单常见）：`retainOverlapTurns`、`recallOverlapTurns`、`recall_max_results`。见到先当成编的。

## 工具入参面（Agent 能做什么的上限）

- `hindsight_recall`：**只有 `query`**。
- `hindsight_reflect`：只有 `query`。
- `hindsight_retain`：`content`、`context`、`tags`（与 `retain_tags` 合并）、`occurred_at`。

推论：「让 Agent 自己按对话噪音收窄标签范围」在工具层做不到——要么改插件加参数，要么裸调 `POST /memories/recall`。

## 增量 retain 的真实机制（别当待办重做）

`sync_turn()` → `_retain_every_n_turns` 缓冲 → `_resolve_retain_target()`：
API ≥0.5.0 返回 `(session_id, "append")`，只发 `_session_turns[_last_retained_turn_count:]` 这一段新轮次（无重叠窗口）；
API <0.5.0 返回 per-process 唯一 document_id 且不带 update_mode，此时每次都是整段会话重发。
要调的是**频率**，不是「模式」。

## 服务端独有的能力（客户端不暴露）

`tag_groups` 复合布尔标签过滤：`TagGroupLeaf.match` ∈ `any` / `all` / `any_strict` / `all_strict` / `exact`，嵌套 `{"and": [...]}` / `{"or": [...]}` / `{"not": ...}` 递归。插件 schema 里没有它 → 只能裸调 API 时用。
