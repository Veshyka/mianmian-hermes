---
name: hindsight-memory-engine
description: Hindsight 记忆引擎操作手册（Hermes 原生版）——retain/recall/reflect 工具用法 + 本机 172.17.0.1:8888 API 参考 + 调优与已知坑
---

# Hindsight 记忆引擎

## 判据别写死 provider 名（2026-10-04 假警报）
健康自检（`scripts/health_all.py`）曾用正则 `✓ hindsight(_guard)? provider active` 判「记忆 provider 就位」—— 把
`memory.provider` 换成新插件（`hindsight_flush`）后，官方 doctor 明明报 `✓ hindsight_flush provider active`，
自检却红了一条并告警给主人。规则：**自检判据用前缀/家族 `hindsight\w*`，不枚举具体 provider 名**；
换 provider、加插件时先跑一次 `hermes doctor | grep -A3 "Memory Provider"` 看它实际印什么。手册（Hermes 原生版）

Hindsight = 棉棉的记忆主库（MEMORY.md 参数层之外的外围记忆），运行在宿主 Docker 容器。**Hermes 侧已通过原生 memory provider 接入**（`memory.provider: hindsight`），日常用工具而非裸 curl。

## 环境速览（Hermes 容器视角）
- API：`http://172.17.0.1:8888`（FastAPI，无 API key，tenant=`default`；容器 bridge 网络经 docker 网关访问）
- Control Plane Web 控制台：`http://<宿主>:9999`（浏览器可视化，宿主侧访问）
- MCP Server：`/mcp`（32 工具，HTTP transport）
- PostgreSQL：容器内 5432（hindsight-data volume）
- 模型/后端（2026-09-19 起，**Ollama 已退役**）：LLM 抽取 = `llama-extract`（容器，8081，qwen3:8b）；embedding = `llama-embed`（容器，8082，qwen3-embedding:4b）→ 经 `llama-embed-trunc`（宿主 systemd，8085）截断 2560→1024 再给 Hindsight
- 同机 GPU：RTX 3060 12GB，**显存紧**（extract 5520 + embed 3972 MiB，仅剩 ~2.4G，llama-vision 挤不进来）
- 主库 bank：**`mianmian-history`**（2.6 万+ 条事实）——所有操作显式指定，勿依赖默认

## Hermes 原生接入（已配置，勿重复 setup）

配置文件：`/opt/data/hindsight/config.json`

```json
{
  "mode": "local_external",
  "api_url": "http://172.17.0.1:8888",
  "bank_id": "mianmian-history",
  "auto_recall": true,
  "auto_retain": true,
  "recall_budget": "high",
  "recall_types": "observation,world,experience",
  "retain_source": "hermes",
  "retain_tags": "hermes"
}
```

**自动机制**（每轮对话生效）：
- **auto-recall**：每轮开始前后台预取相关记忆，注入系统提示（`👁️ Hindsight — recalled N memories` 状态行）
- **auto-retain**：每轮响应后自动把对话原文同步进库（异步，本地 qwen 抽取）
- **显式工具**：`hindsight_recall`（检索）/ `hindsight_retain`（写入）/ `hindsight_reflect`（LLM 综合问答）

**重要**：如果 recall 返回空，先查 config.json 的 bank_id 是否仍是 `mianmian-history`（历史教训：MCP 时代默认 bank 不对导致 recall 空转，主人当考官实测验收过——链路咬合才算真能用）。

## 核心概念
- **bank（记忆库）**：隔离空间，每个有 retain_mission/disposition。主库 mianmian-history（另有 mianmian-history-v2 非主库，别查错）。
- **memory unit（记忆单元）**：retain 抽取的最小事实，带实体/时间/链接。
- **observation（观察）**：consolidation 合并去重后的高结构记忆，recall 默认层（更密，避免喂重复原始事实）。
- **causal 因果链接**：唯一由 LLM 显式抽取的「因→果」，记忆的「质」（当前占比低，靠 retain_mission 提升）。
- **fact（事实）**：可查计量。

## 底层 API 参考（curl，兜底/批量场景用）

所有 REST：`curl -X METHOD http://172.17.0.1:8888/v1/default/banks/mianmian-history/...`，无认证头。
**响应安全**：大 JSON 响应 gzip 崩溃时加 `-H 'Accept-Encoding: identity'`。

### Retain（写入）★
```bash
curl -X POST http://172.17.0.1:8888/v1/default/banks/mianmian-history/memories \
  -H "Content-Type: application/json" \
  -d '{"items":[{"content":"...","document_id":"diary_2026-08-22","metadata":{"import":"manual","source":"note"},"tags":["diary"],"timestamp":"2026-08-22T00:00:00Z"}],"async":false}'
```
- `async=false` 同步稳（单条）；`async=true` 异步快但占 worker 槽，大批量慎用
- 同内容重复 POST 走 DELTA 短路（幂等）

### Recall（检索）★
```bash
curl -X POST http://172.17.0.1:8888/v1/default/banks/mianmian-history/memories/recall \
  -H "Content-Type: application/json" \
  -d '{"query":"主人大学选了哪个专业","budget":"mid"}'
```
- 返回 `results`（text/entities/mentioned_at/document_id/scores:semantic+keyword+final）
- 默认返回 observation；要原始片段用 `recall_types` 或底层参数调整

