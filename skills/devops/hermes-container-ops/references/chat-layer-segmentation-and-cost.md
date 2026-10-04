# 聊天门（第二道门）与前端框架：分段机制、记忆共享、成本

面向 `im-channels-and-frontends.md` 里那套「AstrBot 当纯通道 + NapCat 个人号 + 第二道门 sidecar」落地后的调优事实。

## 分段（源码级，2026-09-20 核对 v4.28.x）

三个门槛全过才切：`platform_settings.segmented_reply.enable` 为真、平台不在排除名单（`qq_official_webhook`／`weixin_official_account`／`dingtalk`）、**文本长度 ≤ `words_count_threshold`（默认 150）**。

- **阈值方向是反的**：长文本（>150 字）**整条发**，短文本才按句切 —— 别记成「超过就切」。
- 只对 `Plain` 组件切；Image／At／Reply 原样保留、顺序不变。
- 切分在 `ResultDecorateStage`：默认正则 `.*?[。？！~…]+|.+$`（`re.findall` + DOTALL/MULTILINE）→ 遇句末标点断一刀，**剩余的尾巴也单独一刀**（所以结尾颜文字会被切出去；真人聊天里这其实常见，不一定是 bug）。
- **换行不是切分符**（拿候选文本直接跑那条正则即可验证，不用起服务）：三行都不带标点 → **只出一条**；以语气词（吧／呢／啊／嘛／呀）收尾的句子会跟下一句**并成一条**。所以「靠 `\n` 分行 = 连发多条」在默认配置下不成立 → **要模型真连发多条，人设必须要求每句用 `。？！~…` 收尾**（或按上文配方自己在插件里切、AstrBot 正则改 `(?s).+`）。改完 SOUL 要**重启该门**才加载。
- 节奏在 `RespondStage`：`interval_method: random` → 每条间 `random.uniform(*interval)`（默认 1.5–3.5 秒）；`log` 模式 → `log(字数+1, log_base)` 到 +0.5（底默认 2.6）。
- **`is_seg_reply_required()` 只要求 `enable_seg` 与平台不被排除，不要求真的切过** → 配方：**自己切**（在插件里按自己的断句规则产出多个 `Plain` 组件），同时把 AstrBot 正则改成不切（`(?s).+`）→ 切法自己定，随机间隔仍白借它的。

## Hermes 侧没有的两件东西（源码级，2026-09-23 核对 0.21.3）

「把聊天门的分段/防抖搬进 Hermes」是常见误会，先把这两条钉住：

1. **分段不是 Hermes 网关能力**。全树 `grep -rni "segmented\|split_reply\|multi_reply"` 无相关命中；唯一叫 segment 的出站机制是 `gateway/stream_consumer.py:367 on_segment_break()`，唯一调用点 `gateway/run_turn_runner.py:958`（模型先说话、再调工具 → 收口另起一条），**只覆盖 preamble 边界**。一个回合正常终点只发一条消息。要「多气泡」只能在**通道层**切（AstrBot `ResultDecorateStage`），或在桥上自己切。
2. **防抖只有「忙时缓冲」，没有「空闲合并」**。`gateway/platforms/base.py:3471 _is_queue_text_debounce_candidate()` 要求 `_busy_text_mode == "queue"`（默认 `interrupt`）**且会话已忙**；窗口 `HERMES_GATEWAY_BUSY_TEXT_DEBOUNCE_SECONDS` 默认 **0.35s**。主人连发五条、等他说完再一起回——网关不提供。`webhook_coalesce.py`（30s/300s）是 webhook 路由专用，与 IM 无关。
   → 结论：**分段与防抖留在通道层**，别搬。

## Hermes 原生平台接不了 QQ 个人号（协议端）

