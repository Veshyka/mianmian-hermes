---
name: local-llm-ops
description: Use when 本地 LLM/Ollama/llama.cpp 选型与运维（显存内存、看图、抽取）或算云端 API 成本。
---

# 本地 LLM 模型选型与 Ollama 运维

宿主环境：飞牛 fnOS + RTX 3060 12G + 15G 内存，Ollama 0.30.7（应用中心安装，路径 `/vol2/@appcenter/ai_installer/ollama/bin/ollama`）。

## 核心原则：多模态模型（VLM）不做结构化抽取 ⚠️

**8/25 实测定案**：VLM（minicpm-v4.5）在 Hindsight 长 prompt 下 `causal_relations` 恒为 0——短 prompt 能提因果（7-9 边），完整管线长 prompt 直接忽略该字段。RETAIN_MISSION 注入 / 因果段中文版 / custom mode 三种 prompt 增强**全部无效**。换回纯文本 qwen3:8b 后因果 5/5 恢复。

**原因**（架构 + 训练）：VLM = 视觉塔 + 投影层 + 语言主干，参数被视觉部分分走；训练重心在多模态对齐，结构化输出（复杂 schema + 长 prompt）稳定性差。这是普遍规律（网上共识：多模态抽取「能力有但稳定性是另一回事」）。

**选型表**：

| 任务 | 用 | 不用 |
|---|---|---|
| 抽取/入库/结构化 JSON | 纯文本指令模型（qwen3:8b） | VLM |
| 看图/多模态理解 | VLM（minicpm-v4.5） | 文本模型 |
| 多轮工具调用 / agent 循环（curator 技能审查、reflect agent） | 云端主模型 | 本地 8b——不产 tool call（**这条只对 8b 那一代成立**；Bonsai 27B 实测能产并行 tool call，见下） |

**为什么本地 8b 不做 agent 循环**：这类活是几十到上百次带参数的工具调用 + 上下文滚到几十 k，跟 8b 的长处（短 prompt + 结构化输出）正相反。两个本机实证：① reflect agent（tool-calling）用 qwen3:8b 不产 tool call → 到点卡死占满唯一 slot（诊断配方见「已知陷阱」）；② Hindsight consolidation 用 qwen3:8b 持续 JSONDecodeError 死循环，换云端立刻过——而那还是**比 agent 循环更简单**的结构化合并。辅助任务要挂模型用 `auxiliary.<task>.{provider,model}`，但**别把 curator 这类审查挂上去**。

**2026-09-21 更新：Bonsai 27B 能产 tool call（实测）**——同实例（8081，alias `bonsai2-27b`）带两个 tool 定义发一次请求，返回**两个并行 tool_calls**（`get_weather({"city":"兰州"})` + `search_memory({"query":"..."})`），参数提取准确、4.7s、55 token。所以「本地模型不产 tool call」这个前提已随模型换代失效：历史上因此废掉的 reflect agent 循环、mental model 自动刷新、consolidation 三条路都值得重估。⚠️ 但能产 call ≠ 能跑长 agent 循环：`-c 8192` + decode 25 t/s 仍是硬约束，几十轮 tool 循环照样跑不动（聊天主对话同理：Hermes 一轮的 system prompt + 工具 schema + Hindsight 召回轻松过 1.5 万 token，远超 8192，且与 retain 抢同一个 `--parallel 1` 槽）。

**引用旧结论前，先确认它的前提还在不在**：模型换代会把成批旧结论一次性作废，而旧结论常以「某模型不能做 X」的形式散落在选型表、坑列表和记忆里，下个会话读到会当现行事实。所以：① 写「不能做 X」时**必须带上哪一代**（8b ≠ Bonsai 27B），否则它迟早误导人；② 凡以旧前提为理由拒绝做某事（不重跑、不启用、不修复），先复述前提再看它是否已变，**变了就重做决定**，不要拿旧结论当挡箭牌。本机典型：「本地模型不产 tool call」当年直接废掉 reflect agent 循环与 mental model 自动刷新两条路，模型换代后这两条都该重估。

**把「看图」这活整体交给本地模型（不让云端主模型碰图片）是纯配置能做的**——`agent.image_input_mode` / `auxiliary.vision` 两个开关，PDF 天然不走图像通道；键名、判定逻辑和「这条路径有损」的代价见 `references/hermes-image-routing.md`。

**⚠️ 让本地 VLM 做「分类/打标」这类小任务，问法决定成败（2026-10-04 实测）**：同一批 4 张 QQ 表情包，直接问结论（「这是什么情绪」）会**偷懒默认**——两张完全不同的图都答「开心」；改成**先描述画面再定标签 + 给判据**（「眯眼/挡眼/托腮→无语；问号脸→疑惑；跪地→累了」）后，4 张里 3 张与独立读法一致。规律：**小模型要先写出「看到了什么」再收敛到受控词表**；只给一个词表要答案，它就挑最常见的那个。这类任务只花 9~10 个输出 token——**贵的是排队不是 token**（同实例 4 条实测 3s/4s/47s/109s，看卡忙不忙），所以入库类打标一律做成后台队列 + 每条之间留间隔，别跟 Hindsight 抽取抢 `--parallel 1` 的槽。落地实现见 `profiles/chat/plugins/onebot/sticker_labeler.py`。

⚠️ **小 VLM 的绝对精度确实差（实测定案）**：12G 卡上的 1B 级 VLM（minicpm-v4.6-1b）在**数字密集表 CER 62.8%、UI 截图 30.5%、小字 40.2%**，且错的是「平滑错值」（`123 GB`→`132 GB`、`/dev/nvme1n1`→`devmne1`、时间戳变成别的年份）——下游无法自检；同一批图上主模型视觉逐行读对。**先跑量尺再谈用途**，方法与场景清单见 `references/vlm-vision-quality-bench.md`（含 `scripts/vlm_page_bench.py`）。

**但主人已拍板：默认看图就走本地小模型**（随手发照片/表情包不出云端、省 token），精度需求靠**当场点名 OCR** 来兜（`scanned-pdf-ocr` 技能；同一张数字密集表实测 MinerU 数字单元格 100% vs 本地 1B 89.7%）。所以 `auxiliary.vision` 指向本地实例是**现行状态，不是待议方案也不是 bug**；2026-09-21 起该端点已从 8083（minicpm-v4.6-1b）改为 **8081（Bonsai 27B + mmproj）**，8083 停用（键值、验证代码、回滚步骤见 `references/hermes-image-routing.md`）。自己遇到「图里要引用数字/表格/单号」的图，**主动提出走 OCR，不要引用本地描述里的数值**。

