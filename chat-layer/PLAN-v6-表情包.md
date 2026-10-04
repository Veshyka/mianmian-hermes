# PLAN-v6 · 表情包（发图 / 偷图 / 读情绪 / 库存）

> 立项：2026-10-03。作者：棉棉（干活门）。
> **状态：P1 + P2 已落地（2026-10-03 06:35 CST），403 项单测全绿；P3 只做了「按情绪随机挑」，
> 「拼图 + VLM 选图」没做（主人 2026-10-03 明确：不用 VLM）。**
> 主人当时的指令：「你自己全部都开始推进吧，上限可以给到 200 张 / 1G」「选不用 vlm，只用同情感/标签随机选就好，
> 帖表情回应肯定要，毕竟相当于真人回复发表情」。
> 需求来源（主人原话）：**「我发什么她就能回复什么就好了，包括用得更多的是图片那种被收藏后的表情包；收到这种表情包式的图片不应该描述图片，应该结合上下文去分析情感，把这个情感合并到文字一起看。和 MaiBot 一样应该做到可以偷我/群友表情包，并且自己看情况根据表情的情绪随机选一个发，存储表情应该有个上限（这个如果不好实现就算了）。」**

---

## 0. 调研结论（一手证据，先说清哪些是实锤）

### 0.1 入站：怎么区分「表情包」和「普通照片」——**有硬判据**

来源：NapCat 源码 `packages/napcat-onebot/api/msg.ts`（picElement 转换器 ~123-151 行、marketFaceElement 转换器 ~258-280 行）、`packages/napcat-core/types/msg.ts`（PicSubType 枚举）。本地副本 `/opt/data/tmp/sticker-recon/nc_src/`。

| 类型 | 上报形状 | 判据 |
|---|---|---|
| 普通照片 | `type=image`，`data.sub_type=0`（KNORMAL） | — |
| **收藏/自定义表情（含动图）—— 主人说的那种** | `type=image`，**`data.sub_type=1`（KCUSTOM）**、`data.summary`（外显文字，如「[动画表情]」）、`data.file`（QQ 原始文件名，动图多为 `.gif`） | **`sub_type==1`** |
| 热图 | `sub_type=2`（KHOT） | 也算表情 |
| **商城/超级表情** | 也是 `type=image`，但带 `emoji_id` / `emoji_package_id` / `key`，`file` 固定形如 `xx-<id>.gif`，`url` 走 `gxh.vip.qq.com`，**不带 `sub_type`** | 命中 `emoji_id` 即商城表情 |
| QQ 原生系统表情（`face` 段） | `type=face`，`data.id` | 单独一类，**不入表情包库**（MaiBot 也只转成文本名） |

- 这些字段**不需要开任何配置**，转换器无条件写入（`disableGetUrl` 只影响 `url`）。
- 参考：MaiBot 的判据更宽——`NapCatInboundCodec._build_image_like_segment()`：`sub_type not in {0,4,9}` 就算表情包。社区插件 Astraea35 用 `sub_type ∈ {1,7,8,9}` 或 `summary` 含「表情/动画/热图」。
- **建议我方判据**：`sub_type == 1` 或 `sub_type == 2` 或带 `emoji_id` → 表情包；`summary` 含「表情/动画」作为补充。**`sub_type=0` 一律当照片。**

### 0.2 出站：怎么把图/表情包发出去——**路是通的，且不需要新工具**

- NapCat 的 `image.data.file` 接受四种形式（`napcat-common/src/file.ts:378-440` `checkUriType`）：**本地绝对路径**（推荐，零拷贝）、`file://`、`http(s) URL`、`base64://`（`base64:`/`data:` 同支）。相对路径不可靠（靠 NapCat 进程 CWD 撞）。
- **`data.sub_type=1` 就是「当表情包发」**（一路传到 `picElement.picSubType` / `uploadFile` 的 `elementSubType`，`napcat-onebot/api/file.ts:62-88`）；`data.summary` 控制外显文字。
- 本地文件发送**不需要**任何开关（`enableLocalFile2Url` 那个键在本机是 false，且它的消费点在所有取到的源码里都没找到 → 作用未证实，但**不需要它**）。
- 大小/格式：源码只对 `fileSize==0` 报错，**没有显式上限**；真实上限由 QQ 服务端定（未查实，不谎称"无限制"）。
- 我方现状：**她的正文发不了图**——`build_action()`（`onebot_proto.py:241-266`）把正文硬编码成单个 `text` 段。但同一文件里已经有 `cq_to_segments()`（群里 `{at}` 用的就是它）。**让它接管正文 → 她的正文里写 CQ 码就能发图/表情包，零新工具。**
- MaiBot 对照：发的是 `{type:image, data:{file:"base64://<b64>", sub_type:1, summary:"[动画表情]"}}`（`segment_encoder.py:153-174`）——和上面完全一致，做法互证。