### 标记记忆作废（软退役）★
```bash
curl -X PATCH http://172.17.0.1:8888/v1/default/banks/mianmian-history/memories/{id} \
  -H "Content-Type: application/json" -d '{"state":"invalidated","reason":"已过时：..."}'
```
- 语义（官方字段说明）：`invalidated` = **排除出 recall/consolidation、剪掉 links 与派生 observation、移入 archive**；改回 `"valid"` 即可 revert。**可逆**。
- ⭐ 作废后 `/memories/list` 就**再也查不到这条**了（进 archive 了）——别把“列表里没了”当成“被删了”。要做“这11条是否真生效”的复验，看它是否从 list 消失 + recall 不再吐它。
- 什么时候用：事实已被现实推翻时（如「口令是 X」而口令已换、「需由主人执行」而已完成），**标作废而不是 PATCH 改文本**——改文本只改动字面，不改“这条还成立”的语义。两条手段的分工：改文本=抹除敏感字面；标 invalidated=宣告旧结论作废。
- 现成脚本（/opt/data/scan_hs/）：`audit_cred_units.py`（按关键词审出候选）、`retire_stale_creds.py`（显式 ID 列表 → PATCH invalidated + 补正确条目，默认 dry-run）、`verify_retire.py`（复验状态 + recall 是否还吐旧条）。

### 图谱/实体/反思/心智模型/合并/指令/文档
```bash
GET  /v1/default/banks/mianmian-history/graph?limit=100          # 图谱 nodes/edges（temporal/semantic/entity/causal 四类 link）
GET  /v1/default/banks/mianmian-history/entities                # 全部实体
POST /v1/default/banks/mianmian-history/reflect -d '{"query":"..."}'  # LLM 综合问答（比 recall 贵）
GET  /v1/default/banks/mianmian-history/mental-models           # 心智模型（主人长期偏好归纳）
POST /v1/default/banks/mianmian-history/consolidate             # 合并去重成 observations（定期跑清积压）
GET,PATCH /v1/default/banks/mianmian-history/directives         # 长期指令（已建 2 条：「深入理解主人」「棉棉自我进化」）
GET  /v1/default/banks/mianmian-history/documents               # 文档管理
POST /v1/default/banks/mianmian-history/files/retain            # 上传文件转记忆
GET  /v1/default/banks/mianmian-history/export                  # bank 模板导出（跨机迁移）
```

### 配置与运维
```bash
GET,PATCH /v1/default/banks/mianmian-history/config   # bank 配置（retain_mission 在这里，改它=改抽取质量引擎）
POST /v1/default/banks/mianmian-history/health/llm    # 测试 LLM 连通
GET  /v1/default/banks/mianmian-history/llm-requests   # LLM 调用追踪
GET  /v1/default/banks/mianmian-history/audit-logs     # 审计日志
GET  /v1/default/banks/mianmian-history/operations     # 异步任务列表（失败任务 POST /operations/{id}/retry）
```

### 备份（生产级，宿主侧）
```bash
docker exec hindsight hindsight-admin backup /home/hindsight/.pg0/hindsight_backup_<TS>.zip
docker exec hindsight hindsight-admin restore <backup.zip>   # ⚠️会删光现有数据，恢复前必须确认
```
- 每日凌晨 3 点自动备份（保留最近 30 份），宿主机脚本 `scripts/hindsight-backup.sh`

## retain 时机与标签过滤的真实旋钮（2026-10-03 实测核实，别信误传）

- Hermes 插件侧只有 `retain_every_n_turns`（**默认 1 = 每轮都 retain**，`get_config_schema` + README 一致）；**没有** `retainOverlapTurns` / `retainEveryNTurns` 这两个键。
  误传来源已定位：`retainEveryNTurns=10 / retainOverlapTurns=2` 是 **Hindsight 官方 Codex / Claude Code 集成**（chunked 模式，窗口 = N+overlap）的默认值，不是核心服务端配置，写进本机 config.json 不生效。
- **增量 retain 已是现行为**：API ≥0.5.0 → `_resolve_retain_target()` 返回 `update_mode='append'` + `_last_retained_turn_count` 水位线，每次只发新增轮次，不重发整段会话、也没有重叠窗口。别再把它当待办。
- 标签分区现状：干活门 `retain_tags="hermes"`(source=hermes)、聊天门 `retain_tags="hermes,chat"`(source=hermes-chat)，同写 `mianmian-history`；库内**没有** `source:*` 标签。**两个 profile 都没设 `recall_tags` → 当前召回不做任何标签过滤**。
- `hindsight_recall` 工具**入参只有 `query`**（插件 RECALL_SCHEMA）→ Agent 无法按标签动态收窄查询；标签过滤只有静态 config：`recall_tags` + `recall_tags_match`(any/all/any_strict/all_strict)。要动态只能改插件或裸调 `/memories/recall`（服务端支持 `tag_groups` 递归 and/or/not，v0.4.18 引入；`TagGroupLeaf.match` 另含 `exact`）。
- **不存在 `recall_max_results`**。旋钮 = `recall_budget`(low/mid/high) + client `recall_max_tokens`（干活门 4096 / 聊天门 1024）+ 服务端 `recall_max_candidates_per_source`。
- ⚠️ 口径漂移待对齐：本 skill 下文写干活门 `recall_budget: high`，2026-10-03 实测 `/opt/data/hindsight/config.json` 是 **mid**。
- 库况基线（2026-10-03 13:20 stats）：nodes 31795+、links 1.17M+（caused_by ≈0.36%）、documents 1944、`pending_operations` 0、failed 0、`pending_consolidation` **422→378（正在被消化，自动整合已于 13:10 打开）**、`last_consolidated_at` 2026-10-03T13:17Z、`.config.enable_auto_consolidation=true`。

## 群记忆库 `mianmian-group` 与「分流」（2026-10-03 上线）

聊天门**群回合**不再走主库，而是写/读这个独立库（`hindsight_guard` 的 `mode: redirect`）：

