# Ollama mmap 调度坑（2026-09-02 实测 · 内存暴涨真凶）

## 症状

- `ollama ps` 显示模型 **100% GPU**（37/37 层 offload，显存占用正常），但 llama-server **RSS 高达 8.7G**
- 宿主内存 available 只剩 1.8G、swap 用 3G，看起来像「分层卸载」
- **不是分层卸载**：显存里权重、KV cache 都在（nvidia-smi 9.5G/12G）

## 真凶：调度器内存压力误判，主动禁用 mmap

加载日志（ai_installer.log，Ollama 0.30.7）：
```
msg="disabling mmap for llama-server load due to host memory pressure"
model_size=4.9 GiB  loaded_mmap_size=2.3 GiB  headroom=7.5 GiB
system_free=10.6 GiB  system_total=15.4 GiB
```

源码逻辑（ollama main 分支 server/sched.go，0.30.7 行为一致）：
```go
// sched.go:1255
func mmapHostPressureHeadroom(totalMemory uint64) uint64 {
    return max(8*format.GigaByte, totalMemory/10)  // 硬编码 ≥8GB！
}
// sched.go:1227
func disableMmapForHostPressure(goos string, opts api.Options, systemInfo ml.SystemInfo,
    gpus []ml.DeviceInfo, modelSize, loadedMmapSize, predictedVRAM, availableVRAM uint64) bool {
    if opts.UseMMap != nil || goos != "linux" || ... || !allDiscreteGPUs(gpus) {
        return false
    }
    if predictedVRAM == 0 || availableVRAM == 0 || predictedVRAM > availableVRAM*80/100 {
        return false
    }
    pressure := modelSize + loadedMmapSize + mmapHostPressureHeadroom(systemInfo.TotalMemory)
    return systemInfo.FreeMemory < pressure
}
```

**判定公式**：`systemFree < modelSize + loadedMmapSize + max(8GB, total/10)` → 禁用 mmap → llama-server 加 `--no-mmap` 参数 → 权重整份读进 RAM 匿名内存 → RSS 暴涨。

本例：15.4G 内存 → headroom=8G；pressure = 4.9G(qwen3:8b) + 2.3G(已加载 embedding mmap) + 8G = 15.2G > 空闲 10.6G → 触发。

**小内存机器必然触发**：8B 模型 4.9G + 8G headroom 就 12.9G，15-16G 内存的机器加载 8B 模型几乎必中。用 MemFree 而非 MemAvailable 判断，page cache 全被无视（GitHub issue #15704 同款缺陷）。

## 修复：显式 use_mmap=true 绕过判定

源码 `opts.UseMMap != nil` 时直接返回 false（不触发压力判定）。`use_mmap` 是官方 Options 字段（api/types.go `json:"use_mmap,omitempty"`），Modelfile 也支持（parser_test.go 有 `use_mmap true`）。

**Modelfile 方案（Hindsight 零改动）**：
```
FROM qwen3:8b
PARAMETER use_mmap true
```
```bash
ollama create qwen3:8b -f Modelfile   # 覆盖同名模型，调用方不用改模型名
```
重启 ollama 后加载走 mmap：权重是文件映射 + page cache（可回收），RSS 预计 8.7G → 1-2G，显存占用不变，性能无损。

API 方案（调用方传）：
```json
{"model": "qwen3:8b", "options": {"use_mmap": true}}
```

## 验证

1. 重启前先记录基线：`free -h`、`ollama ps`、主模型 llama-server RSS（`ps aux --sort=-rss`）
2. 重启后：RSS 应显著下降（目标 <3G）；`ollama ps` 仍 100% GPU
3. cmdline 不应再有 `--no-mmap`
4. 回归：Hindsight retain/recall 正常、无 JSON 截断

## 附带教训（推翻旧判断）

- **`--no-mmap` 不能单独作为 Vulkan/CPU 特征**：CUDA 下调度器也可能因内存压力加 `--no-mmap`。判别后端要看：加载日志 `offloaded N/N layers to GPU` + `inference compute library=CUDA` + nvidia-smi 显存占用。
- 方案转帖核查：`NVIDIA_VISIBLE_DEVICES` 只对 Docker 有效（应用中心 systemd 托管不适用）；`OLLAMA_GPU_OVERHEAD` 默认就是 0；`OLLAMA_NUM_PARALLEL` 默认 1；`OLLAMA_KEEP_ALIVE=0` 对高频调用场景有害（每次请求后卸载重载 8B 模型要 10-30s）——转帖方案不能照搬，先对照本机部署形态。
