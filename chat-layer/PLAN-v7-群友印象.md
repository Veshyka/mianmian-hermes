# PLAN v7：让"她对不同群友有印象"（群记忆分流）

**状态**：✅ 已实施（2026-10-03，主人拍板：一个群库 + 群标签人标签 + 群召回比私聊更省 token +
自动回填走 Hermes 自带 auto-retain。实现见 `ops-changelog/变更-20261003-群记忆分流.md`）
**触发**：主人「没有长期记忆，那我要的可以对不同群友有对应印象咋实现呢」

## 1. 现状：为什么现在做不到

群回合的记忆被 `plugins/hindsight_guard` **焊死**（2026-09-23 主人要求"群聊不污染私聊记忆库"的产物）：

| 动作 | 群回合现状 | 证据 |
|---|---|---|
| 写长期记忆 | `sync_turn` 被拦，不 retain | `hindsight_guard/state.json`: `retain_skipped` |
| 读长期记忆 | `prefetch` 被拦，不召回 | 同上 `recall_skipped` |
| 手动工具 | `hindsight_retain/recall/reflect` 在群里拦死 | `tool_blocked` |
| 兜底 | `group_memory_guard_required: true` → 闸不在位就不唤醒 | `profiles/chat/config.yaml:80` |

她群里现在的"记忆"只有两层：**整群共用的会话历史**（压缩即断片）+ **群窗口 delta**（每群 200 条）。
所以：她在同一个群里能连贯，**换个群/断了会话就谁都不认识**。

## 2. 方案 A（推荐）：给"群"开独立记忆库，群回合从"切断"改成"分流"

### 2.1 数据面

```
私聊回合 ──▶ bank: mianmian-history      （不动，主人私聊记忆）
群回合   ──▶ bank: mianmian-group        （新建）
                 ├─ 每条记忆打标签  group:<群号>   （哪来的）
                 └─                 speaker:<QQ号> （谁说的/关于谁）
```
两个库**互不读、互不写**：私聊不会召回到群聊内容，群里也召不到私聊内容（维持 2026-09-23 那条决定）。

### 2.2 代码面（全在我们自己写的插件里，不碰 Hermes 核心）

`plugins/hindsight_guard` 从"一刀切拦"升级为**分流闸**：

1. 新增开关 `mode: block | redirect`（**默认 block = 现在的行为**，即零风险默认；redirect 才启用新逻辑）
2. `redirect` 下，群回合（判据沿用现有两信号：`chat_type` + 正文 `source=qq-group` 标记）：
   - `sync_turn` → 不拦，改成换 bank 到 `mianmian-group`，并给本轮 retain 打上 `group:<群号>`、`speaker:<QQ号>`
     （群号/QQ 从正文标记和消息行里解析，形如 `名字(QQ:号): 内容`）
   - `prefetch` / 后台 recall → 不拦，改成查 `mianmian-group`，`tags=[group:<群号>]`、`tags_match=any`
   - 手动工具：**先保持拦死**（防她手滑查到私聊库）。等跑顺了再考虑"放开但锁在群库"。
3. 技术上可行已核对（读的是 bundled 实现）：
   - `self._bank_id`、`self._recall_tags` 都是**调用时读实例属性**（`__init__.py:879/889/1005`）→ 包装类可以每轮换掉
   - recall 支持 `tags` + `tags_match`（`recall_tags` 配置项同源）
   - 本机 Hindsight 是多 bank（`GET /v1/default/banks` 实测返回 `banks[]`）
   - retain 有缓冲与水印（`retain_every_n_turns`），不用每轮都写

### 2.3 她"看到印象"的形态

群回合正文里多一段自动召回结果（跟私聊一样的注入形式）：

```
[你还记得的：这个群里的事]
- <OWNER_QQ2>（总是栋真情）上次说过他在准备考研，之前问过三次选校
- <GROUP_ID2> 这个群改过两次群名，最近一次是她改的
```

Hindsight 的 retain 会抽事实 + 建实体（人 / 关系），所以"谁是谁、说过啥、偏好啥"是**自动积累**的，
不需要她每轮手动记。

## 3. 方案 B（轻量，可作补充）：群笔记本

每群一个 `profiles/chat/group-notes/<群号>.md`，她自己用 `file` 工具维护（她群回合工具面里有 file），
每轮唤醒时把该群笔记本注入正文。零新基建、所见即所得、可人工翻看修改。

代价：靠她自觉写；纯文本没有语义检索；群多了以后是 N 个文件。

**A + B 不冲突**：A 做底（自动积累 + 语义召回），B 当"人物卡"（她明确写下的印象，最可靠）。

## 4. 三个要主人拍板的点

1. **一个群库，还是每群一个库？**
   我倾向**一个库 + `group:<群号>` 标签过滤**：同一个人的印象能跨群复用，实体图更完整；
   每群一个库隔离更硬但会碎成一堆小库。
2. **群消息全量入库，还是只存"对人的印象"？**
   我倾向**全量入库 + 开自动整合**（跟主库一样，把噪音压成观察），省事；
   只存印象则需要额外的挑选逻辑。
3. **跨群认人要不要开？**
   默认**只在本群可见**（同一个人在 A 群的印象不出现在 B 群）。要开就靠"同人标签"放行——
   `speaker:<QQ号>` 已经打了，改一行过滤就能开。

## 5. 成本与风险

* 群回合多一次 recall：延迟约 1–2s，每次几百 token（私聊现在就是这个量级）
* 群库会涨：群聊内容比私聊吵；靠 Hindsight 的 consolidate 压（主库已开 `enable_auto_consolidation`）
* 桥接风险：改的是 `memory.provider` 指向的插件，出问题回滚 = `mode: block`（一行）或
  `memory.provider: hindsight`（退到"群回合入库"的老样子，**不建议**，那会污染私聊库）
* 未验证项：群库是否需要手工创建（Hindsight 是否在首次 retain 时自动建 bank）——
  动工前用一次性写入试；不要照假设走

## 6. 验收（做完怎么算成）

1. 群里问她"上次那个说要考研的人是谁" → 能答出来（且答案来自群库）
2. 私聊问她同一句 → **答不出来**（证明群库没污染私聊库）
3. 换一个群问"这边都谁" → **不出现另一个群的人**（证明按群过滤生效）
4. `state.json` 里 `retain_skipped/recall_skipped` 在 redirect 模式下归零、新增 `redirected` 计数