**已验证候选**（详细数据见 `references/model-selection-tests.md`）：
- qwen3:8b = 抽取主力（因果 5/5、格式稳、~50s/条、与 embedding 4b 同驻 9.8G 显存 OK）
- qwen2.5:14b = 粒度更细（9 事实/8 因果边）但权重 9G + embedding 3.9G > 12G，需 `OLLAMA_MAX_LOADED_MODELS=1` 互斥切换（recall 实测 11s 可接受）——主人拍板保持 8b
- qwen3.5:9b = 弃（真实多事实输出编号列表非 JSON）
- ornith-1.5:9b = **弃（9/7 深度实锤，qwen3.5 架构 thinking 失控）**：6.55GB VLM（qwen3.5 推理架构 + projector 456M），单条 test_extract 短测通过（9 事实/8 因果边/91s）**但真实 Hindsight 长 prompt 下 thinking 永不收敛，把生成预算全烧成思考，产出 0 字节**（eval 5248 tok 全思考、content 空、done=length）；8192/16384 ctx、think on/off 全部复现。**关键坑：ollama 的 `think:false` 对 qwen3.5 架构无效**（flag 被无视，照样 thinking 反而更糟）。教训再证：**单条短测试全绿 ≠ 能用于管线，新模型必须过真实长 prompt 才谈替换**。qwen3-vl 默认 tag 同理是 thinking 版（`:latest`/`:8b`/`:8b-thinking` 同 digest），抽取/长 prompt 必须用 `qwen3-vl:8b-instruct`
- minicpm-v4.5 = 仅看图（抽取因果恒 0）
- 32B 级（Q4 20G）12G 卡完全放不下
- **Ternary Bonsai 2 27B（三值量化）实测结论：不换**（2026-09-21，同机 A/B 6 条真实 Hindsight 请求）——官方 llama.cpp 认不出 `PTQ1_0`（`invalid ggml type 143`），只能用 PrismML fork 二进制（塞进现有 CUDA 镜像即可跑）；显存 6374 MiB、decode 25 t/s（8B 是 45 t/s），默认 prompt 下抽取 JSON 通过率 **没有更好**（更啰嗦，8k ctx 下截断成 `Unterminated string`）——**但收紧 prompt 的对照实验把这条翻过来了**（Bonsai 6/6、输出 token 2172→1416、截断消失；8B 收紧后同样 6/6），所以真卡点是提示词不是模型；最终不换的理由落在速度 1.8×、显存 +850 MiB、必须长期挂非官方 fork 上。质量面（原子度/因果链/主体归一化）Bonsai 确实更好。数据、复现命令与下载/并发坑见 `references/ternary-bonsai2-27b-eval.md`
- **生态现状调研（2026-09-21，一手源）：fork 短期无望退役，接受长期挂着。** 上游 `ggml-org/llama.cpp` master 类型表到 `GGML_TYPE_COUNT=43` 为止，`PTQ1_0(143)/PQ2_0(142)` 落在范围外（正是本机 `should be in [0, 43)` 报错的出处）；官方 issue #29058 与 PR #29077（含参考实现）均开着未合并，维护者要求「留给 PrismML 自己提交」，而 PrismML 方回复目标是用官方 Q2_0 + hadamard，PTQ1_0/PQ2_0「might stay in fork only since adding new types is increased maintenance work」——无时间表。官方文档也直写 `Do not run Ternary Bonsai 2 on stock llama.cpp`。另：官方 Q2_0（type 42）**已**合入上游（PR #24448, 2026-07-07），但 Bonsai 2 的 Q2_0 文件在上游能加载却输出乱码（缺 hadamard 折叠），dev 仓那份 `Q2_0-prism-fork-required.gguf` 文件名就写明还得配 fork。**本机用的 `prism-b10709-9a9394a` 就是当前最新 release**（2026-09-18，主要是发布流水线修复 + DSpark 投机解码运行时），别白折腾升级。
- **官方推荐参数 vs 本机选择**：model card 给 `-ngl 99 -fa on -c 32768 --temp 1.0 --top-p 0.95 --top-k 20`（27B 默认开 thinking，可 `--reasoning-budget 0`）。本机生产跑 `-c 8192 --temp 0.1 --reasoning off`——**低温是有意的**（抽取要格式稳定，实测 0.1 下 6/6 JSON 合法），不要照抄 card 的 temp 1.0。真正该调的是 `-c`，见下条。
- **KV cache 显存算法（官方文档 + 本机实测吻合）**：Ternary Bonsai 2 27B 每 token KV 约 **64 KiB**（q8_0 KV）。⚠️ **这个数字绑定在带 `--cache-type-k/v q8_0` 的配置上，而现行生产不是**：2026-09-21 切换 Bonsai 时那条参数被去掉（compose 注释：「按实测配置落地」），运行中的 cmdline 已无 `--cache-type`，即 llama.cpp 默认 **f16**（KV 字节数会按量化类型成倍变）。所以**凡拿这个数算显存前先确认当前 KV 类型**——判据是运行中 cmdline 有没有 `--cache-type-k/v`（`ps -eo args` 就能看到，不需要 docker 权限），别直接套 64 KiB。**现行生产已把 q8_0 KV 加回来**（`--cache-type-k/-v q8_0`，ctx 8192 不变），但**实测只省 240 MiB**（extract 7226→6986 MiB）——远低于「KV 2 GiB→1 GiB」的算术预期，而与差值法上界（8192 时 KV ≤ ~0.9 GiB）自洽：**KV 量化与 ctx 的显存收益一律以 `nvidia-smi` 前后差值为准，元数据手算只能当假设**。（同批实测的反例：embed 降 `-c 8192→2048` 省了 872 MiB，因为那实例的 KV 本来就占大头——两个实例同一招收益差 3 倍多，别互推。）**读运行中的实际参数不需要 docker 权限**：飞牛的容器进程在宿主 `ps` 可见，`ps -eo args | grep <二进制名>` 就能拿到真的在跑的 `-c` / `--parallel` / `--cache-type-*` / `--mmproj`（清单、资源用官方通道 `python3 scripts/trim_cli.py docker container ls|stats`）。**KV 字节数别按元数据手算**：拿 GGUF 元数据算（层数 × KV 头数 × head dim × 字节数）在本机**对不上实测**——算出 256 KiB/token，而 extract 总占用扣掉权重与 mmproj 后只剩 ~0.9 GiB，两者不可能同时成立（错在 `head_dim` 取法：`key_length/n_head` 不成立）。⚠️ **别去翻日志找 `KV self size`：这个 fork 的 llama-server 根本不打印那行**（`--verbosity 3` 也没有），「读服务端日志」这条路走不通。可靠判据是**差值法**：容器总显存 − 权重 − mmproj = KV + 计算缓冲 + CUDA ctx 的**上界**。实测 Bonsai 27B：7226 − 5671 − 600 = **955 MiB** ⇒ KV(8192) ≤ ~119 KiB/token，**故 64 KiB/token 这档成立**，下面按它的估算可用。`scripts/gguf_meta.py` 只用来读架构事实（层数、KV 头数、原生 ctx、量化类型）。以下按 64 KiB／token 计 → 8192 ctx ≈ 512 MiB、16384 ≈ 1 GiB、32768 ≈ 2 GiB；与实测「8192→32768 增加 1560 MiB」对得上。据此算 ctx 上限：extract 现占 7250 MiB（含 mmproj）+ embed 3974 ≈ 11224/12288 → **提到 16384 只多 ~512 MiB（总 ~11744，可行）**；**提到 32768 需 +1.5 GiB（总 ~12724，超）**，除非把投影器挪 CPU（llama.cpp 的 `--no-mmproj-offload`，官方 env `BONSAI_MMPROJ_CPU=1`）省回显存。
   - **⚠️ 上面这个「32768 超了」的结论已随 embed 让位而过时（现行生产就是 32768）**：embed 改 `-ngl 8` 腾出 1.8G 之后，`-c 32768` **放得下且已上生产**——实测只比 16384 多 **~620 MiB**（extract 7.3G→9.2G 量级，整卡占用 9180 / 空闲 2729 MiB，验收线 free ≥ 300 MiB 稳过），重建后 `/props` 读回 `n_ctx=32768`、`total_slots=1`。**每次算 ctx 上限都要用「当前」两个实例的占用重算，别套旧账**（这个数被改动过两次：KV 量化、embed 降 `-ngl`/降 `-c` 各变一次）。
   - **但 ctx 变大不是免费的午餐（延迟换能力）**：32k 下一轮长抽取能跑几十到几百秒，后端 `--parallel 1` 被它独占，**同时段内连 1-token 探针都要排队**（实测排队 >90s 才返回）——这是排队不是后端死，判据是看抽取在不在推进（`llm-requests` 是否仍出 `success`），别据此重启容器。**省多少取决于用哪份 mmproj**：现行 Bonsai 的 mmproj 只有 ~601 MiB，所以现在只省 ~0.6 GiB；「~0.9 GiB」那个数是**已退役的 minicpm mmproj（1057 MiB）**，别照搬。代价（单次请求 0.4~1.1s → 5~7s）也是在那份上量的，**Bonsai 挪 CPU 后多慢未实测**。