* 建库：`PUT /v1/default/banks/mianmian-group`（body: name/mission/background），**创建时就会建向量表 + HNSW 索引**
* 群库配置要点：`retain_mission` 专抽「对人的印象」（昵称+QQ）、`recall_max_tokens=512`、`enable_auto_consolidation=true`
* 标签：`group:<群号>` + `speaker:<QQ号>`；召回按 `tags=[group:<群号>], tags_match=any` 过滤（跨群认人时去掉该过滤）
* 私聊/群双向封闭：私聊写 `mianmian-history`，群写 `mianmian-group`，互不召回
* 排查入口：`/opt/data/profiles/chat/hindsight_guard/state.json`（`mode`、`retain_redirected`、`recall_redirected`、`last_group_id`）

### ⭐ 建 bank 失败：`could not resize shared memory segment ... No space left on device`

2026-10-03 实测根因与修法（**别再往 shm_size/容器重建上奔**）：

1. 报错里的字节数（533,794,976 B ≈ 509 MiB）**正好等于 `maintenance_work_mem`**（当时 512MB）
   —— pgvector 建 HNSW 索引时把整块 `maintenance_work_mem` 放进 **DSM 共享内存**，
   而 hindsight 容器 `/dev/shm` 是 Docker 默认 **64 MiB** → 必然失败。
2. 先把 `max_parallel_workers_per_gather` / `max_parallel_maintenance_workers` 置 0：**无效**（同一个 509MB 请求出现）→ 不是规划器并行度的事。
3. 有效修法（**DB 级、可逆、不用重建容器**）：
   ```sql
   ALTER DATABASE "hindsight" SET maintenance_work_mem = '32MB';
   ALTER DATABASE "hindsight" SET work_mem = '16MB';
   ```
   ⚠️ `ALTER DATABASE SET` **只对新连接生效** → 必须让 API 的连接池重连（`select pg_terminate_backend(pid)` 踢掉旧后端，asyncpg 池会自动重连）。
   落地后 bank 一次建成。回滚：`ALTER DATABASE "hindsight" RESET maintenance_work_mem;`
4. 连接方式（容器内没有 psql）：用 `asyncpg` + `/home/hindsight/.pg0/instances/hindsight/instance.json` 里的
   username/password/port（5433）database=hindsight；脚本 `/opt/data/tmp/pg_fix2.py`（不打印口令）。

## 踩坑：包装 provider 时**必须过滤关键字参数**（2026-10-03 实测）

Hermes 会带 `messages=[...]` 调 `sync_turn()`，而基类 `HindsightMemoryProvider.sync_turn()` 不吃这个参数 →
`TypeError: got an unexpected keyword argument 'messages'`。Hermes 只记一行
`WARNING agent.memory_manager: Memory provider 'hindsight' sync_turn failed: ...`，
**不会中止回合**，所以表层看“闸门分支跑过了”，实际 retain 根本没建、计数停在 0。

写法：只转发**基类签名认识**的参数（`inspect.signature(getattr(super(), 方法名))`），
其余丢弃并记一行 INFO。`sync_turn` / `handle_tool_call` 两条转发路径都要过这层。

另一个观察：闸门/插件的 `state.json` 计数是**每 provider 实例**的，Hermes 每回合新建实例 →
计数不是累计值，且最后写入者不一定带最新数据（看着像“没生效”）。排查要同时看：
agent 日志的判别行 + 代理打印 + 服务端 `GET /banks/<bank>/operations`。

## 调优经验（已验证）
- **consolidation 现在是开的（2026-10-03 主人拍板）**：⚠️ 先记住真正的开关层级 —— **bank config 覆盖 env**，只看 compose 的 `HINDSIGHT_API_ENABLE_AUTO_CONSOLIDATION=true` 会误判成「已开」。查 resolved 值：
  `curl -s http://172.17.0.1:8888/v1/default/banks/mianmian-history/config | jq '.config.enable_auto_consolidation'`
  热改（无需重建）：`curl -X PATCH .../config -H 'Content-Type: application/json' -d '{"updates":{"enable_auto_consolidation":true}}'`
  落地验证看三行日志：`Updated bank config ...` → `Consolidation reconcile: scheduled 1 bank(s)` → `claimed 1 tasks (1 consolidation)`。
  先前「别开」的理由是 qwen3:8b JSON 截断死循环 + prompt 撞 8192 ctx；**两个前提都已消失**（换成 Bonsai 27B、llama-extract `-c 32768`，近 20 次 LLM 调用 20/20 成功），主人 2026-10-03 明确「现在就打开」。
  开启后的现实：`WORKER_MAX_SLOTS=1` 下整合与 retain **抢同一个槽**，retain 先占则整合排队（watch `my_active: retain:...(1)` → `consolidation:...(1)`）；一轮撞 `consolidation_max_memories_per_round=100` 会自排下一轮，把积压吃到 <100 后自行停。
  实测（2026-10-03 13:10 开）：422 → 378 条 / `last_consolidated_at` 00:41Z → 13:17Z / `failed_consolidation=0` / GPU util 98%、显存 free 2588 MiB。
  要停：`PATCH {"updates":{"enable_auto_consolidation":false}}`（不影响已在跑的那轮，要立刻断链就趁下轮 pending 时 `DELETE /operations/{id}`）。细节见 `ops-changelog/变更-20261003-打开自动整合.md`。
  历史教训仍有效：observations 合并的重复收敛依旧依赖 retain 端 DELTA + 稳定 document_id，别只靠 consolidation。
