# Ollama 环境变量方案核查实录（2026-09-01/02）

## 背景
主人从外部转来一篇「飞牛fnOS(Docker-Ollama)+RTX3060-12G，内存暴涨10G+完整根治方案」，
要求对照真实环境核查可行性。核心教训：**外部 Ollama 优化方案常以 Docker 部署为前提，本机是应用中心 systemd 托管，照搬必踩坑**。

## 本机真实形态（实测确认）
- 部署：`trim_app_center.service`（systemd），进程 `ollama.real serve`（wrapper 已生效）
- 非 Docker → fnOS 网页端「容器环境变量」入口不存在，改 env 只能改 wrapper 脚本
- ollama 0.30.7，路径 `/vol2/@appcenter/ai_installer/ollama/bin/`
- `ollama ps`：qwen3:8b 100% GPU、qwen3-embedding:4b 100% GPU
- 显存：9576/12288 MiB（qwen3:8b=5518 + embedding=4044）
- 内存：15G 总，13G used，available 1.8G，swap 4G 用 3G

## 环境变量逐条核查（对照 GitHub ollama/envconfig/config.go）

| 变量 | 方案声称 | 核查结论 |
|---|---|---|
| OLLAMA_KEEP_ALIVE=0 | 对话结束立刻卸载释放内存 | ❌ 有害：Hindsight 高频调用抽取/embedding，=0 每次请求后卸载→下次重载 8B 10-30s，抽取慢到爆炸 |
| NVIDIA_VISIBLE_DEVICES=0 | 强制只用 3060 | ❌ 不适用：Docker/NVIDIA Container Toolkit 变量，非容器环境无效；单卡无需指定 |
| OLLAMA_GPU_OVERHEAD=0 | 关掉显存安全缓冲 | ⚠️ 无收益：envconfig 默认即 0 |
| OLLAMA_NUM_PARALLEL=1 | 禁止并行省显存 | ✅ 默认即 1（`NumParallel = Uint(..., 1)`），无需设 |
| OLLAMA_NUM_GPU=-1 | 全部层上 GPU | ✅ 等价已生效（ollama ps 100% GPU） |
| OLLAMA_FLASH_ATTENTION=1 | 降 KV 显存 | ✅ 已 `--flash-attn auto` 生效，可显式设 1 |
| OLLAMA_KV_CACHE_TYPE=q8_0 | KV 降精度 | ✅ wrapper 已内置（`--cache-type-k/v q8_0`） |

## 真实内存疑点（未决）
- qwen3:8b 主模型 llama-server：RSS 8.7G，cmdline 带 `--no-mmap`
- embedding llama-server：RSS 292M，无 `--no-mmap`
- 疑点：CUDA 下仍可能走 no-mmap 路径 → 权重 RAM 副本 + 显存副本双份
- 排查命令：`ps -o pid,rss,args -p <llama-server pid>` 看 cmdline 是否含 `--no-mmap`
- 注意：`--no-mmap` 在 skill 中原记为「Vulkan/CPU 特征」，但 CUDA 下 qwen3:8b 也出现了——判断后端不能只看 cmdline，要看 `OLLAMA_DEBUG` 日志 `inference compute ... library=CUDA`

## 待办落盘
完整 4 阶段执行计划（P0 基线已做 / P1 wrapper 微调 / P2 重启 / P3 验证 / P4 回归 + 回滚预案）：
`/opt/data/memory/2026-09-02-ollama-memory-optimization-plan.md`