- **想开 `--parallel` 之前必须先算账（本机刻意是 1）**：`--ctx-size` 是**所有槽共用的总量**，每槽实得 `n_ctx_per_seq = -c / --parallel`（官方 issue #11681；本机启动日志同款字段 `n_slots = 1, n_ctx_slot = 8192`）——开并行等于**按并发数切走每个请求的上下文**，不是白捡吞吐。所以先量「真实请求有多大」再定并发数：`GET /v1/default/banks/<bank>/llm-requests?limit=300` 读 `input_tokens`（本机 retain 实测：中位 3821／p95 4771／max 5348，**33% 超过 4096**）→ `-c 8192 --parallel 2` 每槽只剩 4096，**三分之一请求直接放不下**；要并行又不掉能力，`-c` 至少 **12288**（每槽 6144，全部装得下）。按上面 64 KiB/token 口径，`-c 12288` 的 KV 是 768 MiB（比现状 +256 MiB），落在「extract 7250 + embed 3974、剩 ~695 MiB」的账上约剩 440 MiB——**账面刚够、未实测**（每槽 compute buffer 另算；本机验收线是 free ≥ 300 MiB）。取数：逐进程显存 `nvidia-smi --query-compute-apps=pid,process_name,used_memory`，`-c`／槽数 `/props` 的 `n_ctx`／`total_slots`。
- **`--parallel 1` 是本机刻意调的，不是默认残留**：compose 注释原话「单请求拿满 8192 上下文；KV 不再按 4 slot 白吃显存」——是从并行 4 退回来的，改并行等于把这条推翻，先读那段注释再动手。而且**并行对 Hindsight retain 基本换不到收益**：worker 单槽（`WORKER_MAX_SLOTS=1`）+ retain 文档基本 1 chunk，并发 POST 只是把请求堆进队列，GPU 吞吐不变（算力打满时并行只改变交错顺序，不增加总算力）。要提吞吐得先腾显存（最大可腾项是 embed 那 3974 MiB：4B embedding 权重才 ~2.5 G，其余是 `-c 8192` 的 KV+buffer，挪 CPU 或降 `-c` 都能腾），调 `--parallel` 不是那个旋钮。**这步已经做了**：按实测请求分布（embedding 请求最长仅 356 token，典型 85~95）把 embed 的 `-c 8192→2048` → 显存 3974→3100 MiB（省 872 MiB，仍留 5.7 倍余量），GPU 空闲 697 → 1809 MiB。**降 `-c` 之前先量真实请求分布**（`docker logs <容器> | grep -oE "n_tokens = *[0-9]+"`）：embed 能砍、extract 不能（单次 retain 实测 6488 token，砍到 6144 就开始截断抽取输入、漏抽事实，下限被 `Hindsight` 的 reflect 预算顶住）——**同一个参数在两个实例上结论相反，别照抄**。

- **2026-09-21 后续：Bonsai 已上生产（推翻上面「不换」）**——加上 mmproj 后四条件实测全过，主人授权下已切换：`llama-extract`(8081) 现在跑 Bonsai 27B PTQ1_0 + mmproj（fork 二进制在稳定路径 `llamacpp/fork-prism-b10709/`，`mem_limit 10g`，`--alias` 同日规范化为 **`bonsai2-27b`**——Hindsight 的 `HINDSIGHT_API_LLM_MODEL` 与 Hermes 的 `auxiliary.vision.model` 已同步改掉；旧名 `qwen3:8b` 会让排查误判成 8B 还在跑），**llama-vision(8083) 已停用并退役清理**（compose 里的 vision 段删除、`vision-on.sh` 与两份 minicpm 模型文件进回收站；`auxiliary.vision` 现指 8081），一个实例同时顶掉抽取与看图；实测 Bonsai 7250 MiB + embed 3974 MiB，GPU 剩 ~670 MiB，decode 24~29 t/s，端到端 retain 已验真。回滚分层：切 Bonsai 前的 `llamacpp/docker-compose.yml.bak-before-bonsai-*`、删 vision 段前的 `...bak-before-vision-cleanup-*`，都在同目录。**minicpm 两个模型文件与 `vision-on.sh` 已不在盘上**（回收站 7 天内可 `trash-restore`），别再去找它们。
- **Bonsai 看图的能力边界（自测）**：纯中文段落逐字准确（5/5 行，含「¥ 1,280.00」「2026-09-21」这类数字）；但**会把长字母数字编号整体漏读**（实测一张含 `HX-7742`／`BQ-9184-C`／`8F3A-Q7` 的检修单，中文正文全对、三个编号全空）——是漏读不是编造。规则不变：图里要引用单号/表格数值时走 OCR（`scanned-pdf-ocr`），不要引用本地 VLM 的描述。
- **改本地看图路由后必须查日志确认，不能只看 `vision_analyze` 成功**：`auxiliary.vision` 指向的端点连不上时 Hermes **静默**回退到云端主模型（agent.log 出现 `Auxiliary vision: connection error on custom — falling back to main agent model`），工具照样返回正确答案，但看图其实已经上云、耗云端 token。判断本地路由是否真生效的唯一硬证据是这行日志。
- **cron 会话里改不了 `config.yaml`**：安全策略把「overwrite project env/config file」拦掉（`approvals.cron_mode` 未开 approve），所以定时任务里做不了「改看图路由 + 重启网关」这类收尾，得留给交互式会话或让主人执行。

## Ollama 飞牛 CUDA wrapper（内存优化核心）

**根因**：飞牛应用中心启动 Ollama 强制 `OLLAMA_VULKAN=1`（Vulkan 后端权重不能 mmap 进显存，全堆 RAM——minicpm RSS 8.2G）。CUDA 库实际齐全（`lib/ollama/cuda_v12|v13`）。

**方案**：wrapper 覆盖 `ollama` 二进制（原文件备份 `ollama.real`）：
```bash
#!/bin/bash
export OLLAMA_VULKAN=0
export OLLAMA_CUDA=1
export OLLAMA_KV_CACHE_TYPE=q8_0
exec /vol2/@appcenter/ai_installer/ollama/bin/ollama.real "$@"
```
效果：minicpm RSS 8.2G→1.9G，宿主 available 3G→7G+。脚本在 `/opt/data/scripts/ollama-cuda-wrapper.sh`。

⚠️ **应用中心更新 Ollama 会覆盖 wrapper**——更新后需重做（主人已授权找棉棉重做）。

**回滚**：`mv ollama ollama.wrapper.bak && mv ollama.real ollama` + 重启。

## 抽取质量验证方法（因果/格式/语义）

1. **单条对比测试**：`scripts/test_extract.py <model>` ——同一因果链文本，输出事实数/因果边/实体/JSON 完整性，多模型公平对比
2. **真实管线验证（最重要，单条测试会误判）**：
   - `POST /memories/dry-run-extract` body `{"content":"...","retain_extract_causal_links":true}`（不落库，看格式）
   - 真实验证看容器日志：`docker logs hindsight | grep -E "Extract facts|Causal links|parse error"`——Causal links > 0 才算真恢复
   - **模型评估直接构造贴近 Hindsight 的真实长 prompt 测**（带主体归一化说明 + 完整 extract schema），单条短 test_extract 全绿不能采信（minicpm、ornith-1.5:9b 均短测通过、长 prompt 现形）；评估时模型把主体自作主张归一化（小林→<OWNER>）也算不合格——抽取任务要求忠实原文不擅改