- Hermes 唯一的 QQ 平台是**官方机器人 API v2**（`gateway/platforms/qqbot/adapter.py:1-4`，凭据 `app_id`/`client_secret`）。全树 `grep -rni "onebot\|aiocqhttp\|napcat" /opt/hermes` **0 命中**；官方文档索引 `docs/llms.txt`（curl 全文后 grep）同样 `onebot`/`napcat`/`aiocqhttp` 0 命中，且连 `qq` 都没有条目。
- 平台锁是**机器全局**的 `_acquire_platform_lock("qqbot-appid", app_id)`（`platforms/base.py:2142`）→ 同一 `app_id` 只能一个门连，两个 profile 不能共用（本机干活门是官方机器人 `<APP_ID>`，小号 `<BOT_QQ>` 是 NapCat 个人号，两者无关）。
- 所以「让第二个 profile 直接接 NapCat 小号」= **得自己写 OneBot 平台适配器**（数百行 + 长期维护），或让 NapCat 继续挂在通道层、Hermes 只当大脑（走 api_server）。原生零自建的唯一办法是**再注册一个官方机器人**给聊天门（代价：不是小号、且没有多气泡）。
- qqbot 适配器自带扫码绑定流程（`qqbot/onboard.py`，`q.qq.com` bind-task），那是**官方机器人**的绑定，不是个人号登录。

## 量尺的坑：多调用回合的 `prompt_tokens` 是累计值

探针打 `/v1/chat/completions` 时，若该回合调过工具，`usage.prompt_tokens` 是**该回合每一次 API 调用的和**（实测一次问了 11 次 API → 报 143,215，而单次结构只有 ~9.3k）。**结构成本只认「单次调用」的探针（如「在吗」）**；拿多调用回合的数字做前/后对比会得到「改完贵了 8 倍」这种假结论。要看回合级信息就读 `state.db` 的 `sessions(api_call_count, input_tokens, tool_names)`。

## 提示词与人格落点

- 第二道门的人格 = `profiles/<name>/SOUL.md`（宿主 `<映射目录>/profiles/<name>/SOUL.md`）；它是每轮构建提示词时读取的**上下文文件**，改完通常下一轮生效，不放心重启该容器。
- Hermes 会扫描上下文文件里的**注入式写法并拦截**（`agent/prompt_builder.py`）→ 写人设别用「忽略以上指令」这类句式。
- 前端框架（AstrBot）侧**不需要**配人设：它的 persona 只喂它自己的 LLM，而纯通道模式下 `provider_settings.enable=false`，那段配置没人读。

## 记忆共享（实测，含一次误判的纠正）

**新建 profile 默认没接记忆引擎**：`memory.provider` 是空的（主 profile 是 `hindsight`），`<HERMES_HOME>/hindsight/config.json` 也不存在。此时它答对「昨天在弄什么」靠的是 `session_search` 翻会话库，**不是共享记忆**——「它答对了」不能当共享记忆的证据。

**判据只能是库里有没有它的落库记录**（读写两个方向都要看）：
```bash
curl -s --noproxy '*' http://172.17.0.1:8888/v1/default/banks/<bank>/documents?limit=5
#   → retain_params.context / retain_params.metadata.source（如 hermes-chat）/ agent_identity
curl -s --noproxy '*' http://172.17.0.1:8888/v1/default/banks/<bank>/stats
```
（`/memories` 返 405，别试；`/documents` 与 `/stats` 可用。）

- 分工要分清：**长期记忆**（Hindsight）可共享；**会话工作内存**（刚聊到哪）与**任务交接状态**（谁接了活、干完没）两边各一份，要共享得另做通信层。
- 前端框架侧不需要接记忆（它只转发）。要接也就三条路（插件直连 Hindsight HTTP / Hindsight 的 MCP 端点 / 统一由 Hermes 管）；**别在前端侧再起一个带模型的「第二大脑」**——人格与记忆会割成两份。
- 架构定性：并行（两条线互不阻塞）靠「同一大脑跑两个 profile 进程 + 共享 Hindsight」实现。

