# Hindsight 写入失败 / 任务队列排查

用于：`hindsight_retain` 报 `Failed to store memory: `（错误信息体是空的）、记忆「写了但查不到」、怀疑「队列被大批量任务占着所以一直耗着」。

## 1. 先否定两个直觉

- **工具报错 ≠ 数据丢失**。retain 的正常路径是「HTTP 入队 + 后台抽取」——客户端带 `retain_async=True` 时**毫秒返回**；只有**漏传这个标志**才退化成「同步等 worker 跑完」，而本机单批抽取 1300+ 秒，任何让同步等待超时的事（抽取模型慢、后端重启）都会让它报失败，而库里那条 op 其实已经建好了（可能是 `failed`，也可能稍后自己 `completed`）。**那句空错讯就是这个同步退化路径的形状**（工具层 `f"{failure}: {e}"` 拼裸 `TimeoutError`，`str(e)` 为空）——根因与修法（工具入口漏传标志 + 镜像层补丁脚本）见本 skill SKILL.md 的「记忆写入失败怎么查」节，先别去查数据。**判据只看两处**：`/operations` 里有没有这次写入的 op、`/documents` 的 `total` 与最新一条 `created_at` 有没有变。“它答对了”不算证据。
- **「一直耗着」通常不是量大**。实测主库 3000+ 条 op 里 `consolidate` 类型 **0 条**、failed 清一色是 `items_count=1` 的单条任务。**先按 `task_type × status` 聚合再下结论**，不要因为“慢”就假定有个批量任务在跑。
- **「队列不动」通常也不是卡死，而是单槽排队在慢慢推进**。抽取后端只有 1 个槽（llama.cpp `--parallel 1`），一条 retain 十几分钟、单次 LLM 调用 40~150s 都属正常水位；`pending` 十来条、`processing` 那条迟迟不完，看着像全队僵住。**判「慢」还是「死」的三个读数**：① `GET /operations` 的 `pending` **有没有在减少**（在减＝活的）② `GET /llm-requests?limit=3` 最新几条的 `status`/`duration_ms`（有新的 `success` ＝后端在出活）③ `failed` 计数（0 ＝不是“一直在失败”）。**只凭「有一条 op 停着不动」不要下「卡死」结论**——真僵尸要同时满足 §3 的 `updated_at` 停住判据。**同理也别把「某条 pending 消失了」当成泄洪证据**：worker 只有一个槽，前一条走完立刻换上后一条，在 2 条/小时的到达率下 pending 条数会原地不动甚至上涨。看 **pending 条数的趋势** + 最老那条 `created_at` 有没有被消化。
- **判「活没活」别看端口**：`ss -tln | grep 8888` 会命中同机 SearXNG 的 `18888` → 给出假「在监听」。判据是打 `/stats`：重建 hindsight 后 API 约 **30 秒**就绪（`RestartCount=0`、`ExitCode=0` 再核一眼，崩溃重启循环往往几分钟后才显形）。诊断期间**不要删/重试别人的 op**（重试只会去抢那个唯一的槽，让本来就在慢慢走的队列更像卡死）。

## 2. 三分钟体检（只读优先）

```bash
python3 scripts/ops_triage.py            # 本 skill 自带：队列聚合 + failed 清单 + 僵尸 + documents 计数
python3 scripts/ops_triage.py --retry <op_id>   # 显式重试（写操作）
```

裸查要点：

```bash
curl -s --noproxy '*' "http://127.0.0.1:8888/v1/default/banks/<bank>/operations?status=failed&limit=50"
curl -s --noproxy '*' "http://127.0.0.1:8888/v1/default/banks/<bank>/documents?limit=3"
```