3. **Hindsight 因果机制**：`extract_causal_links` 默认 True（env `HINDSIGHT_API_RETAIN_EXTRACT_CAUSAL_LINKS`）；fact 的 `causal_relations` 只能指向更早事实（target_fact_index），relation_type 只能是 `caused_by`；因果存在 memory_links 层，graph API 只暴露 semantic 边（别用 graph 判断因果有无）
4. **判模型死刑前先做「收紧 prompt」的对照实验**：模型的失败经常是**提示词没管住**，不是能力不足——不做这一步就会把提示词问题记成型能问题，选型结论直接反掉（实测：某 27B 默认 prompt 6 条里挂 1 条截断，收紧后 6/6 且输出 token 降 35%，否决理由只剩速度/显存/fork）。做法：写一份硬约束追加文件（强制输出语言、每条事实字数上限、事实条数上限、字段取值域、只输出 JSON），用同一脚本的 `--extra-system <文件>` 对**同一批真实请求**分别打两个模型跑 A/B，再比 JSON 合法率 / 截断率 / 输出 token。模板与数据见 `references/ternary-bonsai2-27b-eval.md`。

5. **推理参数矩阵（温度，2026-09-21 已实测扫完；思考 on/off 仍未扫）**：测试脚本里温度写死是直觉非验证。**已扫结论**（Bonsai 27B @8081，6 条真实请求 + 收紧 prompt，温度 0.1/0.5/0.7/1.0）：根形状合规 6/6、6/6、6/6、**3/6**；因果边（合法）7、**11**、15、4；输出 token 均 1304、1353、1492、1207。**0.5 是甜点**（合规与 0.1 打平、因果边 +57%、token 仅 +3.8%）；0.7 拿空 `what` 脏事实换边数；**1.0 是断崖不是趋势**——模型丢掉 `{"facts":[...]}` 外壳直接吐裸数组。四条必须遵守的规则：

   - **温度是调用方给的，不在 llama-server**：请求体里的 `temperature` 覆盖 `--temp`，只改服务端启动参数**不生效**，要改的是调用方（Hindsight 侧 `DEFAULT_LLM_TEMPERATURE_RETAIN` / retain 调用上的覆盖）。实测真实 Hindsight 请求体顶层就是 `{"temperature":0.1,"response_format":{"type":"json_object"}}`。
   - **合规率必须分三层量，只算 `json.loads` 成功会漏判**：① 严格 `json.loads`（不剥 ``` 围栏）② 根形状＝对象且含必需外壳键（够 pydantic 过）③ 字段层零违规。高温的典型失效是②崩而①过（裸数组、markdown 围栏），**只报①会把「整条被父侧拒收、该 chunk 记忆丢失」算成通过**。
   - **因果边不能只数条数，要看指向分布**：边全部 `target_index=0` 是刷边伪链（qwen3-8b 收紧组出现过 7 条全指 0）；真链＝指向直接前驱 + 跨度 2–3.4 的多跳。同时查 `what` 是否重复（重复 = 拆条灌水，事实数虚增）。
   - **模型 card 推荐的 temp（如 1.0）是对话调参，不能搬到严格 schema 抽取**；矩阵要**串行**跑（生产 `--parallel 1`）、每条**落盘**、整批走**后台任务再轮询**（前台单次等待会在长任务上被截断），每个温度开跑前先 `/health`。

6. **改服务端 env 开关后怎么证明它真生效（env 读过 ≠ 生效）**：三步，缺一不可 —— ① `docker inspect <容器>` 确认变量进了容器；② **grep 容器里读它的那行代码**（`docker exec <容器> grep -rn <FLAG> /app`）确认消费方真的读它、以及读的是哪个层级；③ 读回目标（追踪记录/日志/真实请求）确认**行为**变了。本机 Hindsight 的易错点：它有一族**按操作细分**的开关（`_RETAIN`/`_REFLECT`/`_CONSOLIDATION`），`config.py` 的解析顺序是**操作级 env → 全局 env → 默认**，设错层级会静默走默认；而且 `llm_info.request` **不记 wire 层字段**（只记 `response_schema` 这个名字），拿它证不了 `response_format` 到底是什么——想证就走源码或做行为判据。已达成的状态：`HINDSIGHT_API_LLM_STRICT_SCHEMA_RETAIN=true` 已在生产启用，机制、判据与代价见 `references/ternary-bonsai2-27b-eval.md` §8。

   数据、边质量分析与并发现场校验见 `references/ternary-bonsai2-27b-eval.md` §7

## 模型命名解码与生态边界（选型前先看名字，省一次白下载）

**选型研究先搜新鲜的，再下结论**：模型迭代快，榜单分数、原生向量维度、新模型半年就变——查选型资料用 SearXNG 带时间范围（`http://172.17.0.1:18888/search?q=...&format=json&time_range=year`，也支持 `month`），别只凭训练记忆和旧笔记。**二手博客的数字经常错**（维度、榜单分、是否支持 MRL 都可能抄错），结论以官方 model card / 官方文档为准；检索到 LLM 的版本发布别当成 embedding 模型也发了新版——两条线节奏不同。

- **`-mlx` 后缀 = Apple Silicon 专用**（Apple MLX 框架），NVIDIA/CUDA 机器上跑不了——选型直接跳过。
- **Gemma `E2B`/`E4B` 的 E = effective（有效参数），不是总参数**：E2B 有效 2.3B / 含逐层 embedding 总 5.1B；E4B 有效 4.5B / 总 8B。**权重体积按总参数算**（gemma4:e2b 实际 7.2GB），所以「等效 2B」远重于真 2B dense（qwen3-vl:2b ≈1.9GB），12G 卡上不如直接上 12B（7.6GB）划算。
- **`26B-A4B` 的 A = active**：MoE，每 token 只激活 4B，但**全部 26B 权重都要载入显存**——显存需求接近 dense 26B，不是 4B 模型。
- **`medgemma` = Google 医疗垂直微调**（胸片/皮肤/眼科/病理 + 医疗文本），非医疗用途反而不如通用模型，跳过。
- **Ollama 只做理解（LLM/VLM/embedding），不做生成**：生图/生视频是另一套引擎（ComfyUI / SD WebUI / Diffusers），跟 ollama 无关；ComfyUI 是节点式工作流画布（不对话），自带 API 可被 agent 调用。
- ollama 官方库覆盖不到的无审查/新模型、要绕开 ollama 调度层（mmap 误判、GPU discovery、think flag）的替代引擎，见 `references/model-ecosystem-beyond-ollama.md`。

## 下载与搬运大模型的三个坑（实测）

- **宿主 /vol2 的文件大小与 sha256 要等写回完成再看**：`curl`/`hf` 写完（进程已退出、脚本已打完 sha）之后，`stat`/`sha256sum` 仍可能看到**中间态**——实测同一文件 size 缓慢爬升、sha 与 HF 官方 etag 不符；判据是**每 10s 取两次 size，稳定后再 `sha256sum`**。`x-linked-etag`（`curl -sI <resolve url>`）就是权威 sha256 校验值。容器内 `/opt/data` 与宿主视图会短暂不一致，别据此断定文件坏了。
- **大模型走「分片下载 + 逐片长度校验」**：本机代理会中途掐 HTTP/2 长流（`curl: (18) stream not closed cleanly`），单流下载会静默截断，`curl -C -` 续传又会把整份文件追加成 6~7GB 的坏文件（以 HF 报的 content-length 为准）。稳的做法：按 512MiB 显式 Range 下到 `.parts/part.NNN`，**每片必须恰好等于请求长度**才保留（不等就丢弃重试），全部齐了再 `cat` 成正式文件。模板 `/opt/data/tmp/bonsai/dl5.sh`。
- **别用 `pkill -f <关键词>` 收自己的进程**：承载命令的外层 shell 命令行里就含那个关键词，`pkill`/`pgrep`+`kill` 会把自己一起杀掉（实测连中三次，一次还杀掉正在跑的任务）。只杀目标：`ps -eo pid,args | grep "[关]键词"` 拿 PID 再 `kill -9 <pid>`，或按 `/proc/PID/cmdline` 首 token 区分（`/usr/bin/bash -lic` vs `/bin/bash <脚本>`）。**同一个坑在「查」的场合也会咬人**：`pgrep -f <关键词>` 取进程信息时，承载这条命令的 bash 自己就在匹配集里（远端一条命令里尤其明显，真进程的输出会被挤出 `tail` 窗口，看着像进程根本不存在）。查进程一律用 **`pgrep -x <二进制名>`**（精确匹配进程名，如 `pgrep -x llama-server`）或 `ps -eo pid,rss,comm --sort=-rss`。**最稳的是按二进制路径杀**：遍历 `/proc/*/exe` 只杀 `readlink` 指向目标二进制的 pid——`ps … | grep "[关]键词" | awk | kill` 这种写法里，**关键词本身就写在承载命令的 bash 命令行上**，`[l]` 括号技巧救不了，循环会把自己的 shell 一起 `kill -9`。

