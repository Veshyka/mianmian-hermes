# Hindsight 记忆库：纠错与整备

## 常驻规则（主人定，适用于每一次发现错误结论）

> **写下错误结论后，必须把那条记忆标废弃——不能只补一条新更正。**

只补更正会让**错的和对的同时被召回**，等于没改。当下修正认知的同时，要把库里那条旧结论按下去。

## 接口：软废弃，不硬删

记忆对象本身就带三个字段：`state` / `invalidation_reason` / `invalidated_at`，直接 PATCH：

```bash
curl -s -X PATCH -H 'Content-Type: application/json' \
  -d '{"state":"invalidated","reason":"错误：<为什么错>；已于 <日期> 实测更正"}' \
  "http://172.17.0.1:8888/v1/default/banks/<bank>/memories/<memory_id>"
```

- **不要用 `DELETE /memories`**（那个不带领 = 清空整库）；也不要为了改个结论去 `DELETE /memories/{id}`。软废弃可逆、留痕，以后还能知道「当时为什么会那么想」。
- 可回看：`GET /memories/list?limit=200&state=invalidated`。
- ⚠️ **作废项在 `list` 里照样看得见（带 `state=invalidated`），不会凭空消失**——别把「还能 list 到」当没生效。真正的判据是**召回层面**（下节）。
- 现成工具：`/opt/data/scripts/mem_invalidate.py`（可复用；只 PATCH，永不 DELETE；支持 `--list-invalidated` 与 `--dry`）。用法：`mem_invalidate.py <id前缀> "<原因>" [<id前缀> "<原因>" …]`。

## 验证：读召回，不读 PATCH 的 200

PATCH 返 200 只说明**写进去了**，不证明**它不再误导**。验证要做两件事：

1. 用 `POST /memories/recall` 打**几个直指这些错条目的问题**（就用你当初会问出口的说法）；
2. 确认 ① 废弃项一条都不出现在结果里 ② **正确的事实浮到前面**。

返回键别搞混：`recall` 是 `results`，`list` 是 `items`。

## 找错条目：两条检索都要用

- `GET /memories/list?q=<关键词>` —— **子串**匹配，快；但**措辞不同的同一条捞不到**。
- `POST /memories/recall {"query":"…"}` —— **语义**检索，能捞出同一事实的改写版。**只有关键词搜不到时才换它，容易漏。**

同一件事在库里常是**一组**而非一条（原始结论 / 中间快照 / 事后更正），关键词只打一个会只捞到其中一层，**要把整组一起看再决定动哪几条**。

## 三类条目，只有一类该废弃

| 类别 | 例 | 处置 |
|---|---|---|
| **原始错误结论** | 「X 无法获取，因权限受限」 | **废弃**（reason 写清为什么错、何时以什么方式更正） |
| **过时中间态快照** | 「当前为 N 条」「某进程仍在运行」 | **废弃**（写的时候不假，现在误导召回；reason 注明「过时」并给出当前值） |
| **更正类记忆** | 「此前那条结论作废」 | **绝对不动**——它们是对的那一半 |

## 类级问题：中间态会持续产生（别无限追下去）

retain 会把**每一轮的进度絮语**（"当前为 N 条""进程仍在运行"）当事实存下来，于是中间态越攒越多、会持续误导召回。**清掉最显眼的一层，下一层马上从 recall 结果里浮上来。** 所以：

- 一次性清理只清**与当前话题直接相关**的那批，并**明确告诉主人「这是一个类，会持续产生」**，不要追着 recall 无限往下清（永远清不完，还容易误伤）。
- 上游压制（在 `retain_mission` / `retain_custom_instructions` 里禁止抽取任务进度、计数、进程/容器状态类中间态）**是治本手段，但仍必须先经主人点头再改**（它会改变抽取行为）。已验证的改法：**两份指令都要追加、且只追加不重写**（两份都进抽取 prompt，只改一份会让口径不一致）；改完做**双向 A/B** 再报结论（见下）。
- **改抽取指令的标准流程（含量尺，别再靠感觉说“效果不错”）**：① `GET /config` 全量存盘（带 md5）② `PATCH /config`，body **必须是 `{"updates":{…}}`**——只传要改的键，**不要整份 config 覆盖** ③ 重新 GET 读回逐项核对（原有条目是否逐字保留、`retain_extraction_mode` 有没有被顺手改掉）④ 行为测试走 **`POST /memories/dry-run-extract`**：官方只读端点（`nothing is stored`），支持 `retain_mission`/`retain_custom_instructions` 等 prompt-affecting override——**不传参数＝用 bank 当前配置，传备份里的旧值＝直接做改前/改后 A/B**。这是唯一能绕开队列堵车拿到证据的做法（别指望「入队后去 memories/list 里翻」，队列一堵就等不到）。⑤ **必须双向测**：只测「中间态被挡掉」不够——这条规则天然会往过筛那边倒，必须再拿**纯日常闲聊**输入测一遍，确认该记的生活细节/情绪/偏好一条没漏。实测参照：旧指令 7 条（含「现有 N 条记忆」「进度 M 中完成 K 条」「容器处于运行状态」「正在重跑监控脚本」「排队中剩 N 个任务」）→ 新指令 2 条（只剩那条真决策+理由）；反向闲聊组 6/6 全保留。⑥ dry-run 在单槽 llama 上 **90s~770s** 不等（与库里正在跑的 retain 抢同一个槽），客户端 timeout 给足 900s；`POST /memories` 同步路径也会 >120s 无响应——**那不是失败，是已入队，别重发**（重发会再叠一份进去）。⑦ 确实要入队做端到端验收时，测完**把测试文档删掉并读回确认**（`DELETE /documents/{id}` 会级联删它产生的 memory units，响应里会写删了几条；随后用 `memories/list?q=<关键词>` 复验 total 归 0），别让测试用的假事实留在库里污染召回。