- **容器内 curl 必须加 `--noproxy '*'`**（容器 env 里有 `http_proxy`，不加会被代理绕死）；Python 侧等价：`NO_PROXY=127.0.0.1,localhost,172.17.0.1` 环境变量，或 `urllib.request.build_opener(urllib.request.ProxyHandler({}))`。
- `/operations` 参数：`status`(failed/pending/processing/…)、`type`、`limit`（**上限 100，传 ≥200 返回 `{"detail":…}` 报错**）、`offset`；单条 `/operations/{id}`；`POST /operations/{id}/retry`；`DELETE /operations/{id}`（**对 `processing` 状态常被拒**，别指望它清僵尸）。
- 字段：`id / task_type / items_count / document_id / created_at / updated_at / status / error_message / retry_count / next_retry_at / progress`。
- **一次 retain 生成成对两条 op**：`retain`（子）+ `batch_retain`（父）→ 统计条数时别当两个独立任务。
- 别跟 psql 较劲：容器内嵌 Postgres 在 **5433**、数据在 `/home/hindsight/.pg0/instances/hindsight/data`，psql 在 `.pg0/installation/<ver>/bin/psql` 但**连接要口令**（不在 env 里）；REST API 够用。

## 3. 僵尸与错误分类

| 症状 | 含义 | 动作 |
|---|---|---|
| `processing` 且 `updated_at` 停在很久以前（配对的父 op 一直 `pending`） | worker 崩溃遗留的占用，不是真在跑 | `POST /operations/{id}/retry` 复活，然后盯 `updated_at` 是否走动 |
| `503 Loading model` | 抽取后端正在重载模型 | 等它日志出现 `model loaded` 再 retry——此刻 retry 必再失败 |
| `APIConnectionError: Connection error.` | 抽取容器正在停/重启，连接被掐断（同批调用会**扎堆在那一两分钟内**） | 不用修：判据是**之后还有成功的调用**（`/llm-requests` 出现新的 `success`）就行；与 `exceed_context_size`（ctx 硬超、必失败）、`JSONDecodeError`（输出被截断）完全是两类，别混着查 |
| `502 batch embeddings` | 嵌入链路不通（典型：截断代理的 `BACKEND_URL` 指了 `127.0.0.1`） | 修成 `172.17.0.1` 再 retry |
| `JSONDecodeError` | 8b 抽取输出被截断（与 consolidation 同根） | retry 一次；别开自动 consolidation |
| `exceeded max recovery attempts after crash` | 崩溃重试耗尽 | retry |
| `no tool-calling`（`refresh_mental_model`） | 该功能要求工具调用模型（当时的本地 8b 不支持）；`error_message` 以 `cancelled by ops:` 开头的往往是**人有意掐掉的** | 与 retain 无关，不要去“修”，也不要批量重跑（见 §6） |

## 4. 抽取后端是写入链路的硬约束

- 抽取用 LLM（`llama-extract`）的 `n_ctx` 已抬到 **32768**（历史 8192 → 16384 → 32768）；旧值下 Hindsight 会发出远超它的 prompt（日志原话：`send_error: request (54989 tokens) exceeds the available context size (8192 tokens)`）→ 那批抽取**必失败**，与数据、bank 配置都无关。抬 ctx 的显存代价实测约 **+0.6 GiB**（extract+embed 合计 ~9180 MiB、空 ~2.7G），改前先核 GPU 余量。
- **ctx 够不够的直接证据在 `/llm-requests`**：`-c 16384` 时一条 16691 token 的整合请求被 `exceed_context_size_error` 400 拒掉、那条 op 在单槽里卡了几十分钟；抬到 32768 后同类长 prompt 不再被拒，单次调用回到 **8 次里 7 成功**。判据是那个报错还出不出现，不是猜。
- `llama-extract` 崩溃/重启期间，所有在跑的抽取以 `503 Loading model` 失败 → **失败会成批出现**，看起来像“写入坏了”，其实是后端不可用（查 `docker inspect llama-extract --format '{{.RestartCount}}'` 与它的日志）。
- **排查顺序**：① 看 extract 容器日志有没有 `exceeds the available context size`（→ 上下文问题）② 看 ops 的 `error_message`（→ 后端问题）③ 最后才怀疑数据/配置。
- 修法（择一）：① 抬 `n_ctx`（本机已落地 32768，见上）；② 从 Hindsight 侧限制每批输入量（`consolidation_source_facts_max_tokens`、`reflect_max_context_tokens`）。两者都要先核 GPU 余量、把代价摆给主人拍板。

## 5. 复验口径