## 成本收窄配方（实测：33 万 token → 8 千，延迟 −90%）

**先建量尺再动刀**：同一组探针、改一项测一次。

```python
# 探针（直接打该 profile 的 api_server，读响应里的 usage）
#   P1「在吗」                       = 结构成本基线（不触发工具）
#   P2「用一句话回答：我们昨天主要在弄什么项目？」 = 记忆/工具回路成本
# POST http://127.0.0.1:<port>/v1/chat/completions
#   {"model":"<profile 名>","messages":[...],"stream":false}
# 记录 prompt_tokens / completion_tokens / 墙钟时间
```

**第一步永远是看它现在暴露了什么工具**（响应键是 `data`，不是 `toolsets`）：
```bash
curl -s -H "Authorization: Bearer $API_SERVER_KEY" http://127.0.0.1:<port>/v1/toolsets
```

**四项收窄**（全部只改目标 profile，主 profile 一个字节不动）：

| 键 | 改成 | 为什么 |
|---|---|---|
| `platform_toolsets.<platform>` | 只列真要的，如 `[memory, vision, delegation]` | 默认 14 套工具全开。**同时堵住「IM 消息 → 终端命令」的口子**（输入来自群/陌生人是不可信的） |
| `tools.tool_search.enabled` | `off` | **`auto` 源码注释就是「auto is an alias of on today」**：它把工具藏进目录，模型只能用 `tool_call` 批量取 → 被「本地工具必须一条一个」拒 → **反复重试烧 token**。33 万的真凶在这里，日志特征 `Local tools require one entry per tool_call` |
| `memory.provider` | `hindsight` | 空 = 记忆引擎完全不工作（工具、自动召回、落库全无） |
| 非必要 toolset | 一并删掉 | 关掉 bridge 后剩余工具 schema 会内联进 prompt，工具越少越省 |

**Hindsight 是 profile-scoped 配置**：`<HERMES_HOME>/hindsight/config.json`（不写就回退 env 默认 `mode=cloud / bank=hermes` → 对本地自建实例等于不工作）。可用键（源码 `plugins/memory/hindsight/__init__.py`）：`mode`/`api_url`/`bank_id`/`auto_recall`/`auto_retain`/`recall_sync`/`recall_budget`(low·mid·high)/`recall_types`/`recall_max_tokens`/`memory_mode`(hybrid·context·tools)/`recall_indicator`/`retain_indicator`/`prefetch_waits_for_retain`/`retain_source`/`retain_tags`/`retain_context`。

面向聊天的取值与理由：
- **`recall_sync: true`** —— 前端插件把历史整段重发（无状态请求，没有 Hermes 会话可依托），默认的「后台预取、下一轮注入」对第一条消息永远不生效 → 必须同步召回（代价 1–2 秒）。
- **`memory_mode: "context"`** —— 只自动注入、不暴露记忆工具。暴露时它真会去调 `hindsight_reflect`，而本地自建实例的 reflect 返 **500**，一次卡 40 秒。要用工具就先手动验一次 reflect 再开。
- `recall_max_tokens: 1024`、`recall_indicator: false`、`retain_indicator: false`（聊天里不该冒出 `👁️ recalled N` 状态行）、`prefetch_waits_for_retain: false`（少等一次本地队列 drain）。
- `retain_context`/`retain_tags` 标明来源（如 `棉棉与主人的 QQ 聊天（聊天门）` + `hermes,chat`），日后能按来源筛。

**改完必须重启该 profile 的容器**（配置在进程启动时读），再用 `/v1/toolsets` + 三个探针复核。

**实测（前 → 后）**：问候 16,632 → **8,546**；要记忆的问题 329,577 / 30.6s → **8,677 / 3.1s**；要人物记忆的问题 76,042 / 124.6s → **8,481 / 3.6s**。