### 0.3 MaiBot 的表情包子系统（照抄哪些、丢哪些）

| 环节 | MaiBot 做法（依据 `src/emoji_system/emoji_manager.py` 等） | 我方取舍 |
|---|---|---|
| 判定 | `sub_type not in {0,4,9}` → emoji | **照抄判据**（`{1,2}` + `emoji_id`） |
| 落库 | `data/emoji/<sha256>.<ext>`；DB `Images` 表（`image_hash`/`description`/`query_count`/`last_used_time`/`is_registered`）；**文件层 + DB 层双去重**，`asyncio.Lock` 按 hash 串行化 | **抄**：sha256 命名 + 去重（我方落盘已是 sha256 命名，天然去重）；索引改用**纯文本一行一条**（她只有 file 工具、没有 execute_code，JSON 容易写坏） |
| 打标 | **VLM 看图**打 1–5 个情绪/场景标签（prompt：『提取这个表情包主要表达的情绪、语气或常见使用场景标签，最多5个，逗号分隔』）；GIF 先抽帧横向拼图再送 VLM；**不看偷来时的上下文** | **抄思路**，但默认走**离线批处理**（本地视觉模型，省钱、不占她回合），失败就把该条标 `未标` |
| 发送 | LLM 自己决定调 `send_emoji` 工具 → `random.sample` 抽 25 张 → 拼成带序号网格图 → **VLM 子代理**按上下文选一张 → 发 | **先不做拼图 VLM**：她 `read_file` 索引（带情绪标签）自己挑、正文发 CQ 码。够用、零基础设施。拼图选法列为可选进阶 |
| 上限 | `max_reg_num=64` 张；单文件 ≤5MB；满额时权重 `1/(query_count+1)` 采样候选 → **LLM 决策替换哪张** | **抄上限概念，简化淘汰**：条数上限 + 总体积上限 + 按 `last_used` LRU 淘汰（不用 LLM 决策）。主人说"不好实现就算了"——**这个好实现** |
| 清理 | 每 6h 清「未注册且 30 天未使用」的缓存文件；已注册的永不删 | 抄：接进现有 cron，保留天数可配 |
| 频率 | **全仓未找到**频率/冷却配置，发送时机全由模型工具调用决定 | 我方补上（群里需要克制） |

---

## 1. 目标（可验收）

1. **她能发图**：私聊/群里，正文里能带图片段（本地文件或 URL），当表情包发时带 `sub_type=1`。
2. **入站表情包 ≠ 照片**：她收到的正文能区分「表情包」和「照片」，**表情包不描述画面**，只读情绪并入文字理解。
3. **偷表情包**：主人/群友发的表情包（含动图）自动进她自己的表情库，去重、有上限。
4. **按情绪发表情包**：她能按当前对话情绪从库里挑一个发（有随机性、有频率克制）。
5. **可回滚**：全部改动有备份 + 单测 + 回滚脚本。

## 2. 分期与改动面

### P1 · 最小可用（1 个文件 + SOUL，先做这批）

| # | 改动 | 文件 |
|---|---|---|
| 1 | `build_action()` 正文改走 `cq_to_segments()`（**保留**原有"无 CQ 码时行为逐字不变"） | `onebot_proto.py` |
| 2 | 出站表情包段带 `sub_type=1` + `summary`（在 CQ 码里显式写，适配器不猜） | 同上 + SOUL |
| 3 | 入站：判为表情包时正文占位从 `[图片:<path>]` 改成 **`[表情包:<path>]`**（附 `summary`）；照片仍是 `[图片:<path>]` | `onebot_proto.py` `_media_placeholder()` / `with_media_paths()` |
| 4 | SOUL：① 表情包不描述、只读情绪并入文字 ② 要发图/发表情包怎么写（CQ 码示例） | `profiles/chat/SOUL.md` + 共享 `提示词.txt` |

