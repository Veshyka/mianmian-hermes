# 无外网环境的 HF 陷阱 & "哪个 env 让服务崩" 的定位法

来源：2026-10-02 夜排查 Hindsight 崩溃重启循环（线上真实事故 + 探针容器复现验证）。

## 一、教训：本地已缓存的模型，启动时仍可能联网 → 服务直接起不来

**现象**：给 Hindsight 加 `HINDSIGHT_API_RERANKER_PROVIDER=local` 后，容器每 ~5 分钟被 restart policy 拉起一次（`RestartCount` 递增、API 端口一直不通），日志尾部看似停在成功处：

```
... Reranker: initializing local provider with model cross-encoder/ms-marco-MiniLM-L-6-v2
... Reranker: applied transformers 5.x compatibility patch for XLM-RoBERTa
Loading weights: 100%|██████████| 105/105
```

**真相**：`Loading weights: 100%` 是**本地缓存加载完成**，不是启动成功。紧接着 tokenizer 走了一条**联网**路径：

`cross_encoder.py:220 CrossEncoder(...)` → transformers `tokenization_auto` → `utils/hub.py:147 list_repo_templates` → `huggingface_hub.list_repo_tree` → httpx GET → `httpcore.ConnectTimeout: [Errno 110] Connection timed out` → `Application startup failed. Exiting.`

**通用规则**：没有外网（或被墙）的机器上，任何走 transformers / huggingface_hub 的组件都可能**即使权重已在本地缓存**也去联网列 repo 文件而炸。

**修复**（对该组件所在容器加两个 env）：

```yaml
- HF_HUB_OFFLINE=1
- TRANSFORMERS_OFFLINE=1
```

实测：加上后 `/health=200`、`RestartCount=0` 连续 9 分钟稳定。

**顺带否掉的偏方**：调大 `HINDSIGHT_API_MODEL_INIT_TIMEOUT`（试到 1800）**无效** —— 抛的是硬 ConnectTimeout，不是超时器能兜的。

## 二、方法：用一次性探针容器隔离“哪个 env 让服务崩”

不要**在线上逐项试**环境变量（每次试错都真的断一次服务；本人已付过一次代价：4 个 env 一起加 → 服务崩 1 小时）。改用探针容器：

1. 同一镜像起一次性容器，名字统一前缀（如 `hs-probe-`）+ **空闲端口** + **/tmp 临时数据目录**，**绝不挂载线上数据卷**。
2. 其余 env 从线上容器 `docker inspect` 拷过来保持一致，只改被测的那一项。
3. **多个变体同时起**（基线 BL、单变量 A/B、组合 C），一起观察 —— 比串行快得多，还能互相校正。
4. 观察时长 ≥ 线上崩溃间隔的 2 倍（本例线上约 5 分钟崩一次 → 观察 13 分钟）。
5. 判据：`docker inspect` 的 `RestartCount` / `ExitCode` / `State.StartedAt` + `/health` + 日志尾部。
6. 收尾：按前缀守卫先回显确认、再 `docker rm` + 删 `/tmp` 目录，列“已清理”清单；**全程不碰线上容器**。

### 假阳性陷阱（血泪）
冷启动可能比健康检查窗口还久。串行轮里一个**无害**的变体因为首次启动超过 300s 健康窗，被误判成“崩”。修法：把启动等待调大（如 `STARTUP_WAIT=1800`）后重跑受控对比，才能分清“真崩”与“启动慢”。**没排除假阳性前不要下结论。**

## 三、Hindsight 队列/积压的口径（配合使用）

- 判积压看 API `/stats` 的 **`pending_consolidation`**（不要用 `pending_operations`，它含每次 retain 生成的无载荷父聚合器）。
- `[PENDING_BREAKDOWN]` 里 `batch_retain ... payload_null=N claimable=0` 是**正常设计**（父聚合器无 payload，子跑完自行收尾），不是“僵尸”，**不要 DELETE**。
- 容器里**没有 psql**、也没有 `HINDSIGHT_API_DATABASE_URL` —— 想直接查库这条路走不通，老实走 API。
