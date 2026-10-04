# 模型抽取质量测试记录（2026-08-25）

测试文本（因果链文本）：空调坏→维修费800→决定不修→高温中暑→找小张→学校免费维修→修好→请奶茶→意识到学校服务重要。

## 单条对比（Ollama chat 接口，同 prompt，temp 0.2）

| 模型 | 事实数 | 因果边 | 实体 | 用时 | JSON |
|---|---|---|---|---|---|
| qwen3:8b | 6 | **5（链全对）** | 17 | 86.7s | 稳（1883 tokens 完整） |
| qwen2.5:14b | 9 | **8（更细）** | 16 | 79.1s | 稳 |
| minicpm-v4.5 | 5 | **0** | 13 | 8.1s | 稳（但忽略 causal） |

第二条文本（熬夜→免疫力↓→淋雨→发烧→肺炎→住院→错过答辩→成绩↓→决定不熬夜）：

| 模型 | 事实数 | 因果边 | 备注 |
|---|---|---|---|
| minicpm-v4.5 默认 | 5-7 | 0 | 即使强指令（"You MUST output ALL causal links"）也 0 |
| minicpm-v4.5 + few-shot 中文示例 | 11 | 7-8 | 能提！但 relation_type 乱写（result_of/because_of 非规范 caused_by） |
| minicpm-v4.5 + 强调段（V2 中文增强） | 10-11 | 7-9 | relation_type 全 caused_by，但部分 target_fact_index 自我引用/错位 |

## prompt 增强实验（minicpm，全部无效于真实管线）

| 方案 | 位置 | 单测 | Hindsight 真实管线 |
|---|---|---|---|
| RETAIN_MISSION 注入 | prompt 开头 | 有效（短 prompt 场景） | Causal links 0 |
| CAUSAL_RELATIONSHIPS_SECTION 中文版（改容器代码） | prompt 末尾 | 有效 | Causal links 0 |
| custom mode（HINDSIGHT_API_RETAIN_EXTRACTION_MODE=custom + CUSTOM_INSTRUCTIONS） | 全 prompt | 有效（格式更稳） | Causal links 0 |

**结论**：minicpm 短 prompt 能提因果（模型有能力），但 Hindsight 完整长 prompt（几百行模板）下**直接忽略 causal_relations 字段**。3 种 prompt 工程均无法救回 → 换纯文本 qwen3:8b（真实管线 Causal links 5/5）。

## Hindsight 真实管线验证（最终）

| 模型 | Extract facts | Causal links | parse error | 耗时 |
|---|---|---|---|---|
| qwen3:8b + NUM_CTX=8192 | 6 | **5** | 0 | ~50s |
| qwen2.5:14b + MAX_LOADED_MODELS=1 | 6 | **5** | 0 | ~40s |

## 显存预算（RTX 3060 12G）

- 8b（5.4G VRAM）+ embedding 4b（3.9G）+ face_det（0.5G）= 9.8G ✅ 同驻
- 14b（~9G）+ embedding 4b（3.9G）= 13G ❌ 爆 → 需 `OLLAMA_MAX_LOADED_MODELS=1` 互斥（recall 实测 11.4s 含 embedding 重载）
- 14b 测试时 embedding 加载峰值 10.4G/11.6G（KV q8_0 后）安全

## 结论（8/25 晚定案）

- Hindsight 抽取 = qwen3:8b（因果 5/5、无切换代价、主人拍板）
- 看图 = minicpm-v4.5（Hermes 独立调用）
- qwen2.5:14b 留库备用（9G 磁盘，不占显存）

## ornith-1.5:9b 深度评估（2026-09-07，已弃）

qwen3.5 架构 VLM（projector 456M）。测试用贴近 Hindsight 真实 custom-mode prompt（银行中文 CI + 因果段 + schema 注入，system ≈9.1k 字符；num_ctx 8192，temp 0.1）——**与单条 test_extract 完全不同**。

| 模型 | 文本 | JSON | 事实 | 因果边 | 备注 |
|---|---|---|---|---|---|
| ornith-1.5:9b | A 因果链 | ❌ | 0 | 0 | thinking 失控：eval 5248 tok 全思考、content 空、done=length；8192/16384 ctx、think on/off 均复现 ×3 |
| ornith-1.5:9b | B 流水账 | ❌ | 0 | 0 | 同上（eval 5235, think≈4741 tok, out 0B）~127s 空转 |
| qwen3:8b | A 因果链 | ✅ | 7 | **5 有效** | done=stop 完整，266s（含冷加载），实体 26 |
| qwen3:8b | B 流水账 | ✅ | 10 | **0（正确不编造）** | 唯一弱因果「设闹钟怕误车」未强连，62s |

