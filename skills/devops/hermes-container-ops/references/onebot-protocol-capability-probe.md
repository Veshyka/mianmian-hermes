# OneBot v11 协议端能力探测（NapCat）

问「某能力到底有没有」时，插件 README 和上游文档只说明**作者以为的**；本机协议端实际注册了哪些 action 只有探针能回答。源码能定「机制是什么」，能力清单只能实测。

## 怎么连

- 配置在 `stack/napcat/config/onebot11_<QQ>.json`：`network.httpServers[]` 给 host/port/token，`network.websocketClients[0].url` 是反向 WS（承载事件与 action 回调）。
- 探针走 **HTTP server**：`POST http://127.0.0.1:3000/<action>`，头 `Authorization: Bearer <token>`，体是参数字典。这条不碰反向 WS，**不会干扰正在跑的聊天链路**。
- 只读存活探针（无参、无副作用）：`get_version_info` / `get_status` / `get_login_info`。

## 出站能力硬结论：`face` 段发不了「大表情」（2026-10-03 实测定论）

**入站**（主人发 QQ 内置大表情时，NapCat 给的原始报文）：

```json
{"type":"face","data":{"id":"500",
  "raw":{"faceIndex":500,"faceText":"/秋秋赏月","faceType":3,"packId":"1","stickerId":"102",
         "sourceType":1,"stickerType":3,"resultId":"0","chainCount":1},
  "resultId":"0","chainCount":1}}
```
- 原生的**小黄脸没有 `raw`**（只有 `id`）；带 `raw.faceType=3` 的就是大表情。
- 可当标志的字段：`faceText`（名字）、`stickerId`、`packId`；报文里**没有 url/file/emoji_id/key**（所以也抓不到图）。
- 本机渲染（`onebot_proto._big_face_name()`）：带名字的渲染成 `[表情:500 秋秋赏月]`，原生仍是 `[表情:14]`。

**出站试过且全部失败的写法**（都是对**自己账号**发，零打扰）：

| 写法 | 结果 |
|---|---|
| `{"id":"500"}` / `+resultId,chainCount` / `+raw` / 原生字段平铺 / 字符串 `[CQ:face,id=500]` | ❌ `retcode=200 消息体无法解析` |
| `{"type":"mface","data":{"emoji_package_id":1,"emoji_id":"102","key":""}}` | ❌ 走到 QQ 后被拒 `result:-13100`（缺 `key`） |
| **对照**：`{"id":"14","foo":"bar"}`、`{"id":"14","faceType":3,"ext":"…"}` | ✅ **成功**（后者发出的是普通「微笑」） |

⇒ **两个定论**：① `face` 段**只读 `id`**，`faceType`/`ext`/`raw` 等一律**静默忽略**；
② 因此编号 500 永远被当成非法系统表情。**大表情在 OneBot 层无法回发**，想做成只能改上游
（让 face 段接受这些字段 / 新增段类型）或用 `send_packet` 自己拼 NT 包（需逆向、易随版本失效）。

**探针方法论（这次学到的）**：判「多余字段有没有被采纳」**不能只看 retcode** —— 必须用
**合法 id + 目标字段**发一次（`id=14,faceType=3` → 收到的是普通微笑 = 字段被忽略），否则
「retcode=0」会被误读成「字段生效了」。

## 错误三态：先校准探针，再下结论

NapCat 4.x 用同一段「不支持的Api」文案表达**两种完全不同**的意思。不校准就会把「参数写错」误判成「这个能力不存在」，据此给出一条错的结论。

| 返回 | 含义 |
|---|---|
| `{"retcode":200,"message":"不支持的Api <名字>"}` | **action 不存在**（名字原样出现） |
| `{"retcode":200,"message":"不支持的Api \"<参数名>\":<值>"}` | **action 存在，但参数名或类型不匹配**（引号里是参数，不是 action） |
| `{"retcode":400,"message":"Schema compilation error: Expected required property"}` | **action 存在**，必填参数没给 |
| `{"retcode":0,"status":"ok","data":…}` | 存在且调用成功 |

