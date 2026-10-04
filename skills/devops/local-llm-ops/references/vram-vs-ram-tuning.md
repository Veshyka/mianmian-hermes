# 本地 LLM 的显存/内存调优（实测流程）

触发场景：显存吃紧、容器被 OOM 杀、要「省内存但保证功能」。

## 0. 先分清两笔账（最容易搞错）

| | 受什么限制 | 怎么量 | 怎么省 |
|---|---|---|---|
| **显存 VRAM** | `-ngl` / `-c` / `--parallel` / KV 类型 | `nvidia-smi --query-compute-apps=pid,used_memory --format=csv` | 降 ctx、KV 量化、少 offload |
| **系统内存 RAM** | cgroup `mem_limit` | `dmesg \| grep "Killed process"` + `docker inspect --format '{{.HostConfig.Memory}}'` | 抬上限 / 降 batch |

显存**不占** cgroup memory。容器 OOM 杀进程（`dmesg` 里 `Memory cgroup out of memory: Killed process <proc> ... anon-rss:NNNkB`）永远是 **RAM** 问题。

## 1. 别拿 pid 猜服务身份

2026-09-22 踩过：按「pid 新的 = 刚重启的那个」推断，把 extract 的 7.2GB 说成是 embed 的。**认服务看端口 / cmdline**：
```bash
for P in $(nvidia-smi --query-compute-apps=pid --format=csv,noheader | tr -d " "); do
  tr '\0' ' ' < /proc/$P/cmdline | cut -c1-120; echo
done
```

## 2. 量化 KV 的收益必须实测（算术会给错答案）

KV 体积可以按 GGUF 元数据算准：

```
KV/token = \"<SECRET>\" × n_kv_head × (key_length + value_length) × 字节( fp16=2 )
```

解析脚本：`/opt/data/ops-backups/gguf_info.py`（只读 GGUF 头，不加载模型）。
例：Bonsai-27B（64 层 / KV heads 4 / 256+256）→ 256 KiB/token → -c 8192 时 KV 2.0 GiB。

**但 fork/新实现的实测收益可能远低于算术值**：Bonsai 的 PrismML fork 上 `--cache-type-k/-v q8_0` 只省 **240 MB**（算术预期 1.0 GB）。→ **以 `nvidia-smi` 前后差值为准，并把实测值写进注释**，别写算术值。

## 3. 砍 ctx 前先量真实请求分布

```bash
docker logs <容器> 2>&1 | grep -oE "n_tokens = *[0-9]+" | awk '{print $3}' | sort -n | tail -5
```

- 2026-09-22 实测：`llama-embed` 342 次请求**最长 356 tokens**（典型 85–95）→ `-c 8192 → 2048` 安全（5.7 倍余量），显存 3972 → 3100 MiB。
- 同一轮实测：`llama-extract` 单次 retain 用到 **6488 tokens**（`truncated = 0`）→ **不能砍**，砍了就截断抽取输入、漏抽事实。
- 相关约束：Hindsight 的 `HINDSIGHT_API_REFLECT_MAX_CONTEXT_TOKENS`（本项目 6000）与 bank 的 `reflect_source_facts_max_tokens`（4000）会把可用下限顶住。

## 4. `mem_limit` 要按「加载期峰值」定，不是稳态

实测同一进程：**加载期 anon 峰值 4.25 GB，稳态只有 0.6 GB**。上限低于峰值 → 每次加载都可能被杀（`RestartCount` 涨、`docker inspect` 的 `OOMKilled=false`/`ExitCode=0` 会**骗人**，真死因只写在 dmesg）。

⚠️ **运行时的 `docker update --memory` 不落 compose** → 下次 `--force-recreate` 复位。两侧对不上时以『哪个在杀我』为准再统一：本项目最终把 compose 定成 `5g`（峰值 +0.75G 余量）。

## 5. 改完必须验「功能没坏」（只看进程活着不算）

1. **embedding 维度**：原生 vs 代理两条都要测（本项目 8082 → 2560，8085 截断代理 → 1024，Hindsight 走代理）
2. **召回**：`POST /v1/default/banks/<bank>/memories/recall {"query":...}` 返回条数 > 0
3. **真实抽取**：`POST /memories` 入队 → 看返回 usage + 抽出事实的**时间戳/实体/document_id** 是否正确（本项目 43s 同步完成、3 条全对）
4. **残留清理**：测试写的文档按文档级联删（`DELETE /documents/{id}` 会带走其记忆；`DELETE /memories/{id}` 返回 **405**，别当成失败重试）。测试造的假事实（如「已下单 X」）必须删干净，否则会长期污染召回。

## 6. 回滚

改 compose 前备份到项目 `tmp/`（目录可能是 root 属主，写不进去就备份到 `/opt/data/tmp`），回滚 = 改回参数 + `docker compose up -d --force-recreate <服务>`。重启加载模型要 1–3 分钟，期间该后端不可用。
