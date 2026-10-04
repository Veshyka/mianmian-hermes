# Ternary Bonsai 2 27B（PrismML）在本机的实测结论（2026-09-21）

**结论：生产抽取链路不换**（保持 qwen3-8b Q4_K_M）。Bonsai 2 27B 的质量确实更高，但代价与风险不划算；详见下方数据。

## 1. 能不能在 llama.cpp 上跑：能，但**必须换二进制**

- 模型：`prism-ml/Ternary-Bonsai-2-27B-gguf`，文件 `Ternary-Bonsai-2-27B-PTQ1_0.gguf`（5,946,648,928 B，sha256 `53107f53…ee3`）；另有 `PQ2_0`（7.2GB，2-bit 槽位）。训练底座 Qwen3.8-27B，Apache-2.0。
- **官方 llama.cpp 直接拒绝**（实测，build 11028 / commit 972d2313b，即本机 `ghcr.nju.edu.cn/ggml-org/llama.cpp:server-cuda`）：
  ```
  E gguf_init_from_reader: tensor 'output.weight' has invalid ggml type 143. should be in [0, 43)
  E llama_model_load: error loading model: ... failed to load model from ...PTQ1_0.gguf
  ```
  README 原话：stock llama.cpp「rejects PQ2_0 and PTQ1_0 as unknown types」，且对 `Q2_0`（另一仓库的 dev 包）**不报错但出垃圾**（缺 Hadamard 激活运行时）。
- **能用的是 PrismML fork**：`github.com/PrismML-Eng/llama.cpp` release `prism-b10709-9a9394a`，取 `llama-prism-b10709-9a9394a-bin-linux-cuda-12.8-x64.tar.gz`（167MB）。**fork 二进制塞进现有 ggml-org CUDA 镜像里直接能跑**（镜像自带 libcudart.so.12 等 CUDA 12.8 运行时）：
  ```bash
  docker run -d --name llama-bonsai --device nvidia.com/gpu=all \
    -v /vol1/1000/<USER> \
    -v <解压的fork目录>:/fork:ro -e LD_LIBRARY_PATH=/fork \
    --entrypoint /fork/llama-server ghcr.nju.edu.cn/ggml-org/llama.cpp:server-cuda \
    -m /models/bonsai-27b/Ternary-Bonsai-2-27B-PTQ1_0.gguf --alias bonsai2-27b \
    -ngl 99 -c 8192 --parallel 1 --flash-attn on --jinja --reasoning off --temp 0.1 --no-webui
  ```
  即**换后端＝挂载目录 + 改 entrypoint**，不必换镜像、不必动 daemon.json。

## 2. 12G 卡上的实测占用与速度（RTX 3060 Laptop 12G）

| 项 | qwen3-8b Q4_K_M（现行 extract） | Bonsai 2 27B PTQ1_0 |
|---|---|---|
| 权重文件 | 5.23 GB | 5.95 GB |
| 实占显存 `-c 8192` | 5494–5520 MiB | **6374 MiB** |
| 实占显存 `-c 32768` | — | 7934 MiB |
| decode（服务端 tg） | ~45 t/s | **~25 t/s** |
| prefill（服务端 pp） | ~1055 t/s | **~285 t/s** |

共存账（实测，桌面占用可忽略）：extract 5520 + embed 3968 + vision 1934 ≈ 11422 MiB 是**现状能共存的极限**。Bonsai(8192) 6374 + embed 3968 = 10342 MiB **能共存**；Bonsai(32768) 7934 + embed 3968 = 11902 MiB 贴顶、加 vision 必爆。

## 3. 中文长文本事实抽取 A/B（同批真实 Hindsight 请求，6 条 × ~3.2k token prompt）

样本取自 Hindsight 自己的 `GET /v1/default/banks/{bank}/llm-requests`（**该端点回放完整 input messages，是最忠实的复现素材**），逐字用同一 system/user，`temperature 0.1`、`response_format {"type":"json_object"}`（strict_schema 默认 False = 软路径），`max_tokens 3072`。