**校准动作**：先打一个确定不存在的名字（如 `definitely_not_real`）和一个确定存在的只读无参 action（`get_group_list`），确认两种文案各长什么样，再去测目标。跳过这步就会读错。

> 判据先于结论：拿一次「不存在」的返回值当基线，才知道后面那些失败的返回值该怎么读。

⚠️ **校准只对「当时那个探针」有效 —— 探针自己改过就要重校**。最阴的一类假阴性是**探针脚本本身写错**：用带 `shift` 的 shell 函数包探针（`probe(){ echo "$1"; shift; curl "…/$1" … -d "${2:-{}}"; }`）时，`shift` 之后 `$1`/`$2` 整体前移，**参数被拼进了 URL 路径**，NapCat 于是回一句 `不支持的Api "<参数名>":<值>`——和「参数不匹配」那一态长得一模一样，被读成「schema 拒绝了这个参数」，据此写出了一条错的结论并进了报告。**探针函数一律不 `shift`，用命名变量接参**（`action=$1; body=$2`）；改完探针先拿已知阳性 + 已知阴性各跑一次再信它。

## 探针必须无副作用

- 目标 id 一律用**不存在的**：`message_id=1`、`user_id=1`、`group_id=1` → 只会返回 `msg not found` / `消息不存在` / `real fileUUID not found!`，不会真发消息、不会改群、不会真戳人。
- ⚠️ **`send_poke` 对不存在的 user_id 返回 `ok`（静默 no-op）**——它是**写**操作，不是读。别拿它当选型证据，也别在报告里把它写成「探测成功」。真要验一条会改状态的 action，先想清楚最坏后果再发。
- 用存在性探针（空参数 `{}`）批量筛时，**只发读类候选**：无参又有副作用的 action 会当场执行。

## 参数类型坑

- `set_msg_emoji_like` 的 `message_id` / `emoji_id`：**数字与字符串都被接受**（8 种组合实测全过 schema，都进到业务层回 `msg not found`）。本节此前记的「`emoji_id` 必须传数字」是**错的**，根因见上一节那个 `shift` 探针坑。
- 业务层报错反而是「参数已通过」的证明：对一个不存在的 message_id，`set_msg_emoji_like` 回 `{"retcode":200,"message":"msg not found"}` —— 能走到这一步说明 action 名与参数形状都对，只差一个真实目标。

## 本机实测结论（NapCat 4.18.28 / OneBot v11）

**存在：**
- `set_msg_emoji_like`（`message_id` + `emoji_id` 必填）—— 给消息贴表情回应，**写**侧
- `get_emoji_likes` / `fetch_emoji_like` —— 读某条消息收到的表情回应（`fetch_` 那条带分页 cookie）
- **账号自己收藏的表情包（AstrBot 插件生态的空白，要用得自己写）**：`fetch_custom_face`（实测直接返回收藏表情的 CDN URL 列表）、`fetch_custom_face_detail`（带协议端本地 `emoPath`）、`add_custom_face` / `delete_custom_face` / `set_custom_face_desc`（增删改）—— 查过的 AstrBot 表情类插件**没有一个用过这一组**
- `get_cookies`（有必填参数，可指定 domain）—— 协议端持有登录态 cookie 的唯一出口；所有 QQ 空间类方案都靠它拿 `p_skey` 再自算 `g_tk`
- `get_qun_album_list` / `upload_image_to_qun_album` / `get_group_album_media_list` —— 群相册读数与传图
- `send_poke` —— 戳一戳
- `get_msg` / `get_group_msg_history` / `get_forward_msg`
- `get_group_file_url` / `get_private_file_url` —— 群/私聊文件换直链（AstrBot 拿 `file` 段就是这么做的）
- `get_image` / `download_file` —— 媒体取回
- `mark_msg_as_read`、`set_input_status`、`upload_group_file`、`upload_private_file`、`get_file`、`get_friend_msg_history`、`send_group_sign`、`set_friend_remark`、`get_stranger_info`、`get_group_info`、`get_group_member_list`