### P2 · 表情库（偷 + 上限 + 索引）

| # | 改动 | 说明 |
|---|---|---|
| 5 | 入站表情包落盘后**顺手入库**：`<profile>/onebot-stickers/<sha256>.<ext>`，同 hash 跳过 | 复用现有 `_save_blob` 三级兜底 |
| 6 | 索引 `onebot-stickers/index.txt`：**一行一条** `情绪标签 | 路径 | 大小 | 最后使用` | 她 `read_file` 直接看 |
| 7 | 上限与淘汰：条数（默认 200）+ 总体积（默认 100MB），超限按 `last_used` LRU 删；单张 ≤5MB | 抄 MaiBot 的量级，淘汰算法简化 |
| 8 | 清理 cron：未使用超 N 天（默认 30）删除 | 接现有 cron |
| 9 | 打标：离线批处理（本地视觉模型）给每条 1–5 个情绪标签，写回索引；失败标 `未标` | 不占她回合 |

### P3 · 可选进阶（先不做，列着）

- 拼图 + VLM 子代理选图（MaiBot 式）
- 贴表情回应（`set_msg_emoji_like`，依赖 `emoji_id` 编号表——主人还没定）
- 表格化 DB 索引（她拿不到 DB，收益低）

## 3. 验收标准

1. 私聊发一张本地图/一个表情包 → 真到达（主人肉眼确认），且 `sub_type=1` 的那条在 QQ 里显示为表情样式。
2. 主人私聊发一个收藏表情包 → 她的正文里出现 `[表情包:<path>]`，且她**不描述画面**、把情绪并进文字回复。
3. 主人发 20 个不同表情包 → 库里去重后正好 20 条（同 hash 只留一条）。
4. 库里超过上限时 → 删的是最久没用的那条（可复核 `last_used`）。
5. 让她在某个情绪下发表情包 → 发出的图来自库里、且标签匹配（同一情绪连发两次可能不同 → 有随机性）。
6. 全量单测绿；回滚脚本演练通过。

## 4. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 改 `build_action` 波及**所有出站文本** | 只加"文本里的 CQ 码 → 段"这一步；无 CQ 码时行为逐字不变；单测覆盖"正文含字面 `[CQ:`"的边界 |
| 偷到不合适的表情包（涩图/猎奇） | ① 单张大小限制 ② 可选内容过滤（默认**关**，MaiBot 也是默认关）③ 手动删除：私聊给一条脚本命令 |
| 群里发图打扰 | SOUL 规则：群里默认克制（同一群有冷却、不许连发） |
| 表情包识别错判（把照片当表情包） | 判据用**字段**不用启发式；判不出时按照片处理（保守），并在正文里保留 `summary` 供她参考 |
| 落盘目录继续累积 | 本次一并接清理 cron（含 9/23 就欠着的 `onebot-media/`） |

## 5. 待主人拍板

1. **P1 先做吗？**（她能发图 + 入站区分表情包/照片 + SOUL 规则；改动面 1 个文件 + SOUL）
2. **表情包库上限**：条数 200 / 100MB 这个量级行不行？（MaiBot 默认只有 64 张）
3. **偷的范围**：私聊 + 群聊都偷，还是只偷群聊/只偷主人的？
4. **内容过滤**：偷进来的图要不要先过一道"是否合适"的检查（默认关）？
5. **打标用哪个视觉模型**：本地模型（省钱、质量待看）还是走云（质量好、花钱）？
6. **群里发图频率**：要不要设"每个群 X 分钟最多一张"的硬限？

## 6. 回滚

- 代码：插件目录整体备份 + 逐文件 sha256（沿用 9/23 的 `rollback_emoji_media.sh` 模式，新增文件一并登记）
- 配置：`group_stickers` 段缺失即视为关（无感降级）
- SOUL：`SOUL.md.bak-before-*` 现成
- 数据：`onebot-stickers/` 只增不删既有数据；LRU 删除**只删本功能自己库里的文件**

---

## 7. 交付清单（2026-10-03 实际落地）