| 指标 | qwen3-8b | Bonsai 2 27B |
|---|---|---|
| JSON 合法率 | **6/6** | 5/6（s6 输出超预算→`length`→`Unterminated string`）|
| schema 违规 | 1 条（causal target_index 自指） | 0 |
| 事实数 | 4,8,5,7,4,7（均 5.8） | 4,10,7,7,7,22（均 9.5） |
| 因果边 | 3,0,4,6,4,0（17） | 3,8,0,0,3,3（17） |
| 主体归一化 <OWNER> 出现 | 20 | **43** |
| 输出 token 均 | 1365 | 2172（s6 需 3840） |
| wall（同机带并发） | 22–105 s | 92–245 s |

质量面 Bonsai 明显更好（事实更原子、因果链更成结构、"Assistant 做的每件事"这类丢主体的写法少）；但**JSON 稳定性没有变好、反而更依赖输出预算**——它更啰嗦，`-c 8192` 的 extract 实例上余量只有 ~4.2k token，逼近就截断（正是历史上踩过的 `Unterminated string`）。语言规则上两者都守不住（s1–s3 都输出英文，prompt 要求与输入同语言）。

## 3b. 收紧 prompt 后的同一批对照（这一步把上面那条否决推翻了）

做法：把硬约束写成一个追加文件（`/opt/data/tmp/bonsai/tight_constraints.txt`：**每个文本字段必须简体中文**、**≤8 条事实**、**每条 what ≤40 字**、`causal_relations.target_index` 必须严格小于所在条目且 `relation_type` 只能是 `caused_by`、只输出 JSON 无解释），用 `ab_test.py --extra-system <文件>` 追加到 system 末尾，对**同一批 6 条真实请求**分别打两个模型（两个模型必须用同一份新 prompt，否则不公平）。结果：

| 组 | JSON 通过 | schema 违规 | 事实数 | 输出 token 均 |
|---|---|---|---|---|
| qwen3-8b 默认 | 6/6 | 1 | 4–8 | 1365 |
| qwen3-8b 收紧 | **6/6** | **0** | 5–8 | 1507 |
| Bonsai 默认 | 5/6（s6 截断） | 0 | 4–22 | 2172 |
| Bonsai 收紧 | **6/6** | **0** | 4–8 | **1416** |

结论：**截断是提示词问题，不是模型能力问题**。收紧后 Bonsai 的「更啰嗦」被压掉 35%（2172→1416），8k ctx 的余量问题基本消失，s6 那条从失败变通过；8B 收紧后同样零违规。所以「Bonsai 不适合抽取」这个印象要收回——否决理由只剩速度（1.8× decode / 3.7× prefill）、显存 +850 MiB、以及必须长期维护非官方 fork。**通用教训：换模型前先用收紧 prompt 的对照排除「提示词没管住」**（见 SKILL.md「抽取质量验证方法」第 5 条）。

## 4. 换/不换判据

**不换**，因为：① JSON 通过率是主人真正痛过的点，Bonsai 在此**没有优势**（预算更紧、截断即解析失败）；② 速度 1.8× decode、3.7× prefill 慢，Hindsight 单 chunk 从 ~60s 变 ~150s；③ 必须长期维护一条**非官方 fork**（stock 不认 type 143，Ollama 同理不支持）。**但**若目标变成"离线批量重抽历史文档、要最细事实与因果"，Bonsai 是更好的机器，落地方案＝fork 二进制 + `-c 16384~32768` 单独实例（不与 embed 共存），跑批不占在线链路。

## 5. 踩过的坑（下次直接抄）