## 验证产物的收尾：全量复核残留，别只看「删掉的那两条」

验证链路（尤其真链路跑 MCP `retain` / 真写库）本身会**往库里造测试数据**，删掉当次产物不等于干净——同一批验证常留下记忆单元、文档、operation 三类残留。收尾按这个顺序做：

1. **先选不写库的验证路径**：能只读就只读（AstrBot 工具链验证只跑 `local` / `file` 模式，**不跑 `retain`**）；`dry-run-extract` 是官方只读端点，也不落库。
2. **全量扫，别靠关键词召回**：`GET /memories/list?limit=2000&offset=N` 翻到底（本机 2.9 万条几页就完），`GET /documents?limit=500&offset=N` 同理；用一批关键词做**字符串**匹配（tag 名 + 文档 uuid 前缀 + 验证脚本名 + 样本正文特征串）。
   - 别只用 `recall` 判残留：语义检索对「短测试样本」不敏感，容易给出 0 命中的假自信。
   - 命中里先排除**误报**（真人真事里恰好含「验证/测试」这类词的），再动。
   - **复核脚本的命中会随库增长而漂移，别把新命中直接当残留**：最典型的一类误报是**「这次清理已经做完」的结论记忆本身**——它天然带着被清理对象的 id 片段/关键词，于是下一轮扫描必中。判据看内容（是结论还是产物）+ `state`（已 `invalidated` 的是退役项、不参与判定），确认是结论就把该 id 加进脚本的 `REVIEWED_OK` 白名单并注明理由，让脚本回到 `exit 0`；**白名单是脚本的一部分、要维护**，否则复核脚本会长期假报警，看起来像「又出现残留了」。
   - **复跑结论与登记文档冲突时以复跑为准，并当场修脚本/文档**：被问「这件事做完了没有」时不要复述登记文件——重新跑一遍验收脚本；跑出 `exit 1` 就查清是假阳性还是真回归，是假阳性就修完再答，并在回报里说明「复跑发现并修掉了一个误报」（只报一句「已完成」等于把没验过的话当结论）。
3. **两类产物的处置不同**：
   - **文档**：`DELETE /documents/{id}` 级联删它派生的 memory units（响应会写删了几条）；测试文档走这条。
   - **状态快照类条目**（如「N 条测试记忆待清理」）：**软废弃**，不硬删——清理做完了它就过时了，PATCH `state=invalidated` + reason 注明「已清理完毕」。
4. **复验软废弃生效**：默认 `list` 里**查不到**它了，`?state=invalidated` 才看得到 → 所以「默认列表里没了」不等于被删，`?state=valid` 里也不该再有它。
5. **operation 记录是删不掉的，别在这里耗**：`GET /operations`（limit **上限 100**，200/500 一律 `422`；键是 `operations`，字段 `id`/`task_type`/`status`/`created_at`；单条 `GET /operations/{id}` 可用，`batch_retain` 里带 `child_operations`）能定位到「测试那次 retain 的父+子 op」，但 `DELETE /operations/{id}` **只能取消 `pending`**，completed 一律 `409 cannot be cancelled`。想删只能直连 pg `delete from async_operations where operation_id in (...)`（已核**无外键引用**，安全但不是可逆操作）→ **属于不可逆 DB 写，必须先拿主人直接授权**；没授权就**如实报「这几条 completed 的 op 记录还在，纯任务日志、不含记忆内容、无功能影响」**，别硬删也别假装干净。
   - 定位技巧：op 记录的 `created_at` 时间窗对着文档 `created_at` 找（文档是 op 跑完落库的），比凭 uuid 猜快。直连 pg 的口令读法见 `hermes-container-ops` 的僵尸恢复配方（容器内 `instance.json` 的 password 键，不落盘不打印）。

## 与另一份手册的关系（重叠提示）

`skills/memory/hindsight-memory-engine`（**主人自有 skill，棉棉不可自动改**）也有一节「标记记忆作废（软退役）」，含 `scan_hs/` 下的三个脚本。两处内容重叠；本文件额外补的是：**验证口径（读召回而非看 list 消失）、三类条目的区分、中间态会持续产生这一点、以及上游压制的标准流程与双向量尺**。若主人愿意 `hermes curator adopt hindsight-memory-engine`，这些应合并进那一份、并把本文件瘦成指针。

⚠️ **实测：后台 curator 对 `hindsight-memory-engine` 的写入会被直接拒**（报 `not curator-managed (created_by=None)`，需主人跑 `hermes curator adopt` 才能接管）——主人在场的会话里能改、无人值守的巡检会话里改不了。所以**这类 Hindsight 经验先落本文件（或 hermes-container-ops 下其他 reference）**，以后 adopt 了再合并；不要在巡检里反复尝试写它。