**配套的取舍**：聊天层只留 `delegation` 时，遇到不会的事它会**派子代理**，代价是首条消息久等（实测约 79 秒 / 33k token）。⚠️ 但注意：**子代理继承父级工具面、永远拿不到父级没有的工具**（`tools/delegate_tool_toolsets.py`）→ 收窄后的聊天门派出去的子代理**只剩看图工具**，等于派不动活。三条路择一：SOUL 里写明「闲聊不派活、没把握就说不知道」／把 `web` 加回聊天层／**把活投给工具完整的门**（插件层路由，见下文）。

## 排障时的日志特征（省得靠猜）

`docker logs <第二道门容器> --since 5m` 里这几行直接指向根因：
- `Tool tool_call returned error ... Local tools require one entry per tool_call` → deferred bridge 在空转（见上表）
- `vision_analyze returned error ... media file not found: '<某个 .md>'` → 它没有文件工具，却在拿看图去读记忆/日记文件，说明人设（SOUL）在指挥它做没有能力做的事。**通用规则**：收窄工具面之后，它会拿**手边剩下唯一的相邻工具**去凑（实测连着 5 次触发 `same_tool_failure_warning`，白烧一轮）→ 收窄时要保证它真正需要的通路还在（记忆靠**自动注入**，而不是靠它自己去找文件）
- `hindsight_reflect failed: (500)` + `Tool hindsight_reflect returned error (40.59s)` → 记忆工具该关（改 `memory_mode: context`）

## 余额与花费自查

```bash
# 余额（官方接口，直接可用）
curl -s -H "Authorization: Bearer $DEEPSEEK_API_KEY" https://api.deepseek.com/user/balance
# 用量与估算花费（Hermes 自带）
hermes insights --days 2                 # token / Estimated 花费 / 按模型·平台·工具分布
hermes -z "..." --usage-file out.json    # 单次运行输出 JSON 用量报告
```

**峰谷（官方文档核对 2026-09-20）**：高峰 = 周一至周五 **09:00–12:00 与 14:00–18:00（北京时间）**；其余全是谷价，**谷价 = 峰价的一半** → 重活排谷时（工作日 12–14 点、18 点后、周末全天）。

## 调真人感时的参照

主人本人的聊天记录特征：消息**细碎** —— 一条 3~10 字、一条一个意思、常连发 2~5 条、**基本不写句末标点**、表情/颜文字常单独成一条。

⚠️ **别照抄成「短句别补句号」**（曾这么写过，被主人发现「还是旧的换行模式」）：**切分器只认句末标点**，不补标点的短句会被并成一整条长消息——他要的「连发好几条」反而消失。两者冲突时以切分器为准：人设里写「想单独成条的句子用 `。？！~…` 收尾、**不要靠换行**」，同时保留「短、碎、不写长段」的风格；**整条 >150 字则完全不切**，所以「别一次写长」这条风格要求同时也是分段的硬前提。

## 收窄后的能力边界（给它写规则前先看这张表）

| 它能不能 | 收窄后的第二道门 | 依据 |
|---|---|---|
| 写自己的记忆档 `profiles/<p>/memories/MEMORY.md`·`USER.md` | ✅ 有 `memory` 工具就能写 | 实测写通了；**只落自己 profile 的档**，主 profile 的 `MEMORY.md` 一字节未动（两个 profile 的 `HERMES_HOME` 不同） |
| 落库到共享库（Hindsight） | ✅ 自动（`auto_retain`） | 查 `/documents` 可见 `metadata.source` = 该门 |
| 任意文件读写 / 终端 / 跑代码 | ❌ 现况；**文件读写将放开到工作区** | 现在被工具面挡住（收窄时删掉了 `file`/`terminal`/`code_execution`）；主人已定给 `file` 权 + `HERMES_WRITE_SAFE_ROOT` 收窄，terminal/跑代码仍不给 |
| 按人召回（「关于某人」） | ❌ | `hindsight_recall` **只有 `query` 一个入参**（源码），没有按人/按标签过滤 → 只能指望查询词里的人名触发实体遍历，本质是概率命中 |
| 认人（谁在说话） | ❌ | `api_server` 请求不带身份，落库 metadata 的 `user_id`/`user_name` 为空 |