写入是否真的落库，用**数字**证明：记录操作前后的 `fact_count`（`GET /v1/default/banks`）与 `documents` 的 `total`，差值 >0 才算成功；单看某条 op 变 `completed` 不够（它可能在写 observation 阶段失败）。

## 6. 重跑失败任务：挑哪条、怎么串行

**先挑对 op。** 一次 retain 成对产生 `retain`（子）+ `batch_retain`（父）。**父 op 是聚合器**：单条查它可见 `result_metadata.is_parent = true`、`task_payload` 为空、不可 claim → `POST /operations/{id}/retry` 打在父上等于**空转**。要打的是**子 op**（`task_type=retain`）。列表接口可加 `exclude_parents=true` 直接滤掉父 op。

**别重跑「被运维主动取消」的 op。** `error_message` 以 `cancelled by ops:` 开头的是**人有意掐掉的**（当时该能力不可用、对应 cron 已关）——重跑等于替主人翻旧决定。同理 `refresh_mental_model` 报 `no tool-calling` 是**当时的模型能力问题**，不是坏数据。**这类先问，不要批量重跑。**

**必须串行，且等终态再发下一条。** 抽取后端是单槽（llama.cpp `--parallel 1`，Hindsight `WORKER_MAX_SLOTS=1`），而客户端并发度默认很高（`LLM_MAX_CONCURRENT` 32）→ 一口气推 N 条重跑只会让它们排队、单条 wall 拉长、poller 刷 `[STUCK?]`。姿势：**POST 一条 → 轮询 `GET /operations/{id}` 到 `completed`/`failed`/`cancelled` → 再发下一条**。

**重跑只是入队，活是服务端 worker 干的。** 所以本地那个「边轮询边等」的进程即使被中断（会话结束、进程被杀），**已入队那条照样跑完**——不必为了让重跑跑完而让本地进程常驻；要长跑就挂出去 + 建一次性 cron 稍后复查，别在前台盯着。

**重跑有副作用，不是无痕重放**：同 `document_id` 重抽会**重写该文档的既有事实**（`memory_unit_count`、文本长度都会变），全库 `fact_count` 有小幅波动（±百级）。跑前记下 `total_nodes`，跑完对差值——**别把正常波动报成数据丢失**。

**响应外形**：列表接口的数组键是 `operations`（**不是 `items`**）——只读 `total` 会看到 12 而数组为空，误判成「查不到」。单条详情里的键是 `result_metadata` / `task_payload` / `progress`。

现成脚本 `/opt/data/scripts/hs_retry_failed.py`：`--list` 只读列失败；`--retry` 自动跳过父 op 与不可重跑类、串行打并逐条等终态、把状态落 `state.json`；`--report` 事后复查（可挂 cron）。

## 7. 开「自动整合」前的前置条件（只翻开关必炸）

整合是**最占队列**的动作（抢唯一槽、还会自己一轮接一轮排），所以「打开它」不是一个布尔值，而是三项前置齐了才算：

1. **ctx 够大**：`llama-extract` 的 `-c` 要能吃下最长的 prompt（历史事故：tag-scope 的整合 prompt 涨到 10906 token、单条 16691 token，8192/16384 时被 400 拒）。先按 §4 确认。
2. **语法护栏开**：`HINDSIGHT_API_LLM_STRICT_SCHEMA_CONSOLIDATION=true` + `_STRICT_SCHEMA_REFLECT=true`（配合已有的 `_RETAIN`）。
3. **写进 compose**：`HINDSIGHT_API_ENABLE_AUTO_CONSOLIDATION=true` 必须落在 compose 里（只改线上容器 env 的话，下次重建静默打回 false）。

**验收三连**：`docker inspect <容器> --format '{{range .Config.Env}}{{println .}}{{end}}'` 看线上真值 → `grep` compose 看落盘 → 打 `/stats` + 看 `/llm-requests` 成功率。三项都过才算生效。⚠️ **改这些 env 要重建容器（真停机）**，且要**一次只加一项、加完盯几分钟**（曾把四项 env 一起加后服务在启动数分钟内崩溃重启）；拿不准的单项先用一次性探针容器验，规程见 `selfhosted-service-change-safety`。