**不存在（已实测，返「不支持的Api <名字>」）：**
- `set_group_reaction`、`get_msg_emoji_like`、`get_group_emoji_like` —— 没有这些别名，只有 `set_msg_emoji_like` / `get_emoji_likes`
- `get_self_info`（v11 标准里有，这个实现没注册；用 `get_login_info`）
- **QQ 空间一族全无**：`get_qzone_feed` / `get_qzone_msg` / `publish_qzone` / `upload_qzone_img`。QQ 空间能力在协议端这一层压根没有接口，别在 OneBot 上找它。

## 消息段侧：协议端不挡，挡的是客户端

`send_group_msg` / `send_private_msg` 的 `message` 数组接受任意段（含 `face`、`image`、`record`、`video`、`file`）——**协议端不限制**。能不能发出去取决于客户端有没有构造这些段，不是协议端不支持。给「发不了图/表情」下结论前，先分清是链路哪一头的能力缺失。

**表情段的 id 字段名（客户端要渲染出「哪个表情」就靠它）**：`face` → `data.id`（QQ 内置表情编号）；`mface`（商城小表情）→ `emoji_package_id` + `emoji_id`；`marketface`（超级表情）→ 字段不稳，`summary` 常是自带方括号的名字。这几个名字来自官方 schema 与我方合成事件，2026-10-03 已在真机验证（face 送达 + schema 逐字段核对）。协议端与 AstrBot 都**不提供** face id → 表情名 的对照表，但 **NapCat 自带一份原生表情表** `qq_emoji_list.QQ_FACE`（219 项，0–128563），出站白名单直接用它。

## 出站段的**类型**与**取值**：两个都错才叫「消息体无法解析」（2026-10-03 实证）

`retcode=1200/200` + `message=消息体无法解析, 请检查是否发送了不支持的消息类型` 有**两个独立成因**，只修一个还会复现：

**(1) 字段类型**（依据 `packages/napcat-onebot/types/message.ts`，一手）：

| 段 | 字段 | schema 要求 |
|---|---|---|
| `face` | `id` | **String**（发数字必被拒） |
| `at` | `qq` | **String**（qq 号或 `"all"`） |
| `reply` | `id` | **String** |
| `image` | `file` | String |
| `image` | `sub_type` | **Number**（唯一必须数字的段字段） |
| `mface` | `emoji_package_id` | **Number**；`emoji_id` / `key` 是 String |

所以「把 CQ 码里的数字统一 `int()`」这种写法会同时埋掉 `face` 和 `at` —— **类型必须按段类型给**，未知段保守留字符串。`[CQ:at,qq=…]` 一错，群 @ 与「入群欢迎语」这类整条消息都发不出去，且只在真机才暴露（单测只看自己拼的字典）。

**(2) 取值合法性**：`face.id` 必须是**原生小黄脸**。实测（HTTP 探针）：`id="14"` → `retcode 0`；`id="500"` → `retcode 200 消息体无法解析`。500 不在 `QQ_FACE` 表里 —— 它是 QQ 的**大表情/超级表情**，只能走图片路由，当 `face` 段发永远失败。

- ⚠️ **入站能收到 ≠ 出站能发回去**：入站 `face` 段会出现非原生编号（模型看到 `[表情:500]` 照抄回发就是上面这个坑）。出站前拿 `QQ_FACE` 当白名单，非法编号**丢掉该段**（其余文本照常发；整条只剩它时退化成本字 `[表情:500]`），比让它拖垮整条消息好。
- **探测格式问题的正确目标 = 自己的账号**：往**不存在的 user_id** 发时 NapCat **先查用户、后解析消息体**（纯文本/合法 face/非法 face 三种都回 `无法获取用户信息`），分辨不出格式 → 用 `user_id=<登录号>`（消息只出现在自己与自己的会话里，真人看不见），能拿到 diff 级的对照。
- 排障入口：`state.db` 的 `delivery_obligations` 表存的是**投递内容原文 + last_error**，重启后没送出的那条会被重新投递（旧进程序列化的段不会保留，但错误会带着旧内容再报一次，别误判成新代码又坏了）。