## 候选模型真伪核实与 CPU 基准（主人贴来小模型清单时先看这条）

核实存在性 → 量判断力 → 才谈选型。完整配方（HF/GitHub 镜像 API、llama.cpp 预编译二进制、报告口径）在 `references/model-vetting-and-cpu-bench.md`。永远是这几条：

- **HF 直连不通就走 `hf-mirror.com`**（`/api/models?search=`、`/api/models/<repo>?blobs=true` 带文件体积、`/<repo>/raw/main/README.md`）；GitHub 用 `api.github.com`（直连可用），仓正文走 `/repos/<o>/<r>/readme` + base64，不靠 `raw.githubusercontent.com`。**镜像能拿到就不许说「查不到」**。
- **模型真 ≠ 卡片上的数字真**：清单里模型多半能查到，但伴随的 benchmark 数字常查无出处。无法溯源的写「查不到可信来源」，并**单列搜过的平台与关键词**；绝不编造下载量/延迟。找到出处也要看样本量（自述「86.4%」实际只有 22 条回归样本 = 作者自测，不是独立验证）。
- **服从度与判断力分两栏报**，且服从度要分「严格只输出一行」与「剥掉 think 块后答案行是否合规」——`n_predict` 小于 think 块消耗会造出**假的 0% 服从度**（踩过并已更正；机制同「已知陷阱」里 `reasoning_content` 吃光 `max_tokens` 那条）。别把「必须靠解码侧语法」写成硬结论：给足预算 + 剥壳也能拿到合规答案行，GBNF 的价值是省预算 + 免解析容错。
- **选型看判断力，不看格式**：格式合规普遍能到 100%；另外要看**目标槽位是否恒定**（多数用例都给同一个候选 = 没在候选间真做选择，只是把格式填满），这类「格式对、决策空」要点名。
- **延迟只在轻载时采信**：同机有别的 agent 跑推理时 p50 会在 0.6s ↔ 80s 间摆动；先 `ps` 排除自己的残留 server，并复跑一次——同一输入跳运行可翻结论，只跑一遍就报命中率是过度自信。
- **长基准边跑边报**（每用例一行 + `flush` + 落盘），别静默几十分钟；收尾时**回头检查所有后台任务的输出**，迟到通知里可能带着被放弃的那份完整数据。

## 换推理后端（llama.cpp 直跑）四条硬规则

完整配方（blob→GGUF 搬运、三实例拆分、显存账、运行参数与实测基准、切换顺序）在 `references/model-ecosystem-beyond-ollama.md`，搬运脚本 `scripts/ollama-blobs-to-gguf.py`。动手前先记住：

1. **迁移是「先并行验证、最后才切」**，不是原地替换：新后端全部验证通过前不动旧服务的 base_url，旧服务的模型文件也不删。
2. **容器 `mem_limit` 管系统内存，不管显存**：显存由 `-ngl` / `-c` / KV 量化决定。「只用显存不吃内存」的唯一前提是显存真够；差一点就有层落回 CPU 内存，低 mem_limit 立刻 OOM 被杀（退出码 137）。
   - **但 `docker stats` 的内存数不是「真占用」，调 mem_limit 前先分清两种页**：mmap 加载的权重落在**文件页缓存**（`RssFile`）里，会被 cgroup 记进 `memory.current`、随时可回收，看着大而不吃紧；真正钉死的是**匿名内存**（`RssAnon` = KV + 计算缓冲 + CUDA ctx）。拆法：`grep -E 'RssAnon|RssFile|VmSwap' /proc/<pid>/status`（用 `pgrep -x <二进制名>` 取 pid，见「下载与搬运」的 `pgrep -f` 自杀坑）。实测 27B 三元 @8192 ctx：`RssAnon ≈ 2.0 GiB`，其余显示占用基本都是可回收缓存。
   - **所以「降低 mem_limit 会不会 OOM」不取决于那个百分比，而取决于 anon 装不装得下**：主人把 extract 从 10GiB 收到 **4.13 GiB** 后，容器显示占用 3.5~3.9 GiB（≈90%，绝大部分是缓存）而照常跑完 retain。**收紧要趁容器空闲/刚起时做，收完看计数器验收**：cgroup `memory.events` 的 `oom`/`oom_kill` 全为 0 + `docker inspect` 的 `RestartCount`/`OOMKilled` 不变 + `dmesg -T | grep 'Memory cgroup out of memory'` 无新行。⚠️ 反过来，`max` 计数在涨或 `RestartCount` 在涨 = 真撞上限了，该给回余量或降 `-c`；**别拿 `docker stats` 的「8G/10G」当「它非要吃 8G」**（高上限本身会把页缓存养到上限附近，看着像漏，其实一压就下来）。
   - **`--cache-ram <MiB>` 是宿主 RAM 侧的关键旋钮（本机 fork 支持）**：它限制「保存在宿主内存里的提示缓存」，默认按 GiB 量级 → 大 batch 抽取时 anon 会持续爬（27B 实测曾爬到 4.4–5.17 GiB 被 cgroup 杀掉）。**封成 `--cache-ram 1024` 后实测 anon 掉到 0.86 GiB**，于是 `mem_limit` 才能从 7g 收到 **2583m** 而不 OOM（16k ctx 下 GPU 侧 7286 MiB，总账仍有 ~3.3G 空闲）。判据：容器内 `/sys/fs/cgroup/memory.stat` 的 `anon`（真占用）+ `memory.peak`（曾否顶上限；顶到上限但 anon 很小 = 页缓存可回收，不是要炸）。
   - **embed 这类小模型也可以靠 `-ngl` 让位**：`-ngl 99 → 8`（大半层落 CPU）腾出 **1.8G 显存**（3100 → 1260 MiB），代价是宿主 `file` 页涨到 ~2.3G → **`mem_limit` 必须相应给到 3.5g，照旧套 1g 会被 OOM**（同一个模型，改 `-ngl` 就把内存账翻了个面，两本账要一起调）。
3. **换后端前先证明向量一致**：同一句话分别打旧 `/api/embed` 与新 `/v1/embeddings`，比余弦相似度、要原始数字（>0.999 才算一致），并看**最近邻排序是否一致**——只盯单个相似度会误判。对不上就别切，既有向量库的召回会静默变差。
4. **判断本地推理速度别用小样本**：只让模型生成几个 token 再算平均值，首 token 的 prefill 开销会摊进去——实测过「7 token 得出 12 tok/s」的假象，同一实例跑 130+ token 实际是 43 tok/s。**要跑 ≥100 token 再测**，或直接读 llama-server 日志里的 `slot print_timing` 行（带 `tg = xx t/s`）。另：**prompt eval 速率是「真在 GPU 上跑」的硬证据**（12G 卡跑 8B Q4 是几百 tok/s，纯 CPU 只有个位数到几十），报速度问题时先拿它定性，别猜。
   **更要紧的是计时口径：别用墙钟。** 排队、被别的请求占卡的时间会全算进 wall，而服务端自报的数是干净的——实测同一实例连打两次同一请求：decode 23.4 vs 24.1 t/s（几乎没变），wall 却是 **157.7s vs 5.7s**，差的 150s 全是排队。取法：`POST /completion`（`n_predict: 128`、`cache_prompt: false`）读响应里的 `timings.predicted_per_second` / `prompt_per_second` / `predicted_n`，脚本 `scripts/llama_speed.py`（顺带读 `/props` 的 `n_ctx`／`total_slots`）。**测基准要挑空闲时**：后台有重跑/批处理占着卡时，decode 会比空载低 ~4 t/s，别把那个数当常态。