- **召回质量调优（服务端 env，2026-09-19 重新定值并实测）**：
  `HINDSIGHT_API_RECALL_STRATEGY_BOOSTS=semantic:high` + `HINDSIGHT_API_RECALL_MAX_CANDIDATES_PER_SOURCE=4`
  效果（10 道真实历史题）：hit@1 6/10→**7/10**，hit@3 8/10→**9/10**，平均名次 4.20→**1.80**，**每次召回 63 条(~19k tok)→9 条(~3k tok)**。
  这两项是 **static（服务端级）字段，bank config 无法覆盖**（`PATCH /config` 明确拒绝），只能走容器 env → **必须写进重建脚本** `/vol1/1000/<USER>`，否则每次重建容器都会静默丢失。⚠️ 2026-08-25 就丢过一次（只存在于一次手工 docker run 里），9/19 才发现——**任何调优都要落到脚本里**。
  cap 拐点实测：`0`→6/10(名次3.80/23条)、`2`→7/10(5条，但 MISS=1 过狠)、**`4`→7/10(名次1.80/9条 最优)**、`6`→6/10(14条)、`8`→7/10(18条)。官方默认值：boosts=`""`（完全不加权）、cap=`0`（不限流）。
  boost 变体实测：加 `temporal:high` 或 `graph:low` **无额外增益**（都是 7/10、名次 2.1/2.0）。
- **量尺（调优前必跑）**：`/opt/data/hindsight-eval/eval_recall.py` —— 10 道真实历史题 + 期望关键词自动判分，结果存 `results/*.json`，`--compare a.json b.json` 直接对比。**别再靠感觉说「变好了」**；`--budget` / `--max-tokens` 可扫描召回量。
- **retain/抽取侧的量尺（2026-10-01 建，换抽取模型/改指令时用）**：`/opt/data/hindsight-eval/retain_extract/`。做法是把 `GET /llm-requests` 里记下的 **逐字节真实提示词**（`input` 就是完整的 system+user 两条消息，含 retain_mission 前缀）喂给候选后端回放——比自己重写 prompt 靠谱得多，因为线上 prompt 由 bank config + chunk 切分 + schema 共同决定。三层判分（shell/strict-schema 合规 → 字段零瑕疵 → 抽取条数+因果边+防刷量）+ 配对 bootstrap CI + 符号检验，`run_extract.py`（串行/限速/退避/`--resume`）、`score_report.py --compare`、`make_report.py`。**换抽取后端前先量这把尺子。**
- **抽取任务「够不够格」还有一条与质量无关的先决条件**：推理型模型（Groq gpt-oss、nemotron 等）在 Hindsight 的调用形状里没有 `reasoning_effort` 预算控制，会把 `max_tokens` 全烧在 thinking 上、`finish_reason=length` 且 `content` 为空 → 抽 0 条事实。**测新后端时既看质量分，也要先确认它真吐得出正文。**
- **bank 级可热改参数**（42 个，PATCH 即生效、不用重建）：`recall_budget_function`、`recall_budget_min`、`recall_include_chunks`、`recall_budget_fixed_high`、`reflect_source_facts_max_tokens` 等；`recall_strategy_boosts` / `recall_max_candidates_per_source` **不在其中**（static）。
- **召回量的真正约束**：`POST /memories/recall` 的 `budget`(low/mid/high=100/300/1000) 与 `max_tokens`(默认4096)。实测 **`budget=low` 反而伤精度**（hit@1 掉到 4/10），减量要靠 per-source cap，不是降 budget。
- **插件端**：`recall_budget: mid` + `recall_types: observation,world,experience`（默认只 observation，会漏原始事实）。
  ⚠️ 2026-10-03 实测核对：本机两个 profile 的 `hindsight/config.json` 都是 **mid**（本 skill 旧文写 high 系笔误），主人同日确认「保持现状」→ 以 mid 为准。
- 因果链接是「质」但事件流水式经历难抽取；写记忆时明确表达因果句（「因为 X，导致 Y」）可增强
- ⭐ **因果边只能在“同一批抽取”（同一篇文档）内生成（2026-10-02 实测，三次独立取样共 605 条因果边，跨文档 0 条）**。
  机制：抽取器一次只读一篇/一个 chunk，写 `caused_by` 时引用的“原因”必须是**同一批输出里已经出现过**的条目（retain 指令里那条 `target_index 严格小于当前` 就是这个约束）。
  对照验证：同一取样里**实体边跨文档 34118 条**（实体靠共享实体节点跨文档，方法能看见跨文档边）→ 确认不是取样方法问题。
  推论：**跨天/跨话题的因果关系永远连不出来**，而人觉得“该有因果”的恰恰大多是跨天的（“因为U盘会坏→不依赖它”这类）。
  同批内它还**会过度归因**：一个显眼的因会被挂上一串同批相邻事件（实例：“修复内置 overlay 配置”→“增加 7 项检查/发现 SSH 空密码/做回退演练”三条）。
  ⚠️ API **没有创建/编辑链接的接口**（端点清单里 graph/entities 全是 GET，无 POST links）→ 事后补因果边只能直接写 pg（不建议）。
  ✅ 真正能改善的一招：**写记忆时把因果说进同一批**（“因为 X（旧结论）→ Y（新结论）”完整句）→ 跨天因果被搬进同一批。只改写入习惯，不动系统。
  旧笔记（“因果边全是知识类、0 条来自主人真实经历”）已过时：2026-10-02 当天那篇 173 条记忆里 35 条因果边全是他的真实经历/工作。