## AstrBot 源码的参考位置（读，不搬）

源码树 `/opt/data/abr/AstrBotDevs-AstrBot-b065505/`：

- `astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py` — 事件段 → 内部组件的映射。`t == "mface"` 直接 `continue`（**AstrBot 自己也把 mface 丢掉**）；`face` 走通用分支 `ComponentTypes[t](**m["data"])`。
- 同文件 `file` 段：`data.url` 以 http 开头（Lagrange）直接用；否则调 `get_group_file_url` / `get_private_file_url` 换直链 —— **拿媒体 URL 的现成套路**。
- `reply` 段：调 `get_msg` 把被引用消息取回来重建成组件。
- `astrbot/core/message/components.py` — `ComponentTypes` 字典列出全部段类型；`Face` 只有 `id: int` 一个字段。
- **AstrBot 没有 face id → 表情名 的映射表**（全树搜 `face_id` 只有 satori 适配器把它渲染成 `[表情ID:x]`）→ 要让模型「看懂」表情，映射得自己建。

## Hermes 原生 onebot 适配器（`profiles/chat/plugins/onebot/`）

**已落地**（改完必须重启 chat 网关才生效 —— 插件是平台适配器，改文件不等于换版本）：

- `onebot_proto._face_label()` — 表情段带 id 渲染：`face` 取 `data.id` → `[表情:14]`；`mface` → `[表情包:4321/9988]`；`marketface` 优先 id、无 id 时退 `summary`（剥掉自带方括号）。id 全缺则退回无 id 占位，**绝不返回空串**（空串会让整条消息被判 `no_text` 丢掉）
- `onebot_proto.image_segments()` / `with_media_paths()` — **纯函数**：按序抽出 image 段、把正文占位按序回填成本地路径。IO 留在适配器里，proto 保持可离线单测
- `onebot_proto.build_emoji_like_action()` + `adapter.emoji_like()` — 出站贴表情，复用 `_call_action` 的重试与记账。**贴表情不是消息**，别把它塞进 `send()`（那条路会走分段、分隔符清洗、静默标记）
- `adapter._persist_images()` / `_persist_one_image()` / `_save_blob()` — 图片落盘三级兜底：`get_image` 响应里的 base64 → 同响应里的协议端本地路径 → 段自带 url 直连下载
- `adapter._text_with_media()` — 在 `_ingest()` 里挂在**唤醒判定之后**

**已落地**：`build_action()` 委托 `cq_to_segments()` → 模型**在正文里写 CQ 码就能发图/发原生表情/真 @**（群 @ 与「入群欢迎语」走同一条），`send()` 的分段与静默标记链路一字未动。委托后的不变量：**正文无 CQ 码时段序列逐字不变**；数值化的 key **按段类型给**（`_CQ_NUM_KEYS_BY_TYPE`，只在 `image.sub_type` / `mface.emoji_package_id` 上转数字），全局集合那种写法会同时埋掉 `face` 与 `at`。

**仍未动**：`group_window.append()` 只落 `ts/t/uid/name/text` 五个字段，媒体一律不落地。

### 出站闸门：吞掉 Hermes 自己产生的噪声文案

Hermes 会在**人回合**上把她只回静默标记的那一轮**换掉正文**：`gateway/run_turn.py` 把内容替成一段机器文案
（`⚠️ The model returned only a silence marker …`）并记一行 `silence marker rejected on a user turn`。这段文案
会像她的回复一样进 `send()` → 主人收到一条机器腔的告警。

- **闸门写在适配器的 `send()` 里**：拿**特征子串**匹配（`The model returned only a silence marker`），
  **不要匹配 emoji 前缀**——emoji 在日志/终端里容易被吃。命中则返回「成功但不发」，并**计数 + 一行 INFO**
  （吞掉不等于不管：日志里要留得下痕迹，否则以后无从取证）。
