# Hindsight 队列语义：积压口径、阶段模型、心智模型刷新

排查 `hindsight` 队列时，**先分清「这是预期」还是「这是故障」**，再决定动不动手。本文给判据、源码位置与坑（详细端点/错误分类见 `hindsight-queue-triage.md`，本文件不重复）。

## 1. `batch_retain` 父 op 不是僵尸，`payload_null` 不是垃圾

- 每次提交 retain（哪怕只有 1 条）都会拆成 **1 个 `batch_retain` 父 + N 个 `retain` 子**。父行只写 `(operation_id, bank_id, operation_type, result_metadata, status)`，**没有 task_payload 列**（`memory_engine.py:13986-13999`）。
- worker 认领要求 `task_payload IS NOT NULL`（`poller.py:321,997`），`[PENDING_BREAKDOWN]` 把 `payload_null` 直接算成不可认领（`poller.py:1519`）→ **`claimable=0` 是设计使然**。
- 子终态后父由 `_maybe_update_parent_operation` 提升（`poller.py:590`）；真孤儿（子已终态但聚合漏提升 / 零子）由 worker 启动时的 `_reconcile_orphaned_parents`（`poller.py:1043-1160`）自动修，**不用手工清**。
- **约束**：父有活子时 `DELETE /operations/{op}/delete` 会被拒（`memory_engine.py:13217` 提示 resubmit+delete）；删父会破坏聚合。
- **后果**：`pending` 总数被父 op 撑大 → **不能当积压量**。判积压用 `/stats.pending_consolidation`，或 `[PENDING_BREAKDOWN]` 里 claimable 的 `retain`/`consolidation` 数。

## 2. 阶段模型：什么时候「卡住」属预期

| 阶段 | 判据 | 单槽被长任务占着 / retain 排队变慢 / extract 探针要等 |
|---|---|---|
| A 积压期 | `/stats.pending_consolidation` > 每轮上限（默认 100） | **预期：不掐 op、不重启、不告警** |
| B 见底后 | `pending_consolidation` ≤ 100（或 0） | 再卡才是异常，要查 |

- 物理原因：worker 单槽（`WORKER_MAX_SLOTS=1`）+ 本地 extract `--parallel 1` = **队头阻塞**，一条大 retain/整合就独占后端；此时给它发 1-token 探针也会排到超时（那是排队，不是后端死）。
- **「慢」还是「死」的唯一可靠判据是 LLM 调用还在不在成功**：近 N 条 `/llm-requests` 仍有 `success`（单次 50~500s 正常）= 在干活；一条成功都没有且同一条 op 长时间占着 = 真卡。
- 实测基线（供对比，不是阈值）：长抽取期间 op 的 `updated_at` 可停滞一小时以上而行不被写；`pending` 含父 op 可到 30+ 而实际待处理只有十几条。

## 3. 心智模型（mental model）为什么不刷新

1. 每 60s 的 `Maintenance: scheduled mental model refresh took 0.001s` 只处理 `trigger.refresh_cron` **非空**的模型（`maintenance.py:325,333`）；`refresh_cron` 为 `null` 时 `mental_models_with_cron()` 返回 0 行 → 立即 return。**0.001s 是「没候选」，不是「卡住」。**
2. 真触发点是**整合收尾**（`refresh_after_consolidation=true`；`consolidator.py:1419`、`maintenance.py:416`）。
3. **但只在「没撞轮次上限」的那一轮收尾才触发**（`consolidator.py:1393-1396`，命中则打 `skip mental model refresh (round limit hit, re-queued)`）。
4. 于是 `MAX_MEMORIES_PER_ROUND=100`（`config.py:1148` 默认）+ 积压远超 100 ⇒ **每轮必撞上限 ⇒ 永不刷新**。
5. **自愈**：积压降到 ≤ 每轮上限时会出现一次干净轮 → 刷新触发。想提前只有两条路：调大/置 0 该上限（一轮吞完，单轮更久 + 需重建容器），或给模型设 `refresh_cron`（绕过整合门槛，写 trigger）。
6. 判据：`GET /mental-models` 的 `last_refreshed_at` + `/operations` 里最近一条 `refresh_mental_model` 的创建/完成时间（两者吻合 = 之后一次都没跑）。**别用「日志有没有刷」判。**

## 4. 取数口径

- **积压/健康一律读 `/stats`**：`pending_consolidation`、`pending_operations`、`operations_by_status`、`failed_consolidation`、`last_consolidated_at`、`links_by_link_type`、`nodes_by_fact_type`、`total_documents/total_nodes`。
- ⚠️ **容器里没有 `psql`，也没有 `HINDSIGHT_API_DATABASE_URL`**（实测 `env | grep -i database` 为空）→ 「用 SQL 数未整合条数」的配方在容器内跑不通，**别写进定时任务**。

## 5. 一次性探针容器（拿不准某个 env 会不会把服务搞崩时）

同镜像 + 临时数据目录 + 空闲端口 + `--restart no` + 名字前缀（如 `hs-probe-`）；逐个变体跑若干分钟，看 `docker inspect` 的 `RestartCount` / `State.StartedAt` / `ExitCode` + 日志尾部定案。**任何 `rm`/`docker rm` 前先回显目标名带前缀**，用完拆除并列清理清单。

## 6. 两个会造出假结论的探针坑

- **`ss -tln | grep 8888` 会命中同机的 `18888`** → 得出「端口在监听」的假结论。要 grep 就锚定 `:<端口>\s`，更省事的是直接打 API（从消费侧容器内打 `172.17.0.1:<port>`，绕代理）。
- **`pending` / `payload_null` 不能当积压**（见 §1）。报数时分开说「积压（claimable）」与「pending 总数」，否则会把正常状态报成故障。

## 7. 任务类型速查（对着后台操作列表下结论前先对号）

| 类型 | 是什么 | 在队列里长什么样 |
|---|---|---|
| `retain` | 一条记忆的抽取入库 | 有 payload、**可认领** → 真在排队 |
| `batch_retain` | 一次提交的**父聚合器**，自己不下活 | 没有 payload 列 → `claimable=0` 是设计使然（§1） |
| `consolidation` | 记忆整合（去重/归纳，收尾才触发心智模型刷新） | 可认领，**最能长期独占单槽的那一类** |
| `graph_maintenance` | **删/退役记忆后的孤儿边清理** | 可认领，`retry_count=0` 属正常等槽 |

- **分类判据只看 `[PENDING_BREAKDOWN]`**：`claimable=1` = 正经排队等单槽（属 §2 阶段 A 的预期，不掐不重启）；`payload_null>0` 的是父 op，**既不能当积压也不能去删**。
- **`graph_maintenance` 平时见不到是正常的**：它只在记忆被**删除/退役**时才入队（全库只有两处调用 `enqueue_graph_maintenance`，其中一处就在「记忆没了」的路径上）。第一次看到它说明**刚有记忆被删或退役**，不是新开了什么开关——别往刚改的配置上归因。
- **判「真在推进」看换人，不只看数字**：一条长 op 跑完时 `processing` 会换成**另一条 op**（对照 `created_at` 变了 = 槽在流动）；只看 `pending` 总数会因父 op 与持续新增而看不出趋势。