- **retain_mission 并入收紧约束（2026-09-21 落地，实测有效）**：`PATCH /config` body `{"updates":{"retain_mission":...}}`，把 `tmp/bonsai/tight_constraints.txt` 那五条（强制中文 / ≤8 条且 what ≤40 字 · 超了拆条 / 只抽有价值 / 因果 target_index 严格小于当前 / 只输出 JSON）追加在原中文 mission 之后，英文原文直接搬、不必翻译。实测：短输入→3 条中文原子事实；长叙述→**7 条（≤8 到位）**、全带 `When: +08:00` 精确时间戳、主体归一化 <OWNER>、长句被自动拆条。⚠️ 长度上限按「汉字数」判——英文专名/型号按原样保留、不计入，别按字符总数误判超限。备份 `/opt/data/hindsight/retain_mission.bak-<ts>.txt`。
- **改抽取指令的标准流程（2026-09-21 实操，含行为测试量尺）**：①`GET /config` 全量存盘（`/opt/data/ops-backups/hindsight-bank-config-<ts>.json`，带 md5）②`PATCH /config` body **必须是 `{"updates":{...}}`**（`BankConfigUpdate.updates`，只传要改的键，不要整份 config 覆盖；键名可用 `retain_mission` 或 `HINDSIGHT_API_...` 两种格式）③重新 GET 读回逐项核对（含原有条目是否逐字保留 + `retain_extraction_mode` 未变）④**行为测试走 `POST /memories/dry-run-extract`**——官方只读接口（'nothing is stored'），支持 `retain_mission`/`retain_custom_instructions`/`retain_extraction_mode` 等 **prompt-affecting override**；不传=用 bank 当前配置，传备份里的旧值=做改前 A/B。**这才是改指令的正规量尺，别靠「入队后去 list 里翻」**（队列一堵就等不到）。同输入 A/B 实测：旧指令抽 7 条（含「bank 现有 716 条记忆」「进度 12 中完成 3 条」「容器处于运行状态」「正在重跑监控脚本」「排队中剩 3 个任务」）→ 新指令抽 2 条（只剩 `recall_max_tokens=8000` 决策+理由）；反向对照「日常聊天会不会被误筛」：纯生活闲聊输入 6/6 全保留（疲惫早睡 / 姐姐换工作 / 面馆偏好 / 爸爸腰疼的担心）→ 证明是「照单筛中间态」而非整体变吝啬。⚠️ 改指令后**必须做这个双向测试**——只测「中间态被挡」会漏掉过筛风险，因为规则里「三个月后还有意义吗」和「日常聊天都记」天然有张力。⑤dry-run 在本地单槽 llama 上 **93s~770s** 不等（跟 bank 里正在跑的 retain 抢那一张 GPU 槽），客户端 timeout 给足 900s；`POST /memories` 同步路径也会 >120s 无响应，别以为失败了（实际已入队）。
- ⚠️ **retain 指令其实是两份**：`retain_mission` 与 `retain_custom_instructions`（同在 bank config）。后者更长，含「消息 [HH:MM] 前缀必须提取成精确 `occurred` 时间」这条。两份都进抽取 prompt；只改 mission 也生效，但排查效果要看全貌得两份一起读。
- embedding 切换模型后**必须重嵌入**，否则语义空间不一致匹配乱

## 已知坑（务必避免）
0.1 **「env 设了 true 却没生效」= bank 级 override 覆盖 env**（2026-10-03 实遇）：compose env `HINDSIGHT_API_ENABLE_AUTO_CONSOLIDATION=true` 与 bank config 都能设这个键，**bank 级优先**；`banks_needing_consolidation()` 的 SQL 门 (`COALESCE(config->>'enable_auto_consolidation','true')<>'false'`) 直接排除该 bank，日志里 `Consolidation reconcile: scheduled` 出现 **0 次**，retain 侧的 `enable_observations and enable_auto_consolidation` 门也被一起挡。症状：`pending_consolidation` 只涨不降（约 +33/h），服务本身完全健康、`failed=0`、LLM 调用全成功 —— 很容易误判成「队列堵了」。诊断一行：`GET /config` 看 `.config.enable_auto_consolidation`（**别只看 `.overrides`**，也别只看 compose env）。同理适用于任何「env 改了却没效果」的 bank 级字段 —— 先比 env 与 bank config 两级。
0. **删 bank 前先确认**：`DELETE /v1/default/banks/{bank_id}` 会级联删整个 bank（facts+documents+links）。2026-08-28 已删测试 bank：`mianmian-history-v2`(504)、`我自己测试`(12486)、`poc-test`(10)——删后主库 recall 正常。删 bank 不可逆，动手前必须主人确认。
0.5 **consolidation 在 qwen3:8b 上必死循环（8/3 与 8/28 两次验证）**：`enable_auto_consolidation` 保持 **false** 不要开！开了手动 `POST /consolidate` 会因 8b JSON 输出截断（`JSONDecodeError`）卡在 processing 死循环失败重试（30s/次），且 `DELETE /operations/{id}` 与 `/delete` 都拒绝删 processing 状态任务，只能等容器重启清理。consolidation 的 observations 合并功能在本地 8b 上不可用，重复收敛靠 retain 端 DELTA 幂等 + document_id 稳定。
   - **【2026-09-21 更新：换 Bonsai 27B 后老毛病消失，但出现两个新卡点】**实测（`consolidation_max_memories_per_round`=20、`consolidation_llm_batch_size`=4，手动 `POST /consolidate`）：首轮 20/20 条 **86.9s 跑完**，created 4 / updated 4 / failed 0，**无 JSONDecodeError、无死循环**——线程 8b 时代那个必死问题确认消失。但：①**每轮撞 round limit 会无条件自动再排下一轮**（内部 `submit_async_consolidation`，**不受 `enable_auto_consolidation` 约束**）——手动跑一次就等于启动一条会自己跑完整库（当时 1439+ 条待处理）的链；要停只能 `DELETE /operations/{id}` 把 pending 那轮置 cancelled（实测有效，链断后不再新排）。②**部分 tag scope 的 consolidation prompt 会涨到 10906 token，撞 extract 的 8192 ctx**：llama.cpp 返回 `400 exceed_context_size_error`，重试 4 次 + 拆 sub-batch（3→1/2，仍 9567 token）都超 → batch 被 skip，该轮从 87s 拖成 **8 分 03 秒**并触发 poller 的 `[STUCK?]`（是慢不是死）。**所以：单轮可用，整链要开必须先解决 ctx**（估算 `-c 8192→16384` 约多占 520 MiB，当前 GPU 余量 1074 MiB 够，但**未实测**）。试跑前已备份 bank（156MB zip，`hindsight_backup_before_consolidate_test_20260921_0700.zip`），试跑后 config 已恢复、真实 retain 验证正常（fact_count 28634→28643）。
