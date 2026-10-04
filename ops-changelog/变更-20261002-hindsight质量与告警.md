# 变更：Hindsight 质量开关 + 告警通道（2026-10-02 夜）

> 主人当晚逐条拍板：整合**要开**、其它功能"试着打开"、任何告警**走魔法推送**、测量类改完**设定时任务 1天/1周看效果**。
> 全部改动均**先备份后生效**，回滚方式逐条列出。

## 1. llama 栈：extract 上下文 16384 → 32768
- **改哪**：`/opt/data/llamacpp/docker-compose.yml`（`-c 32768`）→ 已重建生效（`/props` 实测 32768）
- **为什么**：整合 prompt 曾 16691 token 撞 16384 上限 → HTTP 400，是成规模的失败源；改后近 20 次 LLM 调用 7/7→20/20 成功
- **代价**：显存 8560→9180 MiB（空 2729 MiB）
- **回滚**：`cp tmp/docker-compose.yml.bak-before-persist-20261002`（同目录）→ `docker compose up -d`

## 2. Hindsight compose（三个备份，按时间）
- `ENABLE_AUTO_CONSOLIDATION=true`（原 false）+ `STRICT_SCHEMA_CONSOLIDATION/_REFLECT=true`（原本只开了 _RETAIN）
- `RERANKER_PROVIDER=rrf → local` **必须**同时加 `HF_HUB_OFFLINE=1` + `TRANSFORMERS_OFFLINE=1`
  （根因：本地缓存已有模型，但 transformers 启动时会联网列 HF repo；本机直连 huggingface.co:443 是黑洞 → `ConnectTimeout` → `Application startup failed`。实测 `hf-mirror.com` 0.1s 通、huggingface.co 超时）
- `RECALL_BUDGET_FUNCTION=fixed → adaptive`
- **备份**：`docker-compose.yml.bak-before-toggles-20261002` / `.bak-before-step1-20261002` / `.bak-before-rerank-live-20261002`（崩溃现场另存 `.crashed-20261002`）
- **回滚**：换回任一 bak → `docker compose up -d`；只想关重排器 = 换回 `.bak-before-rerank-live-20261002`
- **已验证**：`Reranking [cross-encoder]: N candidates scored in 0.961s`（>0 = 真在算分）；`RestartCount` 稳定 0
- **内存实测（2026-10-02，可复现：`docker stats` + `/proc/<pid>/status`+`maps`）**：`hindsight-api` 进程 RSS **940 MiB**（含模型权重 88 MiB、`libtorch_cpu.so` 443 MiB mapped）；整个 hindsight 容器 **1.35 GiB**（宿主 8.7%），**无内存上限**；重排器每次召回多耗 0.4–1s CPU、不占 GPU。**关掉重排器的 A/B 对比未做**——主人 2026-10-02 定「基本上无伤大雅」，不做
- ⚠️ recreate 时有一次**一次性竞态**：内嵌 PG 未就绪 → `psycopg2.OperationalError 127.0.0.1:5433 Connection refused` → 退出一次，被 restart policy 拉起后正常。非 HF 问题

## 3. 告警通道：`health_all.py`（两个备份）
- **主通道改 magicpush**：`POST http://172.17.0.1:818/api/push/<token>`（endpoint id=6「通用推送」→ bark→iPhone），失败回落 Hermes `hermes send`
  - token 存 `secrets/magicpush.token`（700，仅 hermes 可读），**不落脚本/技能明文**
  - 实测：`HTTP 200 successCount=1`；演练 `--simulate-fail` 走主通道通过
  - ⚠️ 曾经的 `:8098` 是退役的 AstrBot 插件 `hermes_report`，与 magicpush 无关
- **新增检查项** `llama 栈参数漂移（compose vs 线上）`：逐项比对 `-c`/`-ngl`/`--cache-ram`/`mem_limit`/`restart`，不一致报❌（今天因"参数只在手工 run 里、compose 没有"被咬过）
- **备份**：`health_all.py.bak-before-magicpush-20261002` / `.bak-before-driftcheck-20261002`

## 4. 新增文件
- `scripts/hindsight_judge.py`：给"脚本判不了的模糊问题"调**本地 API**出第二意见（规则判定为主）。三护栏写进代码：短提示+`max_tokens=120`／默认超时 600s／**排队超时不算失败**（输出「判定不明」、退出码恒 0）
- `secrets/magicpush.token`、`secrets/magicpush.endpoint`（700）
- `~/.ssh/config`：`ControlMaster/ControlPersist 30m` 连接复用 —— 治"健康自检每 30 分钟开一堆 ssh，导致 fnOS 狂推 SSH 登录/断开通知"（改前 19点31条/20点50条 → 改后实测零新增）
- `reports/2026-10-03-hindsight-queue-and-refresh-analysis.md`：队列/心智模型口径与根因分析
- `skills/.../hindsight-memory-engine/references/offline-hf-and-crash-probe.md`：HF 联网陷阱 + 探针容器隔离法

## 5. 定时任务
- 新增 `c4c8f09ea5d9`「hindsight 记忆质量回看（1天/1周）」：每天 21:00，`script=hindsight_judge.py` + agent 汇总，异常才推魔法推送；阶段判据（>100 属预期 / ≤100 才告警）

## 6. 仍属"预期、不要当异常"的现场（主人已确认）
- 单槽被长 retain/consolidation 占用、retain 排队变慢、extract 探针要等 → 积压未清完前正常
- `pending_consolidation` 会因持续落库而平/微涨（口径 ≠ `pending` op 数 ≠ `batch_retain` 父聚合器）
- 心智模型 `last_refreshed_at` 停在 09-21：`refresh_cron=null` + 每轮撞 100 上限 → 积压 ≤100 时自愈
