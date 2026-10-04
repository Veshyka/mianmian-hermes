# 跳出 Ollama：模型来源与替代引擎

## 何时需要这份文档
主人说「是不是被局限在 ollama 了」「ollama 官方库不够丰富」时——他的判断是对的，被限制的不是 ollama 本身，而是**它的模型库 + tag 命名 + 调度器行为**。

## 真实约束层级

```
Hermes / Hindsight  →  只需要一个 OpenAI 兼容的 /v1/chat/completions
                       ↑ 谁提供都行（ollama / llama.cpp / 云端 API）
```
迁移成本 = 改 base_url，比想象低得多。

**硬件（12G 显存）才是真瓶颈，不是 ollama。** ollama 只是最方便的那层皮。

## 一、模型来源

| 来源 | 做法 | 说明 |
|---|---|---|
| Ollama 官方库 | `ollama pull <model>` | 覆盖广但滞后，无审查/新模型往往没有 |
| **HuggingFace 的 GGUF** | `ollama run hf.co/<user>/<repo>:<quant>` | 社区版（含 abliterated 等去掉审查的微调）多在这里，ollama 能直接拉 |
| 社区 GGUF 转换 | TheBloke / bartowski / mradermacher 等 | 只是格式转换，模型本身未改动 |

## 二、替代引擎

| 引擎 | 定位 | 什么时候用 |
|---|---|---|
| **Ollama** | 开箱即用，自动量化/GPU 调度 | 默认，够用 |
| **llama.cpp server** | 同一个 GGUF 文件，控制最细（逐层 n-gpu-layers、KV cache、mmap 手动指定） | 要绕开 ollama 调度层的坑时 |
| **vLLM** | 高吞吐，支持 AWQ/GPTQ/FP8 | 显存更大以后 |
| **云端 API** | 无显存限制 | 抽取质量优先或本地排队时 |

### ollama 调度层挖过的坑（这些在 llama.cpp 直跑时可能不存在）
- mmap 误判（内存压力禁用 mmap → RSS 暴涨）
- 宿主重启后 GPU discovery watchdog 超时 → 全模型回退纯 CPU
- `think:false` 对 qwen3.5 架构无效
- 内存紧张时 evict/reload 打断在跑请求

### llama.cpp 官方 docker（已落地路线）

镜像：`ghcr.io/ggml-org/llama.cpp:<tag>`。要 CUDA + server 就 `server-cuda`（entrypoint 就是 `llama-server`，内含 `libggml-cuda.so`）；另有 `full-cuda`/`server`/`full`，一般用不上。

```bash
# ghcr.io 直连不通时走镜像站（详见 skill hermes-container-ops 的 host-docker-access）
sudo -A docker pull ghcr.nju.edu.cn/ggml-org/llama.cpp:server-cuda
# GPU 用 CDI 直通：不用改 daemon.json，不用重启 docker
sudo -A docker run --rm --device nvidia.com/gpu=all \
  --entrypoint /app/llama-server <image> --list-devices   # 应打印 CUDA0: RTX 3060
```

- 数据映射放 `/vol1/1000/<USER>`（= 容器 `/opt/data/llamacpp`，**两边同一个文件夹**），compose 与部署记录就留在那儿。
- 接看图：加 `--mmproj /models/<mmproj>.gguf`；全量上卡：`-ngl 99`；长上下文：`-c 8192`；工具调用：`--jinja`。
- ⚠️ 启动前先腾显存：Ollama 常驻时 12G 卡只剩 2G 出头，`-ngl 99` 加载不动。腾显存用 `POST /api/generate {"model":"<名>","keep_alive":0}`（可逆，Ollama 下次调用自己重载），**不要 `docker stop` Ollama**。

### 从 Ollama 搬模型：blob 就是 GGUF

模型文件在 `/vol2/@apphome/ai_installer/models/`（**不是** `@appcenter` 下那个 bin 目录）：

```
models/manifests/registry.ollama.ai/library/<name>/<tag>   ← JSON，列出各层 digest
models/blobs/sha256-<digest>                               ← 层内容，model 层就是原样 GGUF
```

manifest 里 `layers[].mediaType` 决定这层是什么：