- **别用 `curl -C -` / `--retry` 下大模型**：本机代理链下 Range 语义不稳（实测把 6GB 文件追加到 6.85GB），`--retry` 还会从头覆盖。改用**显式 Range + 校验 `Content-Range` 起始**的分块下载（`scripts/hf_chunked_dl.py`），下完 `sha256sum` 对齐 HF API 的 `lfs.sha256`。
- **`pgrep/pkill -f '<pattern>'` 会匹配承载命令的自己的 shell**（`pkill -9 -f resolve/main/Ternary` 直接把跑命令的会话 kill 了，退出码 -9）。先 `pgrep -f` 拿 PID 再按 PID kill。
- **`dagster`（并行实例）风险**：多个 agent 同时在同一个 workspace/GPU 上干活时，会互相 append 同一个文件、互相 `docker rm` 对方的测试容器（实测出现过第二次 "文件比 expected 大" 与开着跑一半容器被重启）。下载/长跑任务前先 `pgrep -af` 确认只有一个写入者。
- **`GET /v1/default/banks/{bank}/llm-requests`** 与 **`/config`** 是评估抽取质量的钥匙：前者回放真实 prompt+输出+token+耗时与 `llm_info.request`（temperature/max_completion_tokens），后者给出 bank 级 `retain_extraction_mode`/`retain_mission`/`retain_custom_instructions`/`enable_observations`。
- Hindsight 抽取默认 **strict_schema=False**（`DEFAULT_LLM_STRICT_SCHEMA=False`）→ 走**软路径**：schema 以文本附在 system 末尾 + `response_format=json_object`；所以 JSON 合法性真的是模型的责任，值得在选型时单独量。
- **`SUDO_ASKPASS` 要用宿主 `/vol1/1000/<USER>`，不是容器的 `askpass.sh`**：`ssh 宿主 'SUDO_ASKPASS=<mian_sudo.sh> sudo -A docker …'`。用错会静默失败，极易误判成「sudo 不通」；另外 `docker logs --tail N` 常够不到启动行，别据此断言「日志里没有」。
- **llama.cpp 的 KV 占用这个 fork 不打印**（`--verbosity 3` 也没有 KV self size 行）。可靠办法是**差值法**：容器总显存 − 权重 − mmproj = KV+计算缓冲+CUDA ctx 的上界。实测 Bonsai 27B：7226 − 5671 − 600 = **955 MiB** ⇒ KV(8192) ≤ ~119 KiB/token，公开的 **64 KiB/token 估算成立**。⚠️ 别拿 GGUF 元数据按 `key_length/n_head` 反推 head_dim（那样得 256 KiB/token，与实测矛盾）。
- **`stack/hindsight/` 目录是 root 属主、但 compose 文件是 hermes 属主**：`patch` 工具会在同目录建 `.hermes-tmp.*` → `Permission denied`，同目录 `cp` 备份也会被拒。解法：备份写 `tmp/`，改动用就地 `open(w)` 写（`scripts/enable_strict_schema.py`，带 md5 校验 + 匹配数断言）。

## 6. 2026-09-21 收尾：四条件全过 → 已上生产（本节推翻 §4 的「不换」）

四个 GO 条件全部实测通过（数字皆可复现）：

| 条件 | 实测 | 结论 |
|---|---|---|
| A 抽取（收紧 prompt） | 6/6 JSON 合法、0 截断（`length`=0/24 条）、0 schema 违规（`fact_type` 只能 world/assistant；`relation_type` 只能 caused_by；`target_index` < 自身）、`what` 中文本 41/41 | 过 |
| B 速度 | decode 28.67 t/s（服务端 timings）／自测 24.79 t/s（wall 含 prefill，≥100 token）；单条 wall 61.7s vs 8B tightened 34.7s = 1.78× | 过 |
| C 多模态 | 测试图 5/5 行逐字正确（与 minicpm + 人读两路基准一致） | 过（带限制，见下） |
| D 显存 | Bonsai(含 mmproj) 7250 + embed 3974 = 11224/12288，剩 ~670 MiB，`Restarts=0` 无 OOM | 过 |

