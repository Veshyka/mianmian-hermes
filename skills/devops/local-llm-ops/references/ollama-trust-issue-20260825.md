# Ollama 托管崩溃修复 + MinerU 显存互斥（2026-08-25）

## 场景：应用中心「Ollama 疯狂崩溃」（APP_CRASH 循环）

### 症状
- 飞牛面板显示 ai_installer 崩溃（TRIMEVENT APP_CRASH，启动后 7-40 秒判崩）
- Ollama API 可能实际可用（11434 通）——崩的是应用中心托管状态

### 根因（排查链）
1. **手动/trim-cli 启动的 ollama 成了孤儿进程**（PPID=1，不在 trim_app_center CGroup）——应用中心认为自己管理的进程没在跑
2. 应用中心再启动 → 新实例 bind 11434 失败（`address already in use`）→ 退出 → APP_CRASH
3. **ai_installer 是 Ollama + Open WebUI 捆绑**：open-webui 也有残留实例抢 11436 → 新实例 bind 失败 → 应用中心判整个应用崩

### 修复（干净状态托管启动）
```bash
# 1. 全清残留：ollama.real serve + llama-server + open-webui（ps 匹配后逐个 kill）
# 2. 确认 127.0.0.1:11434 和 11436 释放（172.17.0.1:11434 是 relay 服务，正常保留）
# 3. 容器内 trim-cli app start ai_installer（干净状态，应用中心完整托管）
# 4. 验证：进程起来了 + 无新增 APP_CRASH（journalctl --since 查）+ 11434/11436 都通
```

### 规则
- **启停 Ollama 一律走 trim-cli / 面板，不手动 nohup**（手动 = 孤儿进程 = 下次托管启动端口冲突）
- 判断进程归属看 PPID/CGroup，别只看 ps
- 应用中心把捆绑应用当整体判崩——组件端口冲突会连坐

## MinerU（vllm）与 Ollama 显存互斥

### 症状
MinerU 解析任务 failed：`RuntimeError: Engine core initialization failed`（vllm 无法初始化）

### 根因
vllm 需要 ~5G 显存，但 Ollama 8b+embedding 占着 9.5G/12G（只剩 2.7G）

### 修复
```bash
# 卸载 Ollama 模型释放显存（keep_alive=0 立即卸载）
for m in qwen3:8b qwen3-embedding:4b; do
  curl -s http://127.0.0.1:11434/api/generate -d "{\"model\":\"$m\",\"keep_alive\":0}"
done
# 显存全释放后重试 MinerU 解析 → completed
```

### 规则
- MinerU 与 Ollama **显存互斥**（12G 卡装不下同时跑）——错峰：用 MinerU 前卸载 Ollama 模型；MinerU 按需 `docker start/stop mineru-api`（容器保留，随时启用）
- Ollama 模型卸载后 Hindsight retain 会自动重新加载 8b（正常，稍慢）

## 部署：MinerU 自构建（ghcr 拉不了）

- 官方预构建镜像不可直接拉（docker hub 无此仓库、ghcr 需要认证）——**自构建**：`docker build -t mineru:latest -f docker/china/Dockerfile .`（DaoCloud vllm 基础镜像 + 阿里云 PyPI + ModelScope 模型源，全程国内）
- 运行：`docker run -d --name mineru-api --restart unless-stopped --memory 8g --gpus all -p 8000:8000 --ipc host --ulimit memlock=-1 --ulimit stack=67108864 -e MINERU_MODEL_SOURCE=local mineru:latest mineru-api --host 0.0.0.0 --port 8000 --gpu-memory-utilization 0.4`
- API：`POST /file_parse`（同步，multipart files=）、`POST /tasks` + GET 轮询（异步）；health `/health`
- **大小解释**：1.2B 模型本身 ~2.4G（bf16），镜像大是因为 vllm 框架 ~10G + 全家桶辅助模型（版面/公式 OCR/表格）~5G
