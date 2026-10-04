# Hindsight：换 embedding 模型 / 向量维度的完整实操

Hindsight 自身的用法/调优见 `hindsight-memory-engine` 技能（本文件只补「换维度/换模型」这条链）。

## 为什么不能就地改
向量列维度是**实例级**的：建 bank 时按容器的 `HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS` 定列
（`models.py` 的 `Vector(EMBEDDING_DIMENSION)`），且会校验返回向量长度。
→ **同一实例里无法并存 1024 和 2560 两个 bank**。换维度只能：起第二个实例 → 迁移 → 切流量。

## 官方迁移路径（唯一推荐，别自己写 SQL 改列或回填向量）
`hindsight-admin` 一进一出。⚠️ 它**不在容器 PATH 里**，必须绝对路径 `/app/api/.venv/bin/hindsight-admin`
（`docker exec hindsight hindsight-admin ...` 会 not found）。

**导出（只读生产，可随时跑）**
```bash
docker exec hindsight /app/api/.venv/bin/hindsight-admin export-bank \
  -b mianmian-history -o /home/hindsight/.pg0/export_<TS>.zip
```
- 带上 documents / facts(含文本) / observations / bank config / mental models / directives / webhooks
- **故意不含 embedding 和数据库 id**（源码原文：`Embeddings and database ids are deliberately omitted — they are regenerated/re-resolved on import`）
- 体量极小：1701 文档 / 27641 事实 / 438 观察 → **14 MB**
- 日志里 `Skipped N observation(s) with sources outside the exported documents` 属正常
- 要连审计/llm_requests 一起搬，加 `--include-history`

**导入（在配置成目标维度的实例上跑）**
```bash
docker exec <目标容器> /app/api/.venv/bin/hindsight-admin import-bank -a /import/export_<TS>.zip
```
- 用**目标实例的 embedding 模型重算向量**，并重建 links/indexes
- **不重跑 LLM 事实抽取**（事实文本随档案带过去）→ 属「分钟」级，不是「重抽几小时」
- `--target-bank` 可改目标 bank id；**目标 bank 必须不存在**（整库还原，不是合并）
- 档案要在容器内可达 → 宿主目录挂进去（`-v <宿主路径>:/import:ro`），参数传 `/import/xxx.zip`
- 别拿 `/export`（bank 模板导出）当迁移用

## 平行测试实例
脚本：`/vol1/1000/<USER>`
```bash
sudo bash hindsight_test_2560.sh                      # 只起实例
sudo bash hindsight_test_2560.sh import /import/export_<TS>.zip
sudo bash hindsight_test_2560.sh down                 # 拆容器，保数据
sudo bash hindsight_test_2560.sh down rm-vol          # 连数据一起删
```
与生产实例的差异：`PORT=8889`、独立 volume、embedding **直连 8082 拿原生 2560**（不需要 8085 截断代理）、
`dimensions=2560`、`--restart no`。起之前扫一眼主机可用内存：第二个实例自己的 postgres 要 ~1.5G。

## 切生产前必须量
1. 量尺指向测试实例：
   `HINDSIGHT_RECALL_API=http://172.17.0.1:8889/v1/default/banks/mianmian-history/memories/recall \
    python3 /opt/data/hindsight-eval/eval_recall.py --tag w2560`
2. 与生产结果 `--compare` 对比 hit@1 / hit@3 / 平均名次 / 耗时
3. **只有实测更好才迁移**；否则保留现状（已量过的基线更可信）

代价侧一起算：向量宽度变 N 倍 → 向量存储与相似度计算同步放大，而 auto-recall 是**每轮对话都跑**的固定开销。
反向收益：用原生宽度可**拆掉中间那个截断代理**，少一个会坏的环节。

## 与「截断代理」方案的关系
库列固定 1024 而模型原生 2560 时，中间代理（2560→1024 MRL + L2 归一化）是**零迁移**的权宜方案；
它额外要求代理**同时处理 float 列表和 base64**（写入路径发 float，检索路径发 base64 小端 float32），
只处理一种会让检索拿到 2560 维直接 500。迁移到原生宽度就是把这条链整个删掉。