收紧 prompt 的真实作用：Bonsai 默认 prompt 5/6（失败一例是 `ok=false` 的服务端断连，**不是 `length` 截断**——全 24 条都是 `stop`），收紧后 6/6，输出 token 均值 1838.8→1416.3（↓23%）、最大 2764→1817。

**切换后的生产形态**：`llama-extract`(8081) = Bonsai 27B PTQ1_0 + mmproj（entrypoint `/fork/llama-server`、`mem_limit 10g`；`--alias` 切换时先留 `qwen3:8b`，**同日规范化为 `bonsai2-27b`** 并同步改 `HINDSIGHT_API_LLM_MODEL` 与 `auxiliary.vision.model`——留着旧名会让排查误判成 8B 还在跑）；`llama-vision`(8083) 从 compose 注释停用；fork 搬到稳定路径 `llamacpp/fork-prism-b10709/`；备份 `docker-compose.yml.bak-before-bonsai-20260921-120921`。
端到端验证：`POST /memories` → 文档落库 `memory_unit_count=3`、bank `fact_count` 28529→28532、`llm-requests` 记 `model=qwen3:8b success in=2874 out=783`。

**看图能力边界**：中文正文可靠，但长字母数字编号会**漏读**（实测一张含 `HX-7742`/`BQ-9184-C`/`8F3A-Q7` 的图，中文全对、三个编号全空）——漏读不等于幻觉，但引用单号/数值时要走 OCR。

**未闭环**：① `auxiliary.vision` 仍指 8083 → Hermes 看图静默回退云端主模型（修法：改成 `http://172.17.0.1:8081/v1` + `model: qwen3:8b` 后重启网关；cron 会话改不了 config.yaml）；② 收紧 prompt 未进 Hindsight bank prompt，生产行为仍等同默认组。

## 7. 抽取温度矩阵实测（0.1 / 0.5 / 0.7 / 1.0）

做法：`ab_temp.py --temp T`（独立脚本，未改 `ab_test.py`；在 `/opt/data/tmp/bonsai/`，临时目录可能被清，重建要点＝**只加 `--temp`**、system/user 逐字复用 `samples.json`、**每条落盘**）。**只动 temperature**，其余全部固定：system = 真实 bank prompt + `tight_constraints.txt` 追加、`response_format={"type":"json_object"}`、`max_tokens=3072`；**串行**跑（生产 `--parallel 1`），每个温度开跑前先 `curl --noproxy '*' http://172.17.0.1:8081/health`。评分 `score_temp.py`，另用独立实现 `score_temp_v2.py` 交叉核对。