0.6 **consolidation 换 Bonsai 27B 后的实测（2026-09-21，单轮压到 20 条）**：单轮**能跑通**——`POST /consolidate` 返回 operation_id（异步），20 条一轮 87s，created=4/updated=4 observations，**无 JSONDecodeError、无死循环**。但三个坑：①`hit_round_limit` 会**无条件自动再排一轮**（内部调 `submit_async_consolidation`，不受 `enable_auto_consolidation` 约束）→ 手动跑一次 = 启动全量链；要掐断只能趁下一轮还是 `pending` 时 `DELETE /operations/{id}`（processing 一律 409），或临时 `PATCH enable_observations=false`（下一轮 claim 时 `_run_consolidation_job` 开头直接 return disabled，不产生再排，事后务必还原）；②部分 tag scope 的 prompt 会涨到 **10906 tokens 撞 llama-extract 的 8192 ctx**（`exceed_context_size_error` HTTP 400），重试 4 次、拆 sub-batch 仍超、skip，一轮被从 87s 拖到 8 分钟——根治要么抬 `--ctx-size` 要么再压 `consolidation_source_facts_max_tokens`；③`enable_auto_consolidation` 仍保持 **false**，别开。
1. **改内容不变**：reprocess 内容字面不变 → DELTA 短路不重抽；强制全量重抽先 DELETE 该文档 chunks 再 reprocess
2. **级联删除**：删 document 级联删 memory_units + links（FOREIGN KEY CASCADE），慎删
3. **gzip 大响应**：66KB+ 文档 retain 时 gzip 崩，加 `Accept-Encoding: identity`
4. **同步 vs 异步**：大批量导入用同步串行更稳（async batch 超时会卡 worker 半天）
5. **改 retain_mission 用 PATCH**：`PATCH /.../config` body `{"updates":{"retain_mission":"..."}}`，把「主体归一化到 <OWNER> + 语义因果 caused_by」写进去，因果才会拉起来
6. **reflect_source_facts_max_tokens 别设 -1**（无限制）：本地 8b 处理不完会卡死，设有限值（如 4096）
7. **无 API key**：本机默认无鉴权，**别把 8888 暴露到公网**
8. **mental-models 的 tags**：传库中不存在的 tag 值 → reflect all_strict 过滤 → 召回空 → 生成 "I don't have information"
9. **embedding 维度链（切 llama.cpp 后必读）**：库向量列**固定 1024 维**且会校验返回长度，而 llama.cpp **忽略 `dimensions` 参数恒返 2560 维** → 必须经 8085 截断代理（MRL 截断 + L2 归一化）。代理必须**同时处理 float 列表和 base64**：Hindsight 写入路径发 float list，**检索路径发 base64（小端 float32）**；只处理 list 会让检索拿到 2560 维直接 500。
10. **同权重可免重嵌入**：GGUF 与 Ollama blob **字节数完全相同** = 同权重；实测两后端 embedding 余弦 **0.9983~0.9997（均值 0.9990，均为单位向量）** → 换*后端*不需要重嵌入。⚠️ 换*模型*（不是换后端）仍必须重嵌入。
11. **失败任务可补写**：`GET /operations` 看 `status=failed`，`POST /operations/{id}/retry` 重跑。Ollama 停摆期间（9/18 17:41、17:44）4 个 retain 失败，retry 后全部 completed（事实 28053→28064）。postgres 里的数据在 volume，容器重建不丢。
12. **`hindsight-extract`/`-embed` 标 unhealthy 是假警报**：compose 的 healthcheck 写死 `localhost:8080`，实际端口是 8081/8082，别按 unhealthy 判断服务坏了。
13. **⭐ pgvector HNSW 索引维度上限 2000（硬限制，2026-09-19 实测撞墙）**：把 embedding 维度改成 2560 **做不到**——实例启动自检 `ensure_embedding_dimension` 直接报错退出：`Embedding dimension 2560 exceeds pgvector HNSW index limit of 2000`。所以「llama.cpp 恒返 2560 + 8085 截断到 1024」**不是权宜之计而是唯一可行解**，别再把「改成 2560 就能省掉代理」当成优化方向（曾为此白跑一轮平行实例）。想要更高维度只能在 ≤2000 里选（如 2000）；要 >2000 必须换向量扩展（pgvectorscale/DiskANN）。
14. **换 embedding 模型/维度的正规迁移路子（已实测可用，几分钟级）**：`hindsight-admin export-bank -b <bank> -o <zip>`（只带文档/事实/观察/配置/心智模型/指令，**不含向量**）→ 目标实例按新维度配好 → `hindsight-admin import-bank -a <zip>`（用目标实例的模型**重算向量**并重建 links/索引，**不重跑 LLM 抽取**）。实测导出产物：1701 文档 / 27641 事实 / 438 观察 / 2 心智模型 / 3 指令，zip 仅 **14MB**。⚠️ 目标 bank 必须不存在（整体恢复，不是合并）。
    - 跑一个不同维度的平行实例做 A/B 而不碰生产：`sudo bash /opt/data/scripts/hindsight_test_2560.sh [import <zip>]`（8889 端口、独立 volume、embedding 直连 8082、`--restart no`；`down` 拆除）。脚本里 `/import` 是共享目录 `/vol1/1000/<USER>` 的只读挂载。