| mediaType 结尾 | 是什么 | llama.cpp 里怎么用 |
|---|---|---|
| `image.model` | 权重 GGUF | `-m` |
| `image.projector` | 视觉投影 | `--mmproj` |

`scripts/ollama-blobs-to-gguf.py` 负责认层 + 复制 + sha256 校验（`--check` 干跑）。
**复制，不要 bind mount Ollama 的 blobs 目录**——旧服务后续要删，挂上去会一起断。

### 一进程一模型 → 按消费方拆实例

llama-server 一个进程只服务一个模型（这正是它比 Ollama 可控之处：占用静态，不会两个模型随机同时驻留）。按消费方拆：

| 容器 | 端口 | 模型 | 谁在打 |
|---|---|---|---|
| `llama-extract` | 172.17.0.1:8081 | qwen3:8b | Hindsight 抽取（`LLM_PROVIDER`） |
| `llama-embed` | 172.17.0.1:8082 | qwen3-embedding:4b | Hindsight 向量化（`EMBEDDINGS_PROVIDER`） |
| `llama-vision` | 172.17.0.1:8083 | minicpm + mmproj | 看图（`auxiliary.vision`） |

- 端口绑 `172.17.0.1`（docker 网桥网关）而不是 `0.0.0.0`：容器之间能通，局域网/外网碰不到。
- 模型目录挂 `:ro`，容器不许改模型。
- 显存账（RTX 3060 12G）：8B 权重 5.3G + 8k KV(q8_0) ≈1G + embed 2.7G + vision 1.8G ≈ **10.8G**，每实例还要 CUDA 上下文 ~0.3G → 余量只剩几百 MB。三个全常驻很紧，稳的做法是只让 extract 常驻，embed/vision 用时再 `up`（写个「起容器→等 /health→调用→stop」的小封装）。
- 显存不够先砍 `-c`（8192→4096）和 KV 量化（q8_0→q4_0），不要硬起。

### ⚠️ 两个必须记牢的边界

- **容器内存上限 ≠ 显存上限**：`mem_limit` 管系统内存。显存由 `-ngl`、`-c`、`--cache-type-k/v`、batch 决定。想「只用显存不吃内存」，唯一前提是**显存真够**——够，权重和 KV 全在显存，RAM 只剩 CUDA 运行时/tokenizer/HTTP 那层壳（几百 MB）；差一点，有层落回 CPU 内存，低 mem_limit 立刻 OOM（看 `docker inspect` 的 `OOMKilled` / 退出码 137）。
- **mmap 保持默认开着，别加 `--no-mmap`**：mmap 的权重是可回收页缓存，内核内存紧张时直接丢掉；关掉反而把权重真读进 malloc 的内存里，RSS 暴涨——Ollama 那份 `use_mmap` 补丁要打就是同一个机制。

### 切换后端的验收门槛：向量一致性

向量库里存的是**旧后端**出的向量，不同实现的池化/归一化哪怕细微差别，既有记忆的召回都会静默变差。**切之前先证明对得上**：同一句话分别打旧后端 `/api/embed` 和新后端 `/v1/embeddings`，算余弦相似度，**要原始数字**（>0.999 视为一致；维度不同直接判不合格）。抽取用的 chat 后端只要格式稳就行，没这道门槛。

### 迁移顺序

搬模型 → 起实例逐个验证（`/health`、真实 chat、多模态、embedding 余弦）→ 主人调容器内存上限 → 复验 → **最后**才改 Hindsight 的 base_url 并停掉旧服务常驻。

### 运行参数与实测基准（照抄这一套）

`llama-server` 有几个默认值会让占用和表现不符合预期，compose 里显式钉住：

| 参数 | 为什么 |
|---|---|
| `--parallel 1` | 默认 `total_slots=4`（`/props` 可查）——上下文被切成 4 份、KV 按 slot 分配，既浪费显存又让单个请求拿不到完整 `-c`。串行消费方（Hindsight、看图）一律钉 1 |
| 覆盖镜像自带的 HEALTHCHECK | 镜像探针写死在**默认端口**，`--port` 挪到 8081 后容器会一直显示 `unhealthy`，但服务其实是好的（手打 `/health` 是 200）。compose 里按新端口重写探针，否则监控/编排会误判成故障 |
| `--host 0.0.0.0` + 宿主 `ports:` 绑 `172.17.0.1` | 容器内必须监听 0.0.0.0（绑 127.0.0.1 容器外打不到）；宿主侧绑网桥网关限住暴露面 |
| `--flash-attn on` + `--cache-type-k/v q8_0` | 砍 KV 显存；一个实例 8k 上下文约 1G |