| 件 | 位置 | 说明 |
|---|---|---|
| 表情包库模块 | `profiles/chat/plugins/onebot/sticker_lib.py`（新，~440 行） | 判据引用 proto（单一实现）／sha256 去重／索引纯文本／200 张 · 1G · LRU／gc／`tag` `pick` `list` `stats` CLI |
| 协议层 | `onebot_proto.py` | `is_sticker_image()`／`sticker_label()`；`_render` 把表情包渲染成 `[表情包]`（照片仍是 `[图片]`）；`with_media_paths()` 支持两种占位；**`build_action()` 正文走 `cq_to_segments()`**（她发文/发图的唯一通道）；`EMOJI_LIKE_IDS`（26 个名字，两源交叉核对）+ `extract_emoji_like_markers()` |
| 适配器 | `adapter.py` | 入站表情包顺手入库（`_register_sticker`）；`_last_inbound_mid` 记账；出站剥 `[贴表情:x]` → 发送成功后 `set_msg_emoji_like`（`_apply_emoji_likes`，默认贴「她正在回的那条」）；health_state 加 9 个计数 |
| 单测 | `tests/check_sticker.py`（新，39 项） | 判据／渲染／回填／CQ 出站／贴表情标记／库的增删改查与上限／端到端（假协议端） |
| 旧测试同步 | `tests/check_group_admin.py`（2 处断言） | 因 `build_action` 现在把 CQ 码转真段，欢迎语/违禁词警告的 `{at}` 变成真 `at` 段 —— 断言改成更强的「真 at 段 + 不许有字面 CQ」 |
| 配置 | `profiles/chat/config.yaml` | `stickers_enabled / stickers_max_items: 200 / stickers_max_total_bytes: 1G / stickers_retention_days: 30 / emoji_like_enabled` |
| 索引种子 | `profiles/chat/onebot-stickers/index.txt` | 带表头注释（格式 + 怎么发 + 怎么打标 + 上限） |
| 清理任务 | cron `e4b5357a223d`（每天 04:45，`scripts/sticker_gc.sh`，no_agent） | 只在真删了东西时输出一行，平时哑的 |
| 提示词 | `profiles/chat/SOUL.md`（= 共享终稿 `提示词.txt`，7,922 字符） | 新增「表情包 · 发图」「贴表情回应」两节 |

**索引两版 bug（都被新单测抓出来，已修）**：
1. 秒级时间戳 → 同秒入库的几条 `last_used` 相同，LRU 变成随机删（改微秒）。
2. 用 `time.strptime` 解 `%f` → **全表解析失败 → `last_used` 全 0 → 按顺序删错人**（改用 `datetime.strptime`）。

**回滚**：`/opt/data/tmp/sticker-v6-backup-20261003-062353/`（改动前的 `onebot_proto.py` / `adapter.py` / `group_window.py` + `BEFORE.sha256`）；SOUL 备份 `SOUL.md.bak-before-表情包-20261003-063437`。

**没做的**：拼图 + VLM 选图（主人明确不用 VLM）；`emoji_id` 全表（只收了 26 个两源一致的名字）；贴表情对**真实消息**的真跑（要主人给一条可随便动的消息）。

## 附：本计划的证据来源

- NapCat 源码（tree `0b4cfe65ed889aa8e8061ee4c5b7edddec0d10f5`，副本 `/opt/data/tmp/sticker-recon/nc_src/`）：`napcat-onebot/api/msg.ts`、`napcat-core/types/msg.ts`、`packages/napcat-common/src/file.ts`、`napcat-onebot/api/file.ts`
- MaiBot（`Mai-with-u/MaiBot` main + `MaiBot-Napcat-Adapter`）：`src/emoji_system/emoji_manager.py`、`emoji_cache_cleanup.py`、`src/maisaka/builtin_tool/send_emoji.py`、`codecs/inbound/message_codec.py`、`codecs/outbound/segment_encoder.py`、`official_configs.py`
- 我方现役：`profiles/chat/plugins/onebot/{onebot_proto.py,adapter.py}`、`/opt/data/stack/napcat/config/onebot11_<BOT_QQ>.json`、`profiles/chat/onebot-state.json`
- 旧报告：`tmp/sticker-recon/REPORT-超级表情与可复用性.md`、`reports/emoji-media-impl-2026-09-23.md`、`chat-layer/PLAN-v5-超级表情与群管理.md`