## 脚本判不了的模糊判断：可以调本地模型（主人 2026-10-03 定）

纯脚本探针读不出的判断（「这条任务是真卡死还是只是慢」「这条告警算不算异常」）**允许调本地 API 出意见**——但必须守三条护栏（主人原话：单次用时短 / 超时给宽松 / 不因为排队就报错）：

1. **单次用时短**：提示词压到几百 token、`max_tokens` 给 120 量级，只要一个结论不要解释。
2. **超时给宽松**（默认 600s，可用 env 覆盖）：本地后端 `--parallel 1`，一条长抽取能独占几十秒到几百秒，超时给小了必然误判成「模型不行」。
3. **排队/超时不是失败**：拿不到就输出「判定不明（xx s 未取得）」，**退出码恒为 0**，绝不据此报警。挂在「有输出就投递」的自检/cron 上时，把超时写成失败 = 天天误报。

**分层：规则判定为主、模型意见为辅**——硬数据（时长、计数、成功率）能判的一律先写规则，模型只回答规则答不了的那部分，两者冲突以规则为准。本机实现 `/opt/data/scripts/hindsight_judge.py`（已接进每日回看 job 的 `script:` 槽）。

## 已知陷阱

- **本地 VLM 的正文可能不在 `content` 里**：minicpm-v4.6:1b 这类**思考模型**先吐一段 `reasoning_content` 再写 `content`。`max_tokens` 给小了推理段就把额度吃光 → `finish_reason=length` 且 **`content` 是空字符串**（看着像模型坏了，其实没错）。规则：**按任务给预算**——短答 ≥256（建议 512），**整页转录/长表格 ≥4096**（实测 800 时 6 张 150dpi 图有 5 张 content 全空，4096 才有正文）；并**同时读 `content` 和 `reasoning_content`**。

  关思考别改服务端：单请求带 `chat_template_kwargs: {"enable_thinking": false}` 实测有效（reasoning_len 1929→0，5.6s→2.69s，正文更完整），**不必**给实例加 `--reasoning off` 重启容器（牺牲看图推理质量）。`/props` 里模板写的 `enable_thinking` 默认 false 不代表服务端真按此走——以实测 `reasoning_len` 为准。整条栈的实测配置与数字见 `references/model-ecosystem-beyond-ollama.md`。

- **大 chunk JSON 截断**：`Unterminated string` parse error + 重试全挂 = `num_ctx` 不够（大 chunk 输入吃满，输出被 clamp）。修复：Hindsight 加 `HINDSIGHT_API_LLM_OLLAMA_NUM_CTX=8192`（Hindsight 支持该 env，直接传给 Ollama）；显存紧张时配 `OLLAMA_KV_CACHE_TYPE=q8_0`（KV 减半抵消 context 增长）
- **宿主 ps 看到 docker 容器进程**：飞牛 docker 的容器进程在宿主 ps 可见（如 `shy /app/api/.venv/bin/hindsight-api`）——**不是宿主旧版/遗留**！判断归属看 cgroup（`cat /proc/PID/cgroup` → system.slice/trim_app_center.service 或 docker）或 PPID，别只看 ps 猜（8/25 误判 docker stop 差点搞挂记忆服务）
- **hindsight_retain 工具 60s 超时**：模型切换/未加载时 retain 抽取排队超时（报 `Failed to store memory: ` 空错误）——不是工具坏。**批量重跑失败 op 期间也会这样**：worker 是单槽（`HINDSIGHT_API_WORKER_MAX_SLOTS=1`），你刚塞进去的重跑队列会把槽占满，此刻任何新 retain 都排队到 60s 超时——是自造争用，不是链路坏了（等这批重跑过去再补写那条记忆）。**recall 正常但 retain 全挂 = worker 侧问题**，两层排查：① ollama 是否回退纯 CPU（见下条）② hindsight worker slot 是否被卡死任务占满：`docker logs hindsight | grep -E "STUCK|PENDING_BREAKDOWN"`——`slots=1/1` + 同 op `age>300s` = 卡死；常见元凶是 **mental model 每日 refresh_cron**（`GET .../mental-models` 看 trigger.refresh_cron）——刷新走 reflect agent（tool-calling），本地 qwen3:8b 不产 tool call → 到点卡死占满唯一 slot。修复：`PATCH .../mental-models/{id}` body `{"trigger":{"mode":"full","refresh_cron":null}}` 停自动刷新 + 存量 pending/claimed 卡死 op 直接 DB 标 failed（psql 清 async_operations，端口 5433 不是 5432，容器内 `/home/hindsight/.pg0/installation/18.1.0/bin/psql`）。API 直连格式是 `POST /memories {"items":[{"content":...}]}`（工具走 SDK 同一格式）
- **失败 op 重跑配方（重试父 op 无效）**：`GET .../operations?status=failed&limit=100` 拿列表（响应键是 **`operations`**，不是 `items`）→ **跳过父聚合器**（`result_metadata.is_parent=true`／`task_payload` 为空；Hindsight 的 `batch_retain` 是父，真干活的是它底下的 `retain` 子 op）→ 对子 op `POST /operations/{id}/retry` → **逐条轮询到终态再打下一条**（worker 单槽，一次全 POST 只是把请求堆进队列）。父 op 在子 op 修好后**不会回溯变绿**，会永久留在 failed——残留计数不归零是显示层现象，不是还有坏任务。副作用：重跑 retain 会重抽同 `document_id`，全库 `fact_count` 小幅波动，不是丢数据。
  判断一条 op 为何处于失败态，读 `error_message` 原文（如 `cancelled by ops: ...; cron disabled` = 当年停某项自动刷新时被顺手标掉的**孤儿失败**，无人会再碰）比读记忆准；**不要**拿库里一条 `who=<OWNER>` 的事实去证明「这是主人的决定」——retain 管线把**助手侧**的结论也写成 `who=<OWNER>`（只靠 `fact_type=assistant` 区分）。
