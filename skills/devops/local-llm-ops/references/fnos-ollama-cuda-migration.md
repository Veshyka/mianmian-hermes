# fnOS Ollama CUDA 迁移实录（2026-08-25）

## 场景
飞牛 NAS（15G 内存 + RTX 3060 12G）上 Ollama 0.30.7 由应用中心托管，Hindsight 容器（host network + privileged）通过 `172.17.0.1:11434` 调用 minicpm-v4.5 抽取 + qwen3-embedding:4b。

## 问题
宿主 available 仅 3G。minicpm 的 llama-server **VmRSS 8.2G**（Vulkan 后端 `--no-mmap`，权重全量进 RAM）。

## 诊断链路
1. `ps aux` 看到 ollama serve（PID 3682，root，应用中心拉起）
2. `tr '\0' ' ' < /proc/3682/environ` → `OLLAMA_VULKAN=1 OLLAMA_IGPU_ENABLE=1 OLLAMA_HOST=127.0.0.1:11434`
3. `tr '\0' ' ' < /proc/<llama-server>/cmdline` → `--no-mmap --flash-attn auto -c 4096 -np 1`（--no-mmap = Vulkan/CPU 特征）
4. `ldd` / `ls lib/ollama/` → cuda_v12 + cuda_v13 库齐全（`libggml-cuda.so`）
5. **临时实例验证**：`OLLAMA_VULKAN=0 OLLAMA_HOST=127.0.0.1:11435 ollama serve`（复用同一 OLLAMA_MODELS），加载 0.6b → 日志 `inference compute id=0 library=CUDA compute=8.6 name=CUDA0 ... RTX 3060` ✅
6. 清理临时实例用精确 PID（pkill -f 会匹配到 SSH 命令自身 → 自杀）

## 切换（wrapper 方案）
```
# /vol2/@appcenter/ai_installer/ollama/bin/
cp -p ollama ollama.real                          # 备份原二进制
# 覆盖 ollama 为 wrapper（先 kill 进程，否则 Text file busy）：
#!/bin/bash
export OLLAMA_VULKAN=0
export OLLAMA_CUDA=1
export OLLAMA_KV_CACHE_TYPE=q8_0
exec /vol2/@appcenter/ai_installer/ollama/bin/ollama.real "$@"
# 启动（手动，带完整 env）：
OLLAMA_MODELS=/vol2/@apphome/ai_installer/models OLLAMA_IGPU_ENABLE=1 \
OLLAMA_HOST=127.0.0.1:11434 OLLAMA_API_BASE_URL=http://127.0.0.1:11434 \
nohup $BIN/ollama serve >> /vol2/@appdata/ai_installer/ai_installer.log 2>&1 &
```
回滚：`mv ollama ollama.wrapper.bak && mv ollama.real ollama`

## 结果对比
| 指标 | Vulkan | CUDA |
|---|---|---|
| minicpm VmRSS | 8.2G | 1.9G（8192 ctx 时）|
| 宿主 available | 3G | 7.3G |
| 显存（含 embedding 加载）| 10.8G | 10.4G |

自启：宿主重启后应用中心 fork 的 ollama 走 wrapper（CUDA 自动生效）；⚠️ 应用中心更新会覆盖 wrapper。

## 附带修复：Hindsight JSON 截断
症状：Hindsight retain 频繁 `JSONDecodeError: Unterminated string`，4 次重试全挂 → retain 丢记忆。截断位置随机（727/825/1116/1393 chars），`prompt_eval_count + eval_count` 远小于 4096。

根因：Hindsight 大 chunk 输入（~3500 tokens）吃满默认 num_ctx=4096，输出空间被 clamp。

修复：
- Hindsight 容器加 `HINDSIGHT_API_LLM_OLLAMA_NUM_CTX=8192`（docker run -e，重建容器；脚本 `/opt/data/scripts/hindsight_switch_model.sh`）
- wrapper 加 `OLLAMA_KV_CACHE_TYPE=q8_0` 抵消显存增长（KV 减半）
- 验证：`/api/ps` 显示 `context_length: 8192`；重跑 retain parse error 归零（33 条事实大 chunk 成功）

排查注意：max_tokens 配置在 Hindsight 是 `max_tokens: int = 4096`（interface/http.py 默认），retain 不走 response_schema（"not yet supported"）——所以截断主因锁定 num_ctx。

## 进程归属判断（血泪教训）
Hindsight 是 host network + privileged 容器 → 容器内进程在宿主 `ps` 可见（shy 用户 `/app/api/.venv/bin/hindsight-api`），且 PID 是宿主视角。曾误判「宿主旧版在服务、docker 版空转」→ `docker stop hindsight` → 8888 直接挂 → 发现该进程就是容器内进程。
**正确判断方法**：`docker top hindsight` / 看 `/proc/PID/cgroup` / 看 PPID 链；不确定就 `docker stop` 前先看 8888 是否由容器端口映射独占（host network 时无映射，直接看进程 env 里的容器特征路径）。