**实测基准（RTX 3060 Laptop 12G，qwen3:8b Q4_K_M）**，留给下次对照：

| 项 | 实测 |
|---|---|
| prompt eval（861 token） | 514 tok/s |
| 生成（130+ token） | 43.4 tok/s |
| 显存 | extract 5510 MiB / embed 3968 MiB |
| 容器 RSS | extract 123 MB / embed 77 MB → `mem_limit` 给 1–2G 绰绰有余（mmap 的权重页缓存不计入 RSS） |

### embedding 后端切换的实测验收样例（Ollama → llama.cpp，qwen3-embedding:4b）

- 余弦相似度 0.9983~0.9997（平均 0.9990）；两边都是 2560 维且已是单位向量
- 跨句相似度绝对偏差 最大 0.0071 / 平均 0.0025；**最近邻排序完全一致**
- 同一输入重复 3 次结果并非 bitwise 相同（两两 0.99935）——GPU kernel 累加顺序导致，属正常，不是 bug
- 结论：差异来自实现差异而非配置错误，**既有向量库不用重建**。判别口径是**召回排序一致**，不是单个相似度的绝对值

## 三、生成类（生图/生视频）完全另一条线

Ollama 做不到生成。

| | Ollama | ComfyUI |
|---|---|---|
| 干什么 | 理解（文字↔理解） | 生成（文字/图 → 像素） |
| 模型 | LLM/VLM，GGUF | 扩散模型（SD/FLUX/Wan），safetensors + VAE/CLIP |
| 交互 | API 一问一答 | **节点式画布**：连线搭工作流（无对话） |
| 显存 | 12G 跑 8B | 12G 跑 SD1.5/SDXL/FLUX 低配 |
| 玩法 | 聊天、抽取、看图 | 图生图、ControlNet、LoRA、高清放大、文生视频 |

```
Hermes ──→ Ollama（理解）
       └─→ ComfyUI API（生成）  ← 可被 agent 调用出图
```

「无审查生图」的核心在**模型本身**（社区微调的扩散模型不审查 + LoRA 控风格），ComfyUI 只是不管内容的工作台。

## 四、落地现状与验收命令（已切过去，别重复问「切没切」）

- 三个实例已**常驻**：`llama-extract` 8081（extract 2g 上限）/ `llama-embed` 8082（1g）/ `llama-vision` 8083（688MB）；compose 在 `/opt/data/llamacpp/docker-compose.yml`（= 宿主同目录，两边同一个文件）。
- **ollama 已停用**：`trim-cli app status ai_installer` → `control.isOpen=false`，宿主 :11434 无监听。「现在到底谁在服务」用 `/props` 判——那是 llama.cpp 专属接口，ollama 不实现（`/v1/models` 两家返回长得很像，容易看错）。
- Hindsight 接线（以容器 env 为准）：`HINDSIGHT_API_LLM_BASE_URL=http://172.17.0.1:8081/v1`、`HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL=http://172.17.0.1:8085/v1` + `..._DIMENSIONS=1024`。
- **维度差靠截断代理补**：llama.cpp 的 `/v1/embeddings` 忽略 `dimensions`、恒返 2560，Hindsight 校验 1024 → 8085 放一层 2560→1024 截断代理。验收两行：同一请求打 8082 应得 2560 维、打 8085 应得 1024 维。
- **读容器内存别被 `docker stats` 吓到**：cgroup 把只读 mmap 的模型文件算进 page cache，extract 会常年显示「2047/2048 MB」顶到上限；真实匿名内存只有 290~400 MB（page cache 可回收，不构成 OOM）。要判真实占用看 `/proc/<pid>/status` 的 `VmRSS`/anon。
- 资源实测（三实例常驻）：宿主 used ~6.6 GB / 16 GB、available ~10 GB；三实例 RSS 合计 ~2.7 GB；显存仍是主约束（extract 5.5 GB + embed 4.0 GB ≈ 9.5 GB / 12 GB）。