- **「跑完不用盯」的长任务这么摆**：重跑/retry 类 POST 只**入队**，真活由服务端 worker 干，所以发起进程死了任务照跑——不必在会话里守着轮询。要「晚点再看有没有出错」就挂**两个一次性 cron**：① 复查（读状态文件，无异常则空输出 → 静默不投递）② **幂等补跑**（只捡此刻仍在 failed 的，没目标则静默）——会话被掐断也能接着做完。
- **别凭截断的 ID 前缀拼 UUID**：列表只显示前 8 位时，按印象补全后面几段再去查，得到的 `not_found` 是**你自己编的 ID** 的错，不是服务端的回答（第 2、3 段凭印象补必错）。完整 ID 只从保存下来的原始响应里取，不要重建。
- **宿主重启后 ollama GPU discovery 会失败 → 全模型回退纯 CPU**（症状：任何 LLM 活都变极慢/超时——8B 抽大 prompt 几分钟）：日志特征 `llama-server GPU discovery watchdog timed out` ×2（cuda_v13/cuda_v12 各 30s）+ `could not determine compute capability`；验证：llama-server 命令行无 `-ngl`/GPU 参数且带 `--no-mmap`、`nvidia-smi` 显存 0 MiB、实测 tok/s 个位数。修复：trim-cli `app restart ai_installer`（应用中心托管）→ discovery 重跑成功（日志 `offloaded 37/37 layers to GPU`）→ nvidia-smi 有占用即恢复。
- **use_mmap 补丁不持久**：`ollama pull`/create 覆盖会重置 manifest 丢掉 PARAMETER（9/2 打的全丢过）。恢复后先 `ollama show <model> | grep use_mmap` 抽查，丢了重跑 `/opt/data/scripts/ollama-mmap-patch.sh`（对全部已装模型幂等重打）
- **HF 缓存的本地模型启动时仍可能联网**：重排器 / transformers 类库启动时会去列 HF repo 文件（`huggingface_hub.list_repo_tree` → httpx），本机 `huggingface.co` 是域名级黑洞 → `ConnectTimeout [Errno 110]` → 进程 `Application startup failed. Exiting.` 进崩溃重启循环。**权重在本地缓存 ≠ 不需要网**：装离线旗标 `HF_HUB_OFFLINE=1` + `TRANSFORMERS_OFFLINE=1` 即解；日志里 `Loading weights: 100%` 只说明缓存加载完成，不代表启动成功，别拿它当「启动到一半才崩」的依据。
- **改容器内代码**：`docker restart` 保留修改，但 `docker rm` + 重建恢复镜像原版——持久化需 docker commit 或挂载覆盖
- **Ollama pull 走代理**：`--proxy` 参数不存在，用 `HTTPS_PROXY=http://127.0.0.1:17890 HTTP_PROXY=...` 环境变量
- **ollama 的 OpenAI 兼容 `/v1/embeddings` 忽略 `dimensions` 参数**（实测传 `dimensions:1024` 仍返回模型原生 2560 维）——想按 MRL 截断输出**不能靠参数**，只能在中间做截断代理，或换支持 `dimensions` 的服务层（vLLM 原生支持，但要求模型 `config.json` 有 `is_matryoshka`；Qwen3-Embedding-4B 没有这个键，需 `--hf-overrides '{"is_matryoshka": true}'`，否则改维度直接 400）。它**支持** `encoding_format: base64`（返回 base64 小端 float32）——写代理/客户端时两种编码都要处理。
- **embedding 端点重复调用不保证逐位一致**：实测 1024 维截断代理首次调用与后续调用分量最大差 8.98e-03（同题余弦低到 0.9985），而 2560 直连端点逐位一致。**任何靠相似度/名次差异下结论的脚本都要重复取数 ≥5 次**并确认名次稳定，否则 ±1 名可能就是噪声。
- **别信 `/v1/models` 的 `capabilities` 声明，也别把 embedding 模型当小 LLM 用（实测定案）**：本机 8085 的 `qwen3-embedding:4b` 自报 `capabilities: ["completion"]`（GGUF 保留了语言头），但真打 `/v1/chat/completions` 只会**退化重复**（实测 `昨晚-nighttimeLast nightnighttimeLast night night night…`），短 `max_tokens` 请求直接 HTTP 500——嵌入微调已把语言建模能力洗掉。**「接口自称能生成」≠ 能说人话；要用就先真发一次生成请求看输出成不成句。**
- **「用 embedding 相似度当意图判官」不成立**（同批实测）：拿「在叫我 / 跟我无关 / 在讨论话题」三类锚句比余弦，无关群消息反而跟「在叫我」最像（0.576 vs 0.491）、所有分数挤在 0.42~0.58（≈噪声），且本机链路是 2560→1024 的 MRL 截断代理、判别力更差。**门控/判定类活不要用 embedding 顶替**：要么上真正的小 LLM 并给**独立端点**（别跟抽取抢同一个 `--parallel 1` 槽），要么先用纯规则+概率扛（规则层零成本且本来就必须有）。
- **判断宿主服务「在不在」先看它绑在哪个地址**：`ss -tlnp` 显示 `172.17.0.1:11434` 时，宿主侧 `curl 127.0.0.1:11434` 是**空的**（服务活着，只是没监听 loopback）——别据此判定服务挂了。同一台机器上「宿主用 127.0.0.1 / 容器用 172.17.0.1」不是通用规则，**按 `ss` 输出里的实际绑定地址发请求**。
- **ollama pull 后台静默退出**：SSH setsid 后台跑 pull 会静默死（tail 无输出），用前台跑（9G 约 4 分钟 < 600s 上限）
- **`docker ps` 报的 unhealthy 可能是假的，且只改探针的重建不会发生**：容器里实际生效的是「镜像自带 HEALTHCHECK」和「compose `healthcheck:`」里活下来的那个，`docker ps` 显示的是它——**探错端口的探针会让健康状态一直假**（llama 三件套曾长期报 unhealthy，实际探 8080，而服务在 8081/8082/8083）。更阴的是：**只改 `healthcheck:` 的 compose 改动不会触发重建**——compose 的 config-hash 不覆盖 healthcheck，`docker compose up -d` 判定「配置未变更」，容器照旧（改前改后 hash 一模一样）。所以改探针必须 `docker compose up -d --force-recreate`，改完用 `docker inspect <c> --format '{{json .Config.Healthcheck.Test}}'` + 直接 `curl -f http://172.17.0.1:<port>/health` 复核，**别拿 `docker ps` 的 Status 当判据**。

- **手工 `docker run` 起的容器是「临时态」，别把它当配置（2026-10-02 实测踩中）**：它不继承 compose 的 `restart:`（实测变 `no` → 宿主/docker 一重启服务就静默消失），也不继承 compose 的 `healthcheck:`（退回镜像自带的探 8080 假探针 → 一直 unhealthy）。所以「unhealthy + 重启不自启」这对症状先怀疑「容器是手工起的」，别怀疑模型坏了。**一行核对漂移**：`docker inspect <c> --format '{{join .Config.Cmd " "}} restart={{.HostConfig.RestartPolicy.Name}} mem={{.HostConfig.Memory}} proj={{index .Config.Labels "com.docker.compose.project"}}'` —— 与 compose 原文逐字比；`proj=` 为空 = 不归任何 compose 项目管，下次 `compose up -d` 会把参数打回文件里的旧值（而旧 `mem_limit` 可能已经把新参数下的容器 OOM 掉）。**调优完立刻固化**：终态写进 compose（带依据/实测/回滚注释）→ byte-exact 备份原文 → `docker update --restart unless-stopped <c>` 先零停机堵自启 → `compose up -d` 重建 → 复核 `/props` + `/health` + 一次极小请求（27B 重载约 110s，挑 GPU 空闲窗口做）。
   - **但“GPU 空闲”不等于“可以重建”：重建 LLM 容器会打断 Hindsight 正在跑的抽取/整合任务**（客户端收到 `HTTP 503 Loading model` → 它自己重试，不丢数据但会白烧一轮尝试）。判断能不能重建要看 **Hindsight 的 worker 队列**：`docker logs hindsight | tail -30` 看 `[WORKER_STATS] slots=1/1 ... my_active: consolidation/retain(...)` —— 有 active 就先等；`slots=0/1` 且看不到 `my_active` 才是安全窗口。查队列别只看 GPU 利用率。

## Ollama 服务本身：内存 / 后端 / 截断（原 ollama-ops）

服务由应用中心（`trim_app_center.service`）fork：**kill 后不会自动拉起**（不 watch），手动启动要带完整 env（OLLAMA_MODELS/OLLAMA_HOST…）。

**资源模型（先理解再动手）**：后端决定权重内存归宿——CUDA 后端权重进显存（8B Q4 RSS ~1-2G），Vulkan/CPU 后端 `--no-mmap` 生效、权重全量进 RAM（同模型 8G+），同一模型可差 6-7 倍。判断实际后端：`OLLAMA_DEBUG` 日志 `library=CUDA` vs Vulkan；llama-server cmdline 带 `--no-mmap` = Vulkan/CPU 特征。`OLLAMA_MAX_LOADED_MODELS=0`（默认）无限制，设 1 会导致频繁换载更糟；keep_alive 默认 5 分钟自动卸载（`/api/ps` 的 `expires_at` 可验证），不必改。

**num_ctx 截断根因判断（排除法）**：截断位置随机且随输入长度变化 → 不是 max_tokens；`prompt_eval_count + eval_count << num_ctx` → 不是 context 满；**大 chunk 吃满 num_ctx 剩余空间时输出被 clamp**（`n_prompt` 接近 num_ctx、输出恰好 ≈ 剩余空间）= 主因 → 调用方显式调大 num_ctx（Hindsight：`HINDSIGHT_API_LLM_OLLAMA_NUM_CTX=8192`）配 `OLLAMA_KV_CACHE_TYPE=q8_0`，用 `/api/ps` 的 `context_length` 验证生效。