| 指标 | 0.1（当时生产） | 0.5 | 0.7 | 1.0（model card 推荐） |
|---|---|---|---|---|
| ① 根形状合规（对象含 `facts`） | 6/6 | 6/6 | 6/6 | **3/6** |
| 严格 `json.loads`（不剥围栏） | 6/6 | 6/6 | 6/6 | 5/6（s2 带 ```） |
| 顶层输出裸数组 | 0 | 0 | 0 | **3**（s1/s4/s5） |
| 字段零瑕疵 | 6/6 | 6/6 | 3/6 | 2/6 |
| 事实数 合计 / 均值 | 38 / 6.33 | 39 / 6.50 | 43 / 7.17 | 35 / 5.83 |
| 因果边（合法） 合计 / 均值 | 7 / 1.17 | **11 / 1.83** | 15 / 2.50 | 4 / 0.67 |
| 输出 token 合计/均值/最大 | 7822/1304/1859 | 8119/1353/1748 | 8952/1492/1850 | 7242/1207/1777 |
| 字段值中文占比 均值（排除 N/A） | 0.248 | 0.262 | 0.269 | 0.246 |
| `what` 含中文 | 38/38 | 39/39 | 41/43 | 35/35 |
| 空 `what` / 越界或自指边 | 0 / 0 | 0 / 0 | 2 / 1 | 0 / 1 |
| `what` >40 字（违反收紧约束） | 19 | 18 | 24 | 25 |

**边质量（防刷边，必查）**：0.5 的 11 条边**全部结构合法**、4 条指向直接前驱、其余跨度 2–3.4，是真多跳链而非全指 index 0 的伪链；各温度 `what` **零重复**（事实数增长不是拆条灌水）。同题 s5 对比最能说明问题——0.1：4 条平铺事实 + 1 条单跳边；0.5：5 条事实连成链（`[2]→0` 根因是 reflect 模块 → `[3]→2` reflect 默认上限 100000 致溢出 → `[4]→0` 手动 SQL 重置 zombie 恢复写入）。

**结论：0.1 偏低——是召回保守，不是出错。建议 0.5。** 三条边界：① **不要 1.0**：丢掉外壳对象吐裸数组，而 Hindsight 的 `FactExtractionResponse` 要求根是对象且 `facts` 为列表，**裸数组会被整体拒收 → 该 chunk 的记忆是丢失、不是降级**；② 0.7 边最多但拿 2 条空 `what` 脏事实换，且 token +14.4%；③ 官方 model card 写 `--temp 1.0` 是通用对话调参，**不能搬到严格 schema 抽取**。落地位置是**调用方**（Hindsight 侧温度），不是 `llama-server --temp`——后者被请求体覆盖。

**复现性**：同温度重跑，**因果边数逐条复现**（temp 0.1 两次都是 7 条，历史文件 `bonsai-tight.json` 41 事实/7 边/8498 token vs 本轮 38/7/7822）；事实数与 token 有跑间波动。→ 报指标时「边数」可信，「事实数」只看趋势。

**未验证**：每温度仅 1 轮 × 6 样本，7 vs 11 边**未做显著性检验**；未量下游 recall/问答质量；1.0 吐裸数组的根因未做消融；未验证生产 pydantic 是否拒绝 `fact_kind`/`occurred_start` 这类额外字段。

**并发现场校验（并行 agent 共用 workspace 时必做）**：同一 workspace 里另一个 agent 会写同名脚本/同名报告。**评分前先校验结果文件确是本轮产物**——每条记录带本轮唯一标记（如温度值）、记录数=样本数、样本顺序一致，否则可能把对方的数据混进来。本轮另一 agent 从**同一批结果文件**另写了一份报告，其中裸数组样本的事实数被丢掉、边数记错（15 vs 16）→ **以自己的校验过的数据为准，不要直接引用同 workspace 的旁证结论**；wall-clock 若受并发排队影响会偏悲观，结论不要建立在 wall 上。

## 8. 已落地：生产 retain 温度 0.1 → 0.5（2026-09-21）

**落地位置不是 `llama-server --temp`，而是 Hindsight 的服务端 env。** 准确的变量名（官方配置页 `/developer/configuration`，属 LLM 组的「按操作细分」档）：

| 变量 | 作用 | 默认 |
|---|---|---|
| `HINDSIGHT_API_LLM_TEMPERATURE_RETAIN` | **retain 事实抽取**的温度 | `0.1` |
| `HINDSIGHT_API_LLM_TEMPERATURE_REFLECT` | reflect 思考步 | `0.9` |
| `HINDSIGHT_API_LLM_TEMPERATURE_CONSOLIDATION` | consolidation（心智模型 delta / 去重） | `0.0` |
| `HINDSIGHT_API_LLM_TEMPERATURE_VERIFICATION` | 启动连通性自检 | `0.0` |
| `HINDSIGHT_API_LLM_TEMPERATURE` | 全局兜底；上面各档覆盖它；也接受 `none`/`default`/`off` 表示**完全省略该参数** | 按操作默认 |

只设 `_RETAIN` 就不会碰到 reflect（仍 0.9）与 consolidation（仍 0.0）。

**改法**（Hindsight 由 compose 管理，`/opt/data/stack/hindsight/docker-compose.yml`，宿主同路径 `…/stack/hindsight/`）：在 `environment:` 里加 `- HINDSIGHT_API_LLM_TEMPERATURE_RETAIN=0.5` → `docker compose up -d`（env 变更会触发重建，约 30s 后 `/health` 200）。

**读回验证（别只看命令成功）**：不能靠 `/llm-requests` 列表——它**不含 payload**；温度在**每条记录自己的 `llm_info.request.temperature`** 里：

```bash
curl -s --noproxy '*' "http://172.17.0.1:8888/v1/default/banks/<bank>/llm-requests?operation=retain&scope=retain_extract_facts&limit=6" \
 | python3 -c "import json,sys;[print(i['started_at'],(i.get('llm_info') or {}).get('request',{}).get('temperature'),i['status']) for i in json.load(sys.stdin)['items']]"