- **闸门位置紧跟在斜坡/活动记账之后、真发包之前**：被吞掉的文案不算「她开口了」，不能把主动起头的斜坡归零。
- **定位它从哪个回合来**：拿日志里那行 `silence marker rejected on a user turn` 找时间与会话，区分「模型自己回的标记」与「Hermes 换成的机器文案」。
- ⚠️ **动一个「吞东西」的条件前，先 grep 测试树里钉着的既有决定**。本机实测：把「只在群里吞静默标记」
  顺手扩成「群/私聊都吞」时，被一条早已存在的钉子用例抓个正着（私聊里她**不能**拿静默标记挡主人）。
  这种条件往往来自用户拍板的决定，不在代码注释里。做法：`grep -rln "<那个符号/行为>" tests/` → 读中那条用例的
docstring → 只改被要求改的那部分 → 在代码里写清「哪个分支有意不动、为何」，并在回报里点名它没动。

**加白名单闸时测试要盖三态**（只盖一种就会把合法表情误杀、或让非法编号继续拖垮整条消息）：① 非法编号被丢、同轮其余文本照发 ② 整条只剩非法编号 → 退化成本字 `[表情:N]`（不静默吞、不发空消息）③ 表内编号一个都不能被误伤（取 0、14、307、326、表里最大值各测一个）。端到端那层用假协议端断言「非法段的字节**从未上过线**」，比断言返回值强。

**三条实现约定（照这个走）**：

1. **落盘位置与命名**：`<profile>/onebot-media/<YYYY-MM-DD>/<内容 sha256 前 16 位>.<ext>` —— 按内容命名天然去重。开关 `media_download_enabled`（默认 true）、单张上限 `media_max_bytes`、单条张数上限 `media_max_per_msg`，计数进 `health_state()` 便于心跳观察。
2. **只对「会进 agent 回合」的消息下载**：私聊 + 被唤醒的群消息。群窗口采集**不下载** —— `_collect_group()` 跑在唤醒判定之前，在那里下载等于把每条群消息的图都拉一遍（磁盘/带宽/时间都不划算）。别为了「窗口里也有路径」去调换判定顺序：那会动到门控状态喂入的时序语义，风险大于收益。
3. **回填占位别丢右括号**：`text.split("[图片]")` **会把右括号一起吃掉**，回填时必须自己补 `]`（`head = placeholder[:-1]` 再 `f"{head}:{p}]"`），否则正文里出现半截 `[图片]:/path`。凡是用 `split` 替换带括号占位符的地方都有这个坑。

**怎么验（全程不碰真实会话）**：`tests/run_tests.sh` 跑全量（stdlib unittest，文件名必须是 `check_` 前缀）。`MockNapCat` 是进程内假协议端，**子类化它并覆写 `_reply_for()`** 就能造出 `get_image` 返回 base64 这类定制响应（父类对所有 action 只回 `message_id`）。给「效果类」改动做端到端取证时走它 —— 真反向 WS、真适配器代码路径，但**别去真实群/私聊里试**：本机几个群每个都有主人的账号在，贴表情、发消息这类动作他看得见。

**改动既有的 `check_*.py` 断言时要说明**：行为是有意改的（如占位符从 `[表情]` 变 `[表情:14]`），旧断言会红——把这些断言更新到新行为，并在回执里点明「我动了既有测试的哪几处」，不要让它看起来像悄悄改测试迁就实现。

## 入站：怎么把「表情包」和「照片」分开（一手 NapCat 源码）

QQ NT 的两种元素**都**被上报成 `type=image`，判据在字段、不在启发式（`napcat-onebot/api/msg.ts` 的 picElement / marketFaceElement 转换器；枚举在 `napcat-core/types/msg.ts`）：