**机制**：qwen3.5 推理架构的长 prompt 下 thinking 永不收敛烧光预算 → 0 字节产出。**单条短测全绿（9 事实/8 边/91s）纯属假象**。另发现 **ollama `think:false` 对 qwen3.5 架构无效**（flag 被无视，反而更糟）。

**顺带**：qwen3:8b 对真实流水账判 0 因果边是**正确行为**（不编造弱因果）——评估模型时区分「该抽没抽」与「正确地不编造」。

**vision 确认**：`ollama show` capabilities 含 vision + Projector clip 456.01M；32×32 红 PNG 实测 2s 答"红色" ✅。ornith 是带视觉的 qwen3.5 推理模型，qwen3.5 系 think 不可关 → 任何 qwen3.5 系 VLM 做抽取都要先过真实长 prompt。

**显存**：ornith 单驻 5234 MiB；+ embedding 4b 同驻 9286/12288 MiB 余 3G 可同驻——但模型不可用，无意义。

## 12G 视觉候选调研（2026-09-07，供主人实测挑选）

RTX 3060 12G + Ollama 0.30.7，量化后 ≤7GB 才稳（留 KV + embedding 同驻余量）。pull 命令均核实自 ollama.com library：

| 候选 | pull 命令 | 权重 | 定位 |
|---|---|---|---|
| Qwen3-VL 8B（首选双修） | `ollama pull qwen3-vl:8b-instruct` ⚠️别用默认 tag | 6.1GB | 最新代 Qwen 视觉旗舰，官方称纯文本对齐 Qwen3 语言模型——唯一有机会「看图+抽取一鱼两吃」 |
| Qwen2.5-VL 7B | `ollama pull qwen2.5vl:7b` | 6.0GB | 老一代最被验证：文档/表格/发票结构化 JSON 官方强项，稳妥对照组 |
| MiniCPM-V 4.5 8B | `ollama pull minicpm-v4.5` | 6.1GB | 8B 档视觉第一梯队；已实测抽取因果恒 0，只配看图 |
| Gemma 3 4B | `ollama pull gemma3:4b` | 3.3GB | 最轻带视觉款，视觉中档，中文/抽取一般 |
| Gemma 4 12B | `ollama pull gemma4:12b` | 7.6GB | 2026 新代 dense 视觉，略超舒适线，需与 embedding 互斥切换 |
| GLM-OCR | `ollama pull glm-ocr` | 2.2GB | 文档 OCR 专用（OmniDocBench 94.62），替代 OCR 步骤 |
| Qwen3-VL 4B/2B | `ollama pull qwen3-vl:4b` / `:2b` | 3.3/1.9GB | 轻量视觉选项 |

**排除**：qwen3-vl 默认 tag（`:latest`/`:8b`/`:8b-thinking` 同 digest = thinking 版，须用 `:8b-instruct`）；gemma3:12b 8.1G ✗；gemma4:e4b 9.6G ✗ / e2b 7.2G 边缘；deepseek-ocr:3b（名 3B 实 6.7GB 仅 8K ctx）；llama3.2-vision:11b ≈7.9G 超预算中文弱；glm4v/glm4.1v 官方库 404（GLM-4.1V-Thinking 纯思考架构 = qwen3.5 同款雷）；InternVL3-8B 需走 HF GGUF 非官方（视觉精度早期有翻车报告）。

**版本门槛**：qwen3-vl 需 Ollama ≥0.12.7、qwen2.5vl ≥0.7.0、gemma3 ≥0.6——0.30.7 全过。**所有 VLM 测抽取必须先过真实 Hindsight 长 prompt dry-run**，单条短测不作数。256K 上下文双刃剑：显式设 num_ctx 8192 + KV q8_0。

## 待验证假设：推理参数从未系统调过（2026-09-07 主人提出）

历史测试全部固定 `temperature 0.2`（test_extract.py）/ `temp 0.1`（深度评估），**从未扫过温度/思考强度矩阵**。低温「抽取要确定性」是直觉未经验证。方向：同模型跑 温度 {0, 0.2, 0.7, 1.0} × 思考 {on/off}，看因果边数/JSON 稳定性变化。低温可能让模型不敢抽因果边（因果是推理不是复述）；高温度 + format 强制可能救回 qwen3.5:9b 类编号列表问题。下次选型试新模型前先跑这个矩阵，别直接判死刑。