```

改动前同日 4 条 retain 全是 `0.1`，改后新 retain 是 `0.5` = 生效。⚠️ 顺带确认 `input` 字段里有完整 system/user（该端点确实回放 messages，是复现素材）。

**`HINDSIGHT_API_LLM_STRICT_SCHEMA_RETAIN=true`（2026-09-21 实测生效，当日晚已上生产）**：走 `json_schema` `strict:true` 语法强制（闭集对象、全字段 required）；另有全局版 `HINDSIGHT_API_LLM_STRICT_SCHEMA` 与 `_REFLECT`/`_CONSOLIDATION` 变体。

**源码链路（比日志硬——直接读容器里的代码）**：`retain/fact_extraction.py:1398 strict_schema=config.llm_strict_schema_retain` → `providers/openai_compatible_llm.py:862 strict_json_schema(response_format) if strict_schema else response_format.model_json_schema()`；`config.py:284 _resolve_operation_strict_schema()` 按 env → 全局 env → 默认（`DEFAULT_LLM_STRICT_SCHEMA=False`）解析。provider 注释点名「llama.cpp, vLLM」支持。看代码：`docker exec hindsight grep -rn STRICT_SCHEMA /app/api/hindsight_api/`。

**实测结论（2026-09-21，脚本 `scripts/test_strict_schema_chat.py`）**：
- **判据 = 类型强制**。schema 声明 `answer:string` + `confidence:integer` 时：无约束/`json_object` 输出 `{"answer": 2, "confidence": 1.0}`（**两项都违反 schema**）；开 `json_schema` 后输出 `{"answer": "2", "confidence": 100}` —— 模型不会自发把 `2` 写成带引号的 `"2"`、也不会把 `1.0` 变成 `100`，**故 grammar 生效无疑**。
- ⚠️ **端点差异（极易踩，我自己先踩了一次）**：在裸 `/completion` 端点打同样的 `response_format`，输出会带 `<think>` 前言、看着像没被约束 —— 那是**该端点不走 chat 模板、模型进了思考模式**，不是 grammar 失效。**要验证必须用 `/v1/chat/completions`**（Hindsight 实际走的端点）。
- **「required 会逼模型编造」这个担心已被数据否掉**：模型遇无值字段的行为是写 `N/A` 或 `null`，**不编造**。实测 `where` 在 0.1 档 **38/38 全 `N/A`**；`occurred_start/end` 在 0.1/0.5/0.7 档分别 11/9/19 条 `null`。
- **它确实能挡的**：0.7 档实测出现过 `what` **字段整个缺失**（2/43 条），以及顶层裸 JSON 数组、```` ```json ```` 围栏、散文前言 —— required 与闭集对象正是治这些。
- **它挡不住的两件事（别当万能药）**：`type:string` 的空串 `""` 照样合规（0.7 档的「空 `what` 脏数据」它挡不住）；以及 **token 预算导致的截断**（`Unterminated string` 是预算问题、不是格式问题）。
- 仍未量：语法强制的 decode 速度代价（chat 端点不返回 `timings`）。