## 维度/模型 A/B 的实测方法（决定「要不要迁」用这套）

真实事实文本按 `GET /v1/default/banks/<bank>/memories/list?limit=500&offset=N` 分页取（只读、快：3.5k 条约 1 秒，
样本覆盖最新切片，报结论时写明覆盖率），两个宽度各编码一遍，再余弦=点积（**先验端点返回模长=1.0000 才成立**）。

**先测量尺的分辨率，再信任何名次差**：同一个 batch 请求重复发几次，看返回向量是否逐位一致。
实测 8085(1024 截断) 端点**首次调用与后续调用分量最大差 8.98e-03**（同题余弦低到 0.9985），8082(2560) 端点逐位一致。
→ query 每个宽度**重复编码 ≥5 次并检查名次是否稳定**；不稳定就别把 ±1 名当结论。
（样本向量只编码一次，该量级噪声对两端是同等随机扰动、不偏向任一方，但它是分辨率下限。）

**别只报 hit@k，同时报两样**：①每题**正类数**（含期望关键词的事实条数）；②AUC（正类=含关键词的事实，
不受 top-k 截断影响）。**单正类时 AUC 由一条样本决定，泛词命中（如「主人」占 3496 条中的 2561 条）时名次 1 毫无区分意义**
——这两类题都要在报告里标出来，不能算作证据。

**必须做配对显著性检验**：逐题取 AUC 差（或名次差），配对 bootstrap 重采样题目算 95% CI。
10 道题的 CI 半宽 ≈0.038，远大于实测到的 AUC 差 0.017 → **CI 跨 0 就等于「测不出差别」**。
要下维度/模型迁移这种一次性判决，评测集先扩到 **≥100 道、正类数落在 5–200 条**的题。

**现成实现，照改端点即可，别重写**：`/opt/data/hindsight-eval/embed_width_ab/` ——
`fetch_samples.py`（只读抓样本）、`ab_embed_width.py`（双宽度编码 + 余弦 top-k）、
`analyze.py`（AUC / MRR / hit@k / top10 重叠）、`stats.py`（配对 bootstrap + Wilcoxon）、
`robust.py`（端点重复稳定性 + 5 次名次稳定性）、`final_report.py`（汇总，5 次取中位数）。
recall 端的 hit@1/hit@3 尺子是 `/opt/data/hindsight-eval/eval_recall.py`（支持 `--tag` / `--compare`）。

## 拆掉截断代理的四条路（选之前先看实测结论）

1. **迁到原生宽度实例**（官方 export/import，见上文）：能拆代理，代价是向量存储 + 检索向量运算 ×宽比。
   实测 2560 原生相对 1024 截断**没有判别力增益**（top1 命中 5/10 vs 5/10，9/10 题 top1 完全相同，
   最大反向是某题名次 10→37）→ 拆它的正当理由是「少一个会坏的环节」，**不是质量**。别拿质量当迁移理由。
2. **换服务层**：vLLM 的 `/v1/embeddings` **原生支持 `dimensions`**（服务端截断+归一化），把自研代理换成官方功能，
   模型/维度/存储都不变。**前提**：模型 `config.json` 要有 `is_matryoshka`；Qwen3-Embedding-4B **没有**这个键
   （`hidden_size`=2560），必须显式 `--hf-overrides '{"is_matryoshka": true}'`（或 `matryoshka_dimensions`），否则改维度直接 400。
3. **换原生低维模型**：Qwen3-Embedding-0.6B 原生就是 1024（同族、639MB、32K ctx）→ 拆代理且存储不涨，
   代价是质量降级（官方卡 C-MTEB 检索 71.03 vs 4B 77.03、MTEB 多语言 70.70 vs 74.60）。
   换模型 = 全库重嵌入，**换之前用上面这套量尺实测，别只信榜单分数**。
4. **「加个参数就不用代理」是堵的**：ollama 的 OpenAI 兼容端点**忽略 `dimensions`**（实测传 1024 仍回 2560），
   所以现状架构下这层省不掉——细节见 `local-llm-ops` 技能「已知陷阱」。