**内存压力会悄悄禁用 mmap**（小内存机器几乎必触发，最隐蔽的内存暴涨根因）：判定式 `pressure = modelSize + loadedMmapSize + max(8GB, totalMemory/10)`，空闲不足即禁用 mmap → 权重读进匿名内存（RSS 暴涨），日志 `disabling mmap for llama-server load due to host memory pressure`。修复 = Modelfile 显式 `PARAMETER use_mmap true` 覆盖模型判定（脚本 `/opt/data/scripts/ollama-mmap-patch.sh`，拷到宿主跑）；**manifest 不持久**——宿主/应用重启或 `ollama pull` 后重查 `ollama show <m> | grep use_mmap` 并重打。

**重启后 GPU discovery 可能失败 → 全模型回落纯 CPU**：日志 `llama-server GPU discovery watchdog timed out` ×2 + `could not determine compute capability`，此时 env 里 CUDA 变量全在也没用（日志出现 `library=CUDA available=11.5 GiB` 也不代表成功）。诊断三步：`nvidia-smi --query-compute-apps` 无 llama-server 占显存、`/api/ps` 的 `size_vram=0`、直测 tok/s 个位数。修复：重启应用中心应用重跑 discovery（`trim-cli app restart ai_installer --yes`），成功日志出现 `offloaded 37/37 layers to GPU`。

**模型被驱逐重载 → 请求返回空/截断**：显存压力下日志 `model predicted to exceed available memory, evicting` + `done_reason:"load"`，表现为 consolidation/抽取 JSON 解析失败（部分成功部分失败）。别往 max_tokens / schema `$defs` 方向排查（实测都正常），按内存 + watchdog 处理；仍偶发就降 `OLLAMA_MAX_LOADED_MODELS=2`。

**能不能绕开 Ollama 自己跑 llama.cpp**：Ollama 内部就是 llama.cpp（推理进程名就叫 llama-server），换它 = 绕过调度层、把 `-ngl/--no-mmap/--cache-type-k/-v/-c/--no-kv-offload` 变明牌，但两条硬卡点：① llama-server 只有 OpenAI 兼容接口，**没有 Ollama 原生 `/api/chat`、`/api/embed`**——照原生接口调的调用方要么支持 OpenAI base_url，要么加转接层；② Docker GPU 直通要 `daemon.json` 加 nvidia runtimes（重启 docker 会带下所有容器，须挑时间）。白捡：Ollama 的 blob 本身就是 GGUF，`-m <blob>` 直接能跑。

**操作坑**：`pkill -f "xxx serve"` 会匹配到承载命令的 ssh 会话自身（自杀）→ 先 `pgrep -f` 拿 PID 再 kill；运行中的二进制不能覆盖（`Text file busy`），先停进程再写 wrapper。

**改动后验证清单**：① `/api/ps`（模型名、context_length、size_vram、expires_at）② `nvidia-smi --query-compute-apps` ③ llama-server `VmRSS`（/proc/PID/status）④ 调用方日志 parse error 计数。

## 云端 API 成本估算（原 llm-api-cost-estimation）

任何「这个云端操作要花多少钱」按三步走，**禁止凭记忆报价**（价格频繁变动）：

1. **先查官方最新价目**：`web_search "<模型> 价格 每百万 tokens 高峰 空闲"` + 官方 pricing 页/当日新闻确认（价目表常在 JS 里，抓不到就用搜索摘要拼数字）。
2. **测实际 token 用量**（不拍脑袋）：Hindsight 场景 `GET /v1/default/banks/<bank>/llm-requests` 拿真实 input/output tokens；无实测时输入 ≈ 处理内容 token 数，输出按操作类型估（抽取 ~5-10%、consolidation ~30%）。
3. **套价**：`tokens/1e6 × 单价`，输入输出分开算，注意缓存命中/未命中与高峰/空闲两档。

关键口径：**缓存命中基本不可能**（consolidation 每批输入都不同、prompt 前缀不重复）→ 一律按未命中价算。**consolidate ≠ 全量重抽取**（差两个数量级）：consolidate 合并已抽取词条（~0.3M tokens，1-3 元量级），全量重抽取要重读原始文档（~42M tokens，空闲 ~72 元 / 高峰 ~145 元）——主人问「抽取全部多少钱」与「consolidate 多少钱」是两笔账，别混。何时值得切云端：本地干不了才干（日常 retain/recall 本地够用，云端是临时工具不是依赖），且持续成本也要算（新记忆每周积压 ~350 条 ≈ 0.3-0.6 元/周）。
DeepSeek 价目表与实测 token 口径见 `references/cloud-api-pricing.md`。

**余额与用量的现成查法（不用外挂计费网关）**：
- 余额：`GET https://api.deepseek.com/user/balance`（`Authorization: Bearer <key>`）→ `balance_infos[0].total_balance` + `is_available`。一行拿到余额，适合当阈值告警源。
- 用量账本（Hermes 自带）：`hermes insights --days N` 出会话/消息/工具调用/输入输出 token/**估算花费**/按模型·平台·工具分布；要单次 JSON 用 `hermes -z "..." --usage-file x.json`（含估算成本、token、模型）。
- 主人偏好：**余额与时段提示不用精确** → 挂进心跳（每次一句「余额 + 当前峰/谷 + 今日估算花费」），低于阈值再单独提醒；**不要为此上 LiteLLM / one-api 这类记账网关**（白搭一跳代理 + 一个外挂点）。

**峰谷时段（决定重活什么时候跑）**：高峰 = **周一至周五 01:00–04:00 与 06:00–10:00 UTC**，即北京时间 **09:00–12:00 与 14:00–18:00**；**其余全时段都是谷价，谷价 = 峰价的一半**。重活排到工作日 12:00–14:00、18:00 之后、周末全天（官方 pricing 页脚注，会变动，用前复核）。

**排查 402 Insufficient Balance 的顺序**：先 balance 端点直查（有余额 → 失败的不是这把 key）→ **再对时间**（充值前的 402 会长期留在容器日志里，一批报错可能全是历史）→ 最后才考虑 key/通道来源不同。**别一看到 402 就报「没钱了」**。

## 附：相关主题
- PDF/文档进 Hermes 的入口行为（`read_file` 的 NEEDS OCR 误报与误报真伪判定）与 OCR 兜底一行命令：`references/pdf-ingestion-and-ocr-fallback.md`
- 本地视觉路由（**已生效为默认**：图片不再以像素进主模型、`auxiliary.vision` 指向 8081 的 Bonsai 27B + mmproj，含实测验证代码与回滚）：`references/hermes-image-routing.md`
- QQ 附件落地与读图/读文件链路（图片、文档、语音各自落到哪、主人发的文件能不能直接读）：`references/qq-image-vision.md`
- VLM 识图质量量尺（场景清单、CER/数字命中率口径、失败模式、要不要接后端的判据与实测数据）：`references/vlm-vision-quality-bench.md` + `scripts/vlm_page_bench.py`
- llama.cpp 容器栈的落地状态、验收命令与内存/显存账：`references/model-ecosystem-beyond-ollama.md`
- 现场只读探针（可重复跑）：`scripts/llama_speed.py`（decode/prefill 与上下文，取服务端 `timings`）、`scripts/gguf_meta.py`（GGUF 架构事实；它推算的 KV 仅当假设）
- 显存 vs 内存两本账的实测流程（KV 量化实收为何低于算术、按请求分布降 ctx、`mem_limit` 要按加载期峰值定、**改完的功能验证三件套**：embedding 维度含截断代理 / 召回条数 / 真实 retain）：`references/vram-vs-ram-tuning.md`