由这张表推出的写规则口径：
- 「不装失忆」**可以**进这种门（它能记能召回，没有失忆的借口）；但要求它「去翻日记/查文件」是空头支票。
- 「承诺落盘前先写文件」**不能**原样进——它写不了文件，落盘承诺只会变成撒谎的机会。改成分层：**能兑现的「记一笔」就记（记忆档/落库）；兑现不了的「落盘/交付」不许承诺**（那是干活门的活）。

## 要它「记住某个人／某个群友」——主人已拍板的最小方案（2026-09-20）

`memory_char_limit` 2,200 / `user_char_limit` 1,375 **字符**是硬上限，召回又按不了人（见上表）。曾设计过一套「工作区 + `file` 工具 + SOUL 只读挂载 + 插件身份」的四件套，**主人否掉了前三样**，理由是**「如非必要勿增实体」**。最终采用：

1. **群友印象直接写 `USER.md`**（一句/几字一人）。容量账：1,375 字符 − 主人自己那份（约 300 字）→ 每人约 50 字 × 20 人刚好够（主人按 20 人算的，群友不会更多）。真不够用 `MEMORY.md` 那 2,200 字符当机动。
2. **SOUL 只读 = 规则一句 + 框架自带拦截**（不用挂载、不用只读绑定）：聊天层已被收窄掉 `file`/`terminal`，**物理上碰不到 SOUL.md**；即便哪天给它 `file` 工具，`security.protected_instruction_files: True`（默认开）规定 SOUL.md／AGENTS.md／CLAUDE.md／.cursorrules 的写入**永远要人工批准，连 yolo 模式也不例外**，而无人值守平台走 `approvals.unattended_mode: deny` → **当场拒绝**（不是等超时）。所以规则那句只是防它误以为自己能改。
3. ~~不加工作区、不加 `file` 工具~~ → **同日已改：主人定「聊天门可以给文件权」，细节他要和棉棉一起在提示词里钉死**。落地四条（等开工指令，别自己动）：① `file` 加进该 profile 的 `platform_toolsets`（约 +1k token/轮的 schema）；② 设 `HERMES_WRITE_SAFE_ROOT` = `工作区:<该 profile 的 HERMES_HOME>`，**别把 `stack/`（napcat/astrbot 数据）圈进去**；③ 工作区放 `<映射目录>/<层名>/workspace`，容器内同路径挂入；④ **SOUL.md 只读挂载**（`-v …:ro`）——这是唯一绕不过去的物理防线。
4. **唯一仍需补的**：前端插件发送者身份（现只发 `model/messages/stream`）。**不补的话 USER.md 里写人也只能靠模型从对话里猜**，记不牢。`api_server` 支持 `metadata` 字段与按 `session_id` 复用会话（`/api/sessions/{id}/chat`），身份走这条路传。

**记忆档写满时的行为**（要写进 SOUL）：Hermes **拒绝新增并把现有内容摊出来** → 让它自己合并/淘汰，SOUL 里点一句「满了砍最没用的」就不会卡。

**边界线（一句话）**：记忆档（`USER.md`／`MEMORY.md`）归它自己管；SOUL、规则文件、系统配置只读；宿主其余数据靠**挂载收窄**隔离，而不是靠「不给工具」。

**升级路径**：给文件权之后，四件套里只剩两件要做（专属可写工作区 + 插件身份）——`file` 工具与 SOUL 只读挂载主人已点头。**落盘承诺的口径随之改变**：「承诺落盘前先写文件」在它能写工作区之后可以进人设，但仍限定在**工作区范围内**（写不了的地方不许承诺）。

## 探针的两种量法（数字要报得准）