| 上游元素 | 上报形状 | 判据 |
|---|---|---|
| 普通照片 | `data.sub_type=0`（KNORMAL） | 就是照片 |
| **收藏 / 自定义表情（含动图）** | `data.sub_type=1`（KCUSTOM）+ `data.summary`（外显文字，动图常是「[动画表情]」）+ `data.file`（QQ 原始文件名，动图多为 `.gif`） | **`sub_type==1`** |
| 热图 | `sub_type=2`（KHOT） | 也算表情 |
| 商城 / 超级表情 | 另有 `emoji_id` / `emoji_package_id` / `key`，`file` 固定形如 `xx-<emojiId>.gif`，`url` 走 `gxh.vip.qq.com` | 命中 `emoji_id` |
| QQ 内置表情 | 另一条链：`type=face` + `data.id` | 单独一类，别混进表情库 |

- 这些字段**不需要任何协议端配置**（转换器无条件写入；`disableGetUrl` 只影响 `url`）；**没有 `is_emoji` 这种字段**（源码全仓零命中）——判据要自己按上表写。
- 参考实现：MaiBot 的 `NapCatInboundCodec` 用 `sub_type not in {0,4,9}` 判表情；社区插件（Astraea35）用 `sub_type ∈ {1,7,8,9}` 或 `summary` 含「表情/动画/热图」。**判不出时按照片走**（保守），正文里保留 `summary` 供模型参考。
- 扩展名（`.gif`/`.jpg`）**不是**可靠判据，只有字段是。

## 出站：把图 / 表情包发出去

`image.data.file` 接受四种形式（`napcat-common/src/file.ts` 的 `checkUriType` / `uriToLocalFile`）：

| 形式 | 说明 |
|---|---|
| **本地绝对路径** | 首选，零拷贝（`fs.existsSync(path.normalize(...))` 直接读） |
| `file://` / `file:///` | 剥前缀后当本地路径 |
| `http(s):` | 下载到临时目录（受 `imageDownloadProxy` 影响） |
| `base64://`（`base64:`、`data:…;base64,` 同支） | 解码后写临时文件 |

- **相对路径不可靠**：没有专门分支，只能撞 NapCat 进程 CWD，一律用绝对路径。
- **发本地文件不需要开关**：`enableLocalFile2Url`（本机 false）的消费点在能取到的源码里找不到——发本地图不依赖它（它的确切作用未证实，别写进结论）。
- **`data.sub_type=1` 就是「当表情包发」**（一路传到 `picElement.picSubType` 与 `uploadFile` 的 `elementSubType`）；`data.summary` 控制外显文字；普通图用 `sub_type=0`。
- 大小/格式：源码只对 `fileSize==0` 报错，**没有显式上限**——真实上限由 QQ 服务端定，别写成「无限制」。

## 表情包库（偷图 / 情绪标签 / 上限）的设计参照

MaiBot（`Mai-with-u/MaiBot` + `MaiBot-Napcat-Adapter`）里可直接抄的：

- 落库 `<sha256>.<ext>`，**文件层 + DB 层双去重**（DB 按 `image_hash` 命中即复用），同一 hash 的写入用锁串行化。
- **情绪标签由视觉模型看图生成**（prompt 形如「提取这个表情包主要表达的情绪、语气或常见使用场景标签，最多 5 个」），**不记「偷来时的上下文情绪」**；GIF 先抽帧横向拼图再送视觉模型。
- 发送 = `image` 段 + `sub_type=1` + `summary="[动画表情]"`，`file` 用 `base64://`。
- 上限：可发条数（默认 64）+ 单文件 5MB；满额且允许替换时，用权重 `1/(query_count+1)` 采样候选再让 LLM 决定取消注册哪一张。
- 定时清理：删「未注册且 N 天未使用」的缓存与孤儿文件，**已注册的永不删**。
- **负信号值得记**：它全仓**没有**发送频率/冷却配置——「什么时候该发表情」这件事得自己做（提示词规则 + 冷却计数），别指望框架层给。

我方取舍（该门只有 `file` 工具、没有 `execute_code`）：索引写成**纯文本一行一条**（JSON 容易写坏）、情绪标签走**离线批处理**（省 token、不占回合）、超限用 **LRU** 淘汰而不是 LLM 决策。完整分期计划见 `<项目目录>/PLAN-v6-表情包.md`（chat-layer）。
