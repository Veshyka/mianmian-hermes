# 候选小模型：真伪核实 + CPU 基准配方

适用：主人贴来一份「小模型选型清单」（常是 AI 生成的），要求核实哪些真实存在、并把候选拉下来在本机量输出服从度 / 延迟 / 判断合理性，且零 API 花费。

## 1. 核实通道（HF 直连在本机不可用时换镜像，不是「查不到」）

| 目标 | 通道 |
|---|---|
| HF 模型元数据 / 文件清单 / 体积 | `https://hf-mirror.com/api/models?search=<kw>&limit=20`、`?author=<org>&search=<kw>`、`/api/models/<repo>?blobs=true`（带每个文件 size） |
| HF 数据集 | `https://hf-mirror.com/api/datasets/<repo>` |
| HF README | `https://hf-mirror.com/<repo>/raw/main/README.md` |
| GitHub 仓库元数据（stars / license / pushed_at / language） | `https://api.github.com/repos/<owner>/<repo>`（直连可用） |
| GitHub 仓库正文 / 单文件 | `https://api.github.com/repos/<owner>/<repo>/readme` 或 `/contents/<path>`，再 `base64.b64decode(d['content'])` |
| GitHub 组织全量仓库 | `https://api.github.com/orgs/<org>/repos?per_page=20` |

- **拿不到直连就换通道，别据此下「不存在」的结论**。本机 HF 直连与 `raw.githubusercontent.com` 都不通（超时 / HTTP 000），而 hf-mirror 与 `api.github.com` 都是 200——同一份信息只是换了门。
- **搜不到某个规格先换关键词再下结论**：宽泛关键词只返回该家族的几个热门尺寸（搜 `Qwen3.5` 只出 9B/2B/4B，带上 `.8B` 才命中 0.8B）；用 `?author=<org>&search=` 列全家族比逐个 search 可靠得多。
- **组织名前缀不能省**：漏掉 owner 的仓库名直查必然 404，那是「名字不全」不是「不存在」。同一作者常有 `X` / `X-mac` / `X-windows` / `XHarness` 多个仓，一次列 org 全量最省事。
- 权重体积按**实际文件 size** 报（`?blobs=true`），不要按参数量折算；同一模型不同量化差一倍以上。

## 2. 报告口径（主人明确要求）

- **结论先行**，再上表格；每个名字一行：存在？/ 客观指标（下载量 · likes · license · 创建时间）/ 声称的数据有无出处 / 备注。
- 无法溯源的说法（「社区标记为分类专家」「某 benchmark 588ms/272 tok/s」这类）一律写**「查不到可信来源」，并把搜过的平台与关键词单列一节**。绝不为了凑数把无关仓库当答案，绝不编造下载量或延迟。
- **模型真 ≠ 它卡片上的数字真**：同一份清单里模型多半能查到，但伴随的测试数字常常查无出处——两者必须分开判。
- **找到出处也要看样本量**：某仓库自述的「86.4%」实际口径只有 22 条回归样本，属作者自测，不能当独立验证；同时注意它可能只支持某个平台（如仅 Apple Silicon），我们复现不了。

## 3. CPU 基准配方（不装 torch、不自己编译）

用 llama.cpp 的**预编译 release 二进制**：

```bash
TAG=$(curl -s https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=15 \
  | python3 -c "import json,sys;print(next(r['tag_name'] for r in json.load(sys.stdin) if any('ubuntu-x64' in a['name'] for a in r['assets'])))")
curl -sL -o lc.tar.gz "https://github.com/ggml-org/llama.cpp/releases/download/$TAG/llama-$TAG-bin-ubuntu-x64.tar.gz"
python3 -c "import tarfile;tarfile.extractall('lc')"   # 容器常无 unzip，用 tarfile
```

- `latest` release **可能没有二进制附件**（只有一个 nightly-tag.txt），要往前翻几个版本挑带 `*-bin-ubuntu-x64.tar.gz` 的那个。
- 新版**删掉了 `-no-cnv`**（报 `invalid argument`），单轮生成改用 `-st`。
- **探速度和合规用 `llama-server`，不要用 `llama-cli`**：CLI 每次重载模型（延迟含加载）、还会打印交互横幅与 `[ Prompt: … ]` 污染 stdout；server 的 `/completion` 响应自带 `timings`（`predicted_per_second` / `prompt_per_second`）。
- **server 用后台任务起，别用 `nohup/setsid` 包装**（前台工具会拒绝 shell 级后台包装），起来后**先发一次 warmup 请求**再跑用例，否则首个请求可能慢几百倍。
- 换模型时**先确认上一台的 server 真的死了**（杀掉后端口可能仍被僵尸持有），并给每台用不同端口。

## 4. 判定「输出服从度」的两个必踩陷阱

1. **`n_predict` 必须大于 think 块的消耗**。Qwen3 系即使带 `/no_think` 也会先吐一段 `<think>…</think>`；预算给小了（实测 14）think 块吃光额度、**答案行根本没生成** → 会得出「格式服从度 0%」这个假结论。
2. **判定前先剥掉 think 块**。清理函数要同时处理 `<think>…</think>` 与 `[Start thinking]…[End thinking]`；只剥后者会让首行永远是 `<think>`，同样造出假的 0%。

正确口径**分两栏报**：① 严格「只输出一行」（存在 think 块即不合格）② 答案行格式合规（剥掉 think 后是否匹配目标格式）。两栏差异大时，结论是「需要给足预算 + 剥壳」，**不是「必须上语法约束」**。

- **GBNF/语法约束的价值是省预算 + 免解析容错，不是「没它就出不来」**。这条曾被写成硬结论并被复跑推翻，别再犯。
- **判断力才是分水岭**：格式合规普遍能到 100%，选型依据是判断命中率。
- **看目标槽位是否恒定**：模型多数用例都输出同一个候选（如 `TARGET=C1`），说明它没在候选间真做选择、只是把格式填满——这种「格式对、决策空」必须在报告里点名。
- 同一模型判 `ignore` 时输出短式、判 `text` 时输出完整式，**输出形状不唯一**，下游解析要兼容。

## 5. 结论发布前必须复现

- **延迟数字只在轻载时采信**。同机若有别的 agent 在同一端口段跑推理，load 会到 8 核 × 2，同一条件的 p50 能在 **0.67s ↔ 80s** 之间摆动（相差百倍），那不是模型差异。
- **先排除自己的残留进程**：`ps -eo pcpu,pid,args --sort=-pcpu`。同一模型起过两个 server 是最常见的自造争用；也别把别人的进程当自己的杀。
- **同一输入跨运行会翻结论**（实测某例一次判 text、一次判 ignore）。只跑一遍就报「命中率 X%」是过度自信——至少复跑一次，并说明两次的错例是否一致。
- 报告里把「重载测的延迟（污染）」与「轻载复跑」分列，写明哪个可信。

## 6. 长基准的进度汇报

- **边跑边出**：每完成一个用例就打印一行（`flush=True`）并落盘（`tee` / 写在后台任务里），不要只写最终汇总。
- 别静默跑几十分钟——会被追问「是不是太慢了」。宁可先给已得结果 + 明确还要多久。
- **收尾时回头检查所有后台任务的输出**：迟到的完成通知里可能带着之前被判定失败的**完整数据**，足以推翻一条已经发出去的结论。