- **结构成本**：发固定短消息读 `prompt_tokens`（如「在吗」）；改一项测一项，改完重启该 profile 容器再复核。
- **中文 token 换算**：发一条 N 字的填充消息，比基线 `prompt_tokens` 的增量 ÷ N → 实测 **≈0.5 token/字符**（重复文本 0.37–0.53）。用它把「字符预算」折成钱。
- ⚠️ **报体量一律用字符，别用 `wc -c`**：中文 1 字 = 3 字节。本机 `SOUL.md` 14,287 **字节** = 8,499 **字符**——把字节当字符，「提示词砍一半」的预算会差三倍。Hermes 自己的记忆上限（2,200 / 1,375）也是字符。

## 会话与落库的一个硬事实

`api_server` 的每个请求**各自成一个会话**（落库 `session_id` = `api-<hash>`、`turn_index` = 1）：
1. Hermes 侧没有跨消息的会话连续性——上下文靠前端插件自己带的 hist；
2. 依赖「上一轮预取」的自动召回永远不触发（必须 `recall_sync`）；
3. 落库条目**没有人物归属**，要按人检索得先在插件层补身份。

## 文件权限：区域靠 `HERMES_WRITE_SAFE_ROOT`，动作靠指令文件闸（源码级）

`write_file`/`patch` 依次过五道闸（`tools/file_tools*.py`）：敏感路径硬拒（`/etc`、`/boot`、`/usr/lib/systemd/`、`docker.sock`）→ 二进制/文档写入检查 → **指令文件写入（一次操作一次人工审批）** → 通用审批闸 → 跨 profile 路径检查。

**「只允许写某个目录」是有的 —— 但它是环境变量，不是 config 键**：
```bash
-e HERMES_WRITE_SAFE_ROOT=/opt/data/workspace:/opt/data/profiles/<p>
```
- 多个根用 `os.pathsep`（Unix `:`）分隔；越界报 `denied: '<path>' is outside HERMES_WRITE_SAFE_ROOT`（实现 `agent/file_safety.py`）。
- **只管 `write_file`/`patch`，不管 `terminal`**（terminal 跑在同一 OS 用户下）→ 官方口径：这是「给诚实但会犯错的 agent 的护栏，不是对抗性沙箱」；要真隔离得用挂载或容器后端。
- **必须把该 profile 的 `HERMES_HOME` 也列成一个根**，否则它写不了自己的 cron/skills/状态。
- **记忆档（`USER.md`/`MEMORY.md`）由 `memory` 工具自己写、不走这套闸** → 设了沙箱也压不垮它的记忆。

**「给全权限」vs「收窄根」不是取舍——两者 token 成本完全相同**：成本只取决于 `file` 工具集加不加（约 +1k token/轮），与 `HERMES_WRITE_SAFE_ROOT` 的宽窄**无关**。所以收窄是**免费保险**：能写工作区（「落盘承诺」照样成立），却结构性碰不到 `stack/`（napcat/astrbot 数据）、凭据、别的 profile。主人提「干脆给全权限、我在提示词里限狠一点」时按这条答：提示词管不住「诚实但会犯错」的写入，路径闸能管，而它不花一分钱——除非 workspace 与宿主敏感目录重叠，那就改用挂载隔离。

**单容器模式下怎么只收窄某一个门**：`HERMES_WRITE_SAFE_ROOT` 是**容器级 env**（`-e` 只在 `docker run` 时给），同容器里两个 profile 的门会共享同一个值 → 要按门区分，在该门的 **s6 槽 `run` 脚本**里 `export`（每个槽的 run 有自己的环境）。改这一处即可，不用动挂载、不用重建容器。