15. **凭证被当记忆存下时怎么清（2026-09-19 实操）**：API 0.8.6 **没有**单条 memory unit 删除（只有 `PATCH /memories/{id}` 与 `DELETE /memories/{id}/observations`；`DELETE /memories` 不带领 = 清空整库，禁用）；也没有 entity 删除/改名接口（`POST /entities/{id}/regenerate` 已 410）。做法：①先 `hindsight-admin backup` ②`PATCH /memories/{id}` 把 text 里的口令换成 `<口令已清除>`（保留事实与时间戳，不删 document——每篇有几十上百条无关事实）③复验用全库分页拉取 + 口令值哈希比对。工具已落盘：`/opt/data/scan_hs/recheck_creds.py`（查退役口令）、`/opt/data/scan_hs/check_current.py`（查**当前生效**口令是否在库），两者只打印 id/哈希/长度，**绝不打印明文**。⚠️ entity 名残留（canonical_name 本身就是口令）API 清不掉，需 SQL 或 bank 重导入。另：宿主 `scripts/mian_sudo.sh` 曾 755（other 可读=口令可被全机读到），已收 700——口令文件一律 700。

16. **⭐ reflect 攒爆上下文（llama-extract n_ctx=8192 被 400 拒）**：Hindsight 有个默认 `reflect_max_context_tokens=100000`——reflect 的 agent 循环攒到 10 万 token 才强制收尾，而本地 extract 只有 8192 → reflect 请求被 llama.cpp 以 `400 request (54989 tokens) exceeds the available context size` 拒，一定失败（2026-09-20 实测 8 次全灭）。该字段**是服务端级（bank config 拒绝覆盖）**，只能 env：compose 里加 `HINDSIGHT_API_REFLECT_MAX_CONTEXT_TOKENS=6000`。配套 bank 热改 `reflect_source_facts_max_tokens`（曾是 16384，比 ctx 还大 → 单次 tool 结果就顶满）→ PATCH 改 **4000**。改后实测 reflect 15.4s 出答案，日志出现 `Context budget exceeded on iteration 3: ~33579 tokens >= 6000 limit. Forcing final synthesis.`，各次请求 in=994~4055 token，稳在 ctx 内。
16.5 **⭐ llama-extract 的「实时上限 ≠ compose 里写的值」，且 OOM 后不会自愈（2026-10-01 实测，尚未根治）**：`docker inspect llama-extract --format '{{.HostConfig.Memory}}'` 与容器内 `/sys/fs/cgroup/memory.max` 都显示 **3195011072 B ≈ 2.98 GiB**，而 `llamacpp/docker-compose.yml` 里写的是 `mem_limit: 7g` → **这个容器自创建以来没被重建过，7g 从未生效**。Bonsai 27B + mmproj 的真实占用约 3.0–3.1 GiB，于是**长提示词（~3.5–4.2k input token）会反复把它打穿**（一天见 4 次 `Memory cgroup out of memory`），表现为 8081 连接被拒/HTTP 503。更坑的是它的 **`restart policy = no`**：OOM 后**不会自愈**，必须外部 `cd /vol1/1000/<USER> && docker compose up -d llama-extract` 拉回（`docker compose up -d` 对已存在的容器只 start、不 recreate，所以 7g 仍不会生效）。判据别信 `docker ps` 的 `unhealthy`（healthcheck 写死 8080，恒假警），要看 `docker ps -a` 的 `Exited (137)`。跑批量抽取前先探测 8081 健康，不健康就先拉回。