**指令文件闸（SOUL/AGENTS/CLAUDE/.cursorrules）的真实姿态 —— 别当成「无人值守必然当场拒绝」**：它**故意不走 `approvals`**（不受 yolo/allowlist 影响），一次操作一次授权、不持久化；没有任何人工通道时 fail-closed 直接 BLOCKED，**有 gateway 通道时会去等一个审批回执**（最长 `approvals.timeout`，默认 300 秒）→ 在 api_server 这种没人点按钮的通道上，表现是**白等几百秒再被拒**。而且有绕路案例：审批超时后 agent 把超时当成障碍，改用 `python3 -c` + `shutil` 自己动手删。

→ 结论：**「它改不了 SOUL」的硬保障只有两条 —— 没有 `file` 工具，或 SOUL 以只读方式挂进容器（`-v <SOUL>:/opt/data/SOUL.md:ro`）**；规则句与框架闸都只是辅助。

⚠️ **2026-09-22 实际落地的形态与这条不一致**：主人定「给文件权」，但**没有加只读挂载、也没收窄根目录**（`HERMES_WRITE_SAFE_ROOT=/opt/data` 容器级共享）→ 只剩框架闸在挡：`security.protected_instruction_files=true` + `approvals.unattended_mode=deny`（`hermes -p chat config get` 实测值）。**如实对主人说的口径**：指令文件写入会被拒或白等审批超时（不是物理防线）；上面那个 `python3 -c` + `shutil` 绕路在本门**不可用**（它没有 `terminal`/`code_execution`，只有 `file`）——但要真上物理防线，仍得加只读挂载。

## 跨门派活的准确工具面（别在人设里写模糊的「走 agent 间通话」）

- **`delegate_task` 不是 agent 间通话**：它是同 profile 内的一次性子代理，且继承父级工具面（见上文收窄那节）→ 收窄后的门基本派不动活。
- **真正的跨 agent 通路是 `a2a` 工具集**（官方插件 `plugins/platforms/a2a/`，实现 A2A 协议 v1.0，对端可以是另一个 Hermes／LangChain／CrewAI／Google ADK 等）。两个致命前提：**默认关闭**（`_DEFAULT_OFF_TOOLSETS`）、**没配对端就不注册工具**（fail closed；源码注释说明过去无条件注册会让每个会话白付约 561 token）→ **模型看不到工具名，就会假装「我已经派了」**。
- 工具与入参：

| 工具 | 用途 | 入参 |
|---|---|---|
| `a2a_call` | 把自然语言任务发给对端平级 agent，拿回复；传 `context_id` 续多轮 | `agent`（配置里的对端名或 URL）、`message`、`context_id?` |
| `a2a_discover` | 先拉对端 Agent Card，看它到底能干什么 | `url` |
| `a2a_list` | 已配置对端 / 已持久化会话 / 指标 | — |
| `a2a_history` | 按 `context_id` 取回持久化对话（落 `$HERMES_HOME/a2a_conversations/<peer>.jsonl`，重启不丢） | `context_id`、`limit?` |
| `a2a_orchestrate` | 按能力扇出到多个对端（all/first/best） | `capability`、`message`、`mode?` |

- 配置：接收端 `gateway.platforms.a2a.enabled: true` + `extra.port`；调用端 `a2a_agents: {<名字>: {url, capabilities, auth?, timeout?}}`，再把 `a2a` 加进该 profile 的 `platform_toolsets`。
- ⚠️ `a2a_call` 是**同步等待**（timeout 默认 120 秒）→ 对聊天体感是「要等一下」，长任务必超时；长任务应走「投递 + 回头汇报」（前端插件层路由）。
- **写人设的口径**：要么写准确工具名（`a2a_call`，agent 填对端名）+「等不到回复就说明情况，不许谎称已派」；要么**完全不点工具名**，写成「把任务书写清楚交出去，投递由通道层负责」+「手上没有委派工具时，不许假装已经派了活」——后半句是防空转幻觉的关键。

## 给聊天门开文件权限（标准流程，2026-09-22 实测）

**目标**：聊天门能读主人发来的附件、能读写自己派出去调研的产物。四步，别跳步。