17. **⭐ llama-extract 的 cgroup OOM 重启循环（retain 失败的真正大头）**：`mem_limit: 2g` 对 qwen3-8b 太紧——`memory.current` 长期顶在 memory.max（`memory.events` 的 max 计数上万），一旦 anon-rss 涨到 ~2.04GB 就被内核 OOM 杀（`dmesg -T | grep 'Memory cgroup out of memory'`，进程名 llama-server，exit 0 但 `docker inspect` 的 OOMKilled 是 false，光看它会被骗）。每次重启要 ~35-40s 重载模型，窗口内 Hindsight 全拿 `HTTP 503 Loading model` → retain 判失败（一天 9~11 次）。缓解（已落地）：`HINDSIGHT_API_LLM_INITIAL_BACKOFF=10`（原 1s，1/2/4s 三次全撞重载窗口）。**根治（2026-09-20 已落地，主人拍板）**：`/vol1/1000/<USER>` 里 `llama-extract` 的 `mem_limit: 2g → 4g`（宿主 15.8G 总内存、当时可用 7.4G）。改后 `docker compose up -d --force-recreate llama-extract` 重建，`HostConfig.Memory=4294967296`、`/health` ok、dmesg 无新 OOM、`RestartCount=0`、真实 retain 成功落库（documents +1）。⚠️ `memory.current` 会显示 ~3.7G/4G（mmap 的 GGUF page cache 计入 cgroup，属可回收，不是真占用）。注意 llama 栈的 compose **不是** stack/ 下那个，别改错文件。根治（需主人拍板，属宿主资源改动）：mem_limit 2g→4g（宿主 15G 总内存，当时 available 7G）。
18. **卡在 processing 的僵尸 op（worker_id 变了就永远回不来）**：poller 的崩溃恢复 `recover_own_tasks` 只认 `worker_id = 当前 worker_id` 的 processing 行，而 worker_id 默认取 hostname（`DEFAULT_WORKER_ID=None → hostname`），**容器一重建 hostname 就变**（历史值见过容器 ID、`hindsight`、宿主名）→ 旧 worker 占的任务成永久僵尸（2026-09-18 那条卡了两天，`DELETE`/`retry` 都 409 拒绝，因为二者只收 failed/completed/pending）。修法：①（已落地）compose 加 `HINDSIGHT_API_WORKER_ID=mianmian-worker` 固定 id，以后能自动回收；②清存量僵尸：直连 pg 复刻它的恢复 SQL（只读诊断/恢复脚本 `/opt/data/tmp/hs_zombie_recover.sh`，口令从容器内 `.pg0/instances/hindsight/instance.json` 的 password 键读，不落盘不打印）：`update async_operations set status='pending', worker_id=NULL, claimed_at=NULL, retry_count=coalesce(retry_count,0)+1, updated_at=now() where operation_id=... and status='processing'`。
19. **retain 的 `batch_retain` 父 op 是聚合器（payload NULL）**：重试要挑**子 op**（`task_payload` 非空、result_metadata 里有 `parent_operation_id`），重试父 op 没用——它 claimable=0，要等子 op 终态后由 `_reconcile_orphaned_parents` 收尾。另：失败任务的 `error_message` 会一直挂在字段里，**看 status 不看字段**。
20. **重试 retain 会重写该 document_id 的既有事实**：不是无副作用的重放——同 document_id 重抽会刷新文档（text_length/unit 数会变，实测 200→196），全库 fact_count 有小幅波动（±百级）。重试前想清楚。
21. **Hindsight 并发度 vs 单槽 llama-server**：默认 `HINDSIGHT_API_LLM_MAX_CONCURRENT=32`，retain 会把一个文档的所有 chunk **同时**发出去，而 llama-extract `--parallel 1` 只有 1 个槽 → 请求排队、单条 wall time 拉到几分钟、poller 每 30s 刷 `[STUCK?]`/`[STUCK_STACK]`（阈值 300s，这只是慢不是死）。要治排队过深可加 `HINDSIGHT_API_RETAIN_LLM_MAX_CONCURRENT=1~2`（未落地，属推测性缓解，无实测证据）。
    - 【2026-09-21 观察】这条在真实事故里确实会表现为「队列长时间不排空」：我一共见到 16~17 条 pending、一条 retain 自 15:10 卡在 `processing` 到 16:00 仍未终态，期间新入队的 retain 一律不动（非自杀失败、`failed=0`，poller 无 `[STUCK?]` 日志可查时容易误判成坏了）。**此时不要去删/重试别人的 operation**——先看 `GET /operations?status=processing` + `GET /llm-requests` 最新 `duration_ms`（见到 400~980s 的单次调用就属此类排队），再决定；改抽取指令的效果复验可以完全绕开队列，用 dry-run-extract。

### 数链接/图谱的权威口径（2026-10-02 实测，这里踩过坑）

- **数各类链接只用 `GET /v1/default/banks/<bank>/stats` 的 `links_by_link_type`** ✓ 权威。
  实测主库：total_links **1,168,863** —— semantic 551,746 / temporal 589,596 / entity 23,300 / **caused_by 4,221**（因果边占 **0.36%**）。
  nodes_by_fact_type：world 21784 / experience 8681 / observation 703；total_nodes 31168。
- ⚠️ **`GET /graph?limit=N` 的取样不可信，别拿它计数**：同一库 limit=5000 给 caused_by 848、limit=20000 反而只有 38、全库 31166 只给 24 —— **limit 越大越掉数据**。它只适合看局部图。
- ⚠️ `GET /graph?type=` 的 `type` 是**事实类型**（observation/world/experience），**不是链接类型**；传 `caused_by` 会返回空。
- 因果边在控制台图上是 **灰色 `#999999` 实线**（entity `#ffd700` 金实线 / temporal `#00bcd4` 青虚线 / semantic `#ff69b4` 粉实线），且前端**没有图例文字**（14 个 js 里只有 1 处出现 `linkType`）→ 主人问过“是不是没有因果边”，答案是有、但 0.36% 的灰线在图上几乎看不见。
- 接口里它叫 **`caused_by`**（不叫 causal）；方向是 结果 → 原因（`source` 是被引起的那条）。

## 验证命令（链路咬合测试）
```bash
curl http://172.17.0.1:8888/health                     # 服务健康
curl http://172.17.0.1:8888/v1/default/banks            # 列 bank + fact_count
curl -X POST http://172.17.0.1:8888/v1/default/banks/mianmian-history/memories/recall \
  -H "Content-Type: application/json" -d '{"query":"兰州博文科技学院","budget":"mid"}'  # 应秒出精准记忆
```
**评测法**：用主人当考官的实际提问验证（如 MC 服务器 mod 版本兼容这种具体细节），别只看文档——能精确翻出历史才算真能用。

## 双轨记忆定位
- MEMORY.md（Hermes 内置，2200 字符）= 参数层/核心规矩；Hindsight = 细节库/记忆主库
- MEMORY.md 里相关条目旁标「细节在 Hindsight，查 recall」，启动读 MEMORY.md 被引导查 H