1. **加工具集**：`profiles/chat/config.yaml` 的 `platform_toolsets.api_server` 加 `file`（合法名以官方注册表 `hermes_cli/tools_config.py` 的 `CONFIGURABLE_TOOLSETS` 为准；`file` = read/write/patch/search）。改完**读回**确认。
2. **让附件可读**（跨容器、跨 uid）：
   - AstrBot 以 root 落盘、umask 077 → 附件是 `600 root:root`；Hermes 容器是 uid 1003 → 读不到（目录 700 时连列都列不了）。
   - ⚠️ **本机 btrfs 挂载是 fnOS 的 `trimacl`，POSIX ACL 不生效**：`setfacl` 写得进去、`ls -ld` 显示 `+`、xattr 也在，但**宿主同 uid 能读、容器内同 uid 被拒** → **只能用 chmod**（目录 `755`、文件 `644`），别在 setfacl 上耗时间。
   - **以后的新附件**要靠插件自己放宽：`hermes_forward/main.py` 加 `_relax_perms()`（图片/文件两个分支各调一次，落盘后 600→644）；同时把 `get_file(allow_return_url=True)` 改成 `False` 强制本地路径（`True` 可能返回 URL，而 Hermes 侧读不到 URL，聊天门也没 `web` 工具集）。
3. **重启生效**：改 chat profile config → 重启聊天门（`docker exec hermes /command/s6-svc -r /run/service/gateway-chat`）；改插件 → `docker restart astrbot`（插件目录是 bind mount，重启即加载）。
4. **验证三件套（缺一不算过）**：
   - `s6-svstat` 显示 `up` + 端口在听（`curl -o /dev/null -w '%{http_code}' 127.0.0.1:8643/v1/models` 返 **401** = 服务活着但需鉴权）
   - 插件离线自测：`docker exec astrbot python3 /AstrBot/data/plugins/hermes_forward/self_test.py` → 应「全部通过」
   - **行为测试**：`POST http://127.0.0.1:8643/v1/chat/completions`（Bearer = chat profile `.env` 里的 key）让它 ① 列附件目录 ② 读某文件并原样引用前几行 ③ 写一个文件再读回；**写盘结果必须在文件系统上独立复核**——模型的自我报告不算证据。

**边界（别对主人夸大也别隐瞒）**：`HERMES_WRITE_SAFE_ROOT=/opt/data` 是**容器级 env、两个门共享** → 单容器方案下没法只收窄聊天门（要按服务收窄，得在 `gateway-chat` 的 s6 run 脚本里单独 export）；指令文件写入仍被 `security.protected_instruction_files=true` + `approvals.unattended_mode=deny` 挡住（实测），但 `stack/`（napcat/astrbot 数据、凭据）落在它的可写范围内 → 只能靠提示词约束。

**主人对这个形态的硬要求（2026-09-22）**：**不要为分层再起容器** —— 聊天门与干活门必须同一个 Hermes 容器（`hermes -p chat` + s6 `gateway-chat`），他在 docker 列表里看到多出来的容器会当成残留。想「bot 侧自己编排 + Hermes 只干活」属于**换架构**，先算清代价：聊天侧要自己配 LLM provider（AstrBot 现 `providers: []`、`provider_settings.enable=false`，纯转发）、人设与人设记忆会分叉；Hindsight 本身可配置级接入（有 MCP 端点，`mcp_server.json` 填上即用），**抽取规则本就在 bank 侧、与调用方解耦**。默认结论：维持「一个脑子两个出口」。

**已知坑**：`gateway-chat` 开机可能以 exitcode 78 退出，而 `finish` 脚本对 78 **拒绝自动重启** → 服务永久 down（`s6-svstat` 会显示 `down (exitcode 78) … normally up`）；只有 `s6-svc -u` 能拉起（**`-r` 对已 down 的服务无效**）。
