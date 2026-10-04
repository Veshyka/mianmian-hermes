# 调研：QQ空间自动发说说 / 群管理 —— 现成插件生态 & 能否搬进我们的 Hermes onebot 适配器

调研日期：2026-09-23 · 调研者：Hermes 子代理（只读 + 沙箱探针）
工作目录：`/opt/data` · 未改动任何现役配置、未重启服务、未向任何群/人发真实消息

---

## 0. 三个判定（先给结论）

**判定 1 —— 哪些现成能装、哪些要从零写**

| 能力 | 现成度 | 说明 |
|---|---|---|
| QQ空间发/看/评说说 | **现成**（有成熟仓库） | 但只在 AstrBot 里"现成"；搬到我们这边等于**照协议重写**，见 §1.5 |
| 群管工具集（禁言/踢/全禁/名片/头衔/管理员/精华/公告） | **现成**（AstrBot 侧 4~197 星多个仓库） | 协议端 action 本机实测全存在；我们适配器侧要从零写"壳" |
| 违禁词 / 刷屏检测 / AI 审核 | **现成**（AstrBot 侧） | 同上，逻辑可读可搬，运行时免谈 |
| 入群欢迎 / 入群审批 / 进群验证 | **现成**（AstrBot 侧，有多个） | 我们适配器还**丢掉了 notice/request 事件**，见 §3 |
| 撤回 / 防撤回 / 撤回跟随 | **现成**（AstrBot、MaiBot 侧都有） | 同上，需要先收 notice 事件 |
| 关键词回复 | **现成**（两边都有） | 纯文本匹配，跟我们适配器耦合最低，最容易自己实现 |
| 词云 / 发言统计 | **现成**（AstrBot：CloudRank ★30） | 需要全量落消息，与我们"群消息不入主库"的红线冲突，要单独设计 |
| 宵禁 / 定时打卡 / 群投票禁言 | **现成**（AstrBot、MaiBot 侧） | 都依赖"跑在某个框架里" |

**判定 2 —— 归属：AstrBot / MaiBot / 框架无关**

> **结论：本次找到的 QQ空间插件与群管理插件，100% 是 AstrBot 插件或 MaiBot 插件，没有一个是现成的"框架无关、可直接搬进 Hermes onebot 适配器"的。**

| 生态 | 运行时要求 | 能否直接装进我们的适配器 |
|---|---|---|
| AstrBot 插件（Star 基类 + `@register` + `astrbot.api.event.filter` + `AiocqhttpMessageEvent` + `context.platform_manager`） | **必须跑 AstrBot** | ❌ 不能。代码里 `import astrbot.*` 到处都是 |
| MaiBot 插件（manifest + MaiBot plugin SDK，capabilities 如 `api.call`/`send.text`） | **必须跑 MaiBot** | ❌ 不能 |
| 框架无关 | —— | **本次未找到**（搜过的关键词见 §5） |

**好消息**：两者最终都落到**同一批 OneBot v11 action**。也就是说——**能力在协议端全都有，缺的只是"在我们适配器里的那层壳"**。所以"不为了一个插件重新起整个 AstrBot"这个决策是站得住的：照 action 名重写壳即可。

**判定 3 —— 我们已经自己实现了的（别重复推荐）**

见 §4。摘要：分段发送、入站防抖、群滚动窗口、唤醒门控（三态 + `[SILENT]`）、群门控阈值、唤醒限流、主人/生人信任档、心跳自检 doctor、记忆隔离硬前置。**群管工具（禁言/踢/欢迎/违禁词/撤回）我们一个都没有；notice/request 事件目前被整条丢弃。**

---

## 1. QQ空间（qzone）

### 1.1 本地那份是什么 —— 不是插件本体，只是缓存目录

- `/opt/data/stack/astrbot/data/temp/astrbot_plugin_qzone/` 里**只有一个空的 `builtin_renderer/` 子目录，零个文件**。
- 这是插件的**运行期临时目录**（渲染器缓存），**不是插件源码**。插件源码在 `data/plugins/`，该目录 `root:root 0700`，当前 `hermes` 用户**读不到**（`ls: Permission denied`）。`data/plugins_disabled/`、`data/plugin_data/`、`data/plugins.json` 同样读不到。
- **侧面证据（确认当时装的就是它）**：`data/_apply_boundary.py:57` 的 persona 工具白名单里写着
  ```
  # qzone 插件（她自己发/看说说）
  "llm_publish_feed",
  "llm_view_feed",
  ```
  这两个工具名与上游 `Zhalslar/astrbot_plugin_qzone` 的 `core/llm_action.py` 注册的 LLM 工具一致 → 本地那份就是 Zhalslar 版。

### 1.2 上游仓库

| 项 | 值 | 来源 |
|---|---|---|
| 仓库 | `Zhalslar/astrbot_plugin_qzone` | GitHub Search API |
| ★ | **161** | GitHub API `repos/...`（2026-09-23 实查） |
| 最近推送 | **2026-06-18** | 同上 |
| 语言 | Python | 同上 |
| 许可证 | **GPL-3.0**（仓库 `LICENSE` 文件正文首行 = `GNU GENERAL PUBLIC LICENSE`） | GitHub Contents API 解 base64 实读 |
| ⚠️ 许可证矛盾 | README 顶部徽章写的是 `License-MIT`，**`LICENSE` 文件是 GPL-3.0**。以 `LICENSE` 文件为准；README 徽章是错的。**这条很关键**：直接拷它的代码会让我们的适配器被 GPL-3.0 传染 | 同两处对比 |
| 归档 | 否 | GitHub API |
| 同生态竞品 | `diaomin66/astrbot_plugin_qzone_ultra` ★47 **MIT**（2026-07-13，只看了元数据没读源码）；`roxy2233520/astrbot_plugin_qzone_publisher` ★1 AGPL-3.0（2026-09-23，主打定时发布）；`Catfish872/astrbot_plugin_qzoneActive` ★8 GPL-3.0；`Wyccotccy/astrbot_plugin_qzone_tools` ★45 无许可证 | GitHub Search API |

### 1.3 它怎么登录 —— 不扫码、不手抓 Cookie，走协议端 `get_cookies`

`core/qzone/session.py`：
```python
payload = await self.cfg.client.get_cookies(domain=self.DOMAIN)   # DOMAIN = "user.qzone.qq.com"
cookies_str = payload.get("cookies")
c = {k: v.value for k, v in SimpleCookie(cookies_str).items()}
uin = c.get("uin") ...           # 去掉前缀 'o'
skey  = c.get("skey", "")
p_skey = c.get("p_skey") or c.get("skey")
```
即：**借协议端已经登着的 QQ 会话，向 OneBot 客户端要 `user.qzone.qq.com` 的 Cookie**，抽出 `uin / skey / p_skey`，之后拿这三个值去打 QQ空间的私有 CGI。配置项 `cookie_ttl`（默认 600 秒）到点后失效重取。

**本机实测（NapCat 4.18.28，HTTP server `127.0.0.1:3000` 只读探针）**：
- `get_cookies` **action 存在且已注册**（空参探测返回 `Schema compilation error: Expected required property`，即"存在但缺参数"；对照 `definitely_not_real` 返回 `不支持的Api definitely_not_real`）。
- 真调 `{"domain":"user.qzone.qq.com"}` 时返回：`getaddrinfo EAI_AGAIN ssl.ptlogin2.qq.com` —— 说明**这个 action 依赖协议端能访问 QQ 登录域**（`ssl.ptlogin2.qq.com`），当前时刻该域不通。这不影响"action 存在"的判定，但说明取 Cookie 需要协议端有 QQ 网络通路。
- 探针**没有执行任何写操作**、没有发出任何消息、没有改任何配置。

### 1.4 能做什么 / 有没有"随机发点"的定时机制

命令表（上游 README，权限列节选）：查看看说/访客；`看说说`/`评说说`/`赞说说`（ALL）；`发说说`/`写说说`/`删说说`/`看稿`/`过稿`/`拒稿`（ADMIN）；`投稿`/`匿名投稿`/`撤稿`（ALL，表白墙）；`回评`。LLM 工具：`llm_publish_feed` / `llm_view_feed`。

**"随机发点"机制 —— 有，而且是两层随机**（`core/scheduler.py` + `_conf_schema.json`）：
1. **时间随机**：`AutoRandomCronTask` 用 apscheduler `CronTrigger` 算"基准时间"，再 `random.randint(-offset_seconds, +offset_seconds)` 加偏移。
   - `publish_cron` 默认 `30 23 * * *`（每天 23:30 基准），`publish_offset` 默认 **600 秒** → 实际在 **23:20~23:40 之间随机一发**。
   - `comment_cron` 默认 `0 8 * * *` + `comment_offset` 600 秒 → 每天 08:00 前后随机去评论好友的说说。
   - `like_when_comment` 默认 true（评论完顺手点赞）。
2. **概率随机（聊天中触发）**：`trigger.read_prob` 默认 **0.001** —— 用户聊天时按这个概率随机去评说说。上游 hint 原文："推荐设得非常小"。
3. 内容来源：`source.post_max_msg`（默认 500 条群聊记录）喂给 LLM `generate_post()`；`source.ignore_groups` / `ignore_users` 可排除敏感群/人。

### 1.5 能不能搬进我们的适配器

**机制上：能，而且不需要跑 AstrBot。** `core/qzone/` 那一层（`client.py` / `api.py` / `parser.py` / `session.py`）本质是**纯 HTTP 客户端** —— `aiohttp.ClientSession` 打 `user.qzone.qq.com` 的私有 CGI，带 `uin/skey/p_skey`。它只依赖 AstrBot 三处：

| 依赖 | 换成什么 |
|---|---|
| `from astrbot.api import logger` | 换 `logging.getLogger` — 一行事 |
| `config.client.get_cookies(domain=...)` | 自己发一条 OneBot action `get_cookies`（**本机已实测 NapCat 支持**） |
| `PluginConfig` / AstrBot 事件 / AstrBot LLM Provider | 换成 Hermes 侧的配置 + 我们的 `_call_action` + 我们自己的 LLM 调用 |
| `apscheduler` | 框架无关，独立可 pip 装；或直接用 Hermes 的 cron |

**但有两个必须说清的点：**
1. **GPL-3.0 传染**：`Zhalslar/astrbot_plugin_qzone` 的 `LICENSE` 是 GPL-3.0。想「直接拷代码进我们的适配器」就要接受整个适配器变 GPL-3.0。**建议：读协议思路、照 action/接口自己重写，不拷代码**（这也是我们 `adapter.py` 头注释一贯的"读，不搬代码"原则）。若要拷，优先考虑 ★47 的 **MIT** 版 `diaomin66/astrbot_plugin_qzone_ultra`（未细读源码，需再核）。
2. **风控/封号风险（不美化）**：
   - 这是**非官方私有协议**（`emotion_cgi_publish_v6`、`internal_dolike_app` 这类内部 CGI + `skey`），不是腾讯开放的 API。腾讯随时可改可封。
   - 上游自己就踩过坑：PR #22 标题含 **"Add Qzone rate limit handling"** —— 有速率限制/被限流的现实问题。
   - 上游 TODO 里明写 **"点赞说说（接口显示成功，但实测点赞无效）"** —— 接口行为不稳定，返回成功不代表生效。
   - 腾讯客服明文规则（`kf.qq.com/faq/120322fu63YV130422bEv2IF.html`）：QQ空间仅供个人非商业使用，禁止以日志/说说/评论等方式传播推广、骚扰。
   - 第三方渠道描述（非官方来源，仅作旁证）：非官方插件/刷量被检测，"轻则限流致使空间无人可见，重则封禁数日"。
   - **综合**：拿**主号**去跑自动发说说/自动评论，是把主号的 QQ空间（连带 QQ 账号）置于不可控风险下。若真要跑，应该用小号、限频、且明确告知主人这是可撤销的实验。

### 1.6 搜过但没解决的

- **没有找到"框架无关的 QQ空间 Python 库"**（如可直接 pip 装的 qzone SDK）。搜过关键词见 §5。
- `diaomin66/astrbot_plugin_qzone_ultra`（★47 MIT）只取了元数据，**未读源码**，其登录方式（是否也走 `get_cookies`）**未核实**。

---

## 2. 群管理类插件

### 2.1 AstrBot 生态（**全部需要跑 AstrBot**）

| 插件 | 仓库 | ★ | 许可证 | 最后推送 | 能力 |
|---|---|---|---|---|---|
| astrbot_plugin_qqadmin | `Zhalslar/astrbot_plugin_qqadmin` | **179** | **GPL-3.0** | 2026-09-17 | 禁言/解禁/全禁/改名/改头衔/踢人/群拉黑/上下管/撤回(默认10条)/设群头像/群名/设精移精/看群精华/发公告/看公告/投票禁言/自定义违禁词/内置禁词/刷屏禁言/宵禁/进群审核(白词黑词等级)/整理群友。**多命令已注册为 LLM 工具**。权限链 超管>群主>管理员>成员。前端配置面板 + 按群独立配置 |
| astrbot_plugin_group_guardian | `zcj-ui/astrbot_plugin_group_guardian` | **36** | **MIT** | 2026-09-16 | 28 项群管 + AI 智能审核 + 违禁词热更新 + **AstrBot Dashboard WebUI 面板**。正则初筛 + AC 自动机 + 词库学习；刷屏监控（秒/分/小时速率，红黄绿）；群统计/字数统计/成员列表/禁言列表；20 项 LLM 工具（`ban_group_member`/`kick_group_member`/`set_whole_group_ban`/`set_member_card`/`send_group_announcement`…）；违规记录聚合 + CSV 导出；申诉/解禁；名片监控；入群审核 |
| astrbot_plugin_qq_tools | `YUMU1658/astrbot_plugin_qq_tools` | **13** | **MIT** | 2026-06-05 | 引用回复适配器（`[REPLY:msg_id]` 无需工具调用）、撤回、检索最近消息、群管理（名片/禁言/全禁/踢人/公告/精华/专属头衔/黑名单带过期）、头像、Playwright 网页浏览、Gemini 视频分析、定时唤醒。README 自述"**仅在 aiocqhttp（NapCat）下完成测试**，约 90% 功能仅适用于 OneBot" |
| astrbot_plugin_cloudrank | `GEMILUXVII/astrbot_plugin_cloudrank` | **30** | **AGPL-3.0** | 2025-12-30 | 词云 + 活跃度排行（聊天分析、定时/手动） |
| astrbot_plugin_reread | `Zhalslar/astrbot_plugin_reread` | **40** | **MIT** | 2026-09-20 | 复读姬：复读阈值（文本/图片/表情/At）、复读概率、屏蔽单人、**违禁词**、冷却 |
| astrbot_plugin_recall | `Zhalslar/astrbot_plugin_recall` | **20** | **GPL-3.0** | 2026-03-17 | 智能撤回：自动判断各场景消息是否需要撤回 |
| astrbot_plugin_anti_revoke | `Foolllll-J/astrbot_plugin_anti_revoke` | **16** | **AGPL-3.0** | 2026-09-21 | QQ 防撤回 |
| astrbot_plugin_keywords_reply | `Foolllll-J/astrbot_plugin_keywords_reply` | **12** | **AGPL-3.0** | 2026-09-13 | 关键词回复 |
| astrbot_plugin_group_aip_review | `VanillaNahida/astrbot_plugin_group_aip_review` | **10** | 无 | 2026-06-09 | 群聊内容安全审核（百度内容安全），违规警告/撤回/禁言 |
| astrbot_plugin_qq_group_admin | `CyreneLian/astrbot_plugin_qq_group_admin` | 4 | 无 | 2026-09-21 | 注册给大模型调用的禁言/踢人/拉黑/撤回/设精华工具，可配 Bot 管理员/群主/群管理员权限 |
| astrbot_plugin_welcome | `PhiLia011/astrbot_plugin_welcome` | 4 | **MIT** | 2026-09-16 | 入群欢迎（针对 llbot 适配器） |
| astrbot_plugin_qq_group_admin | `yun474/astrbot_plugin_qq_group_admin` | 1 | **MIT** | 2026-09-19 | 禁言/入群审批/进退群通知/分群管理员/LLM 工具 |
| astrbot_plugin_w1ndys_rules | `w1ndys/astrbot_plugin_w1ndys_rules` | 1 | 无 | 2026-09-23 | 群规：关键词回复、违禁词、欢迎语、入群验证（私聊交码不踢人）、邀请树 |
| astrbot_plugin_qun_steward | `Whereis-Alice/astrbot_plugin_qun_steward` | 0 | **GPL-3.0** | 2026-09-13 | 一站式群管 + 群相册 + WebUI + 操作审计与撤销 |
| astrbot_plugin_qqadmin（分支） | `MengNianxiaoyao/astrbot_plugin_qqadmin` | 0 | **GPL-3.0** | 2026-09-22 | 同上 Zhalslar 版的镜像/分支 |
| astrbot_plugin_qqadmin | `orangestranger/astrbot_plugin_qqadmin` | 0 | **GPL-3.0** | 2026-08-21 | 刷屏检测（默认 3 条），支持按群配阈值 |
| astrbot_plugin_group_manager | `zblx0306/astrbot_plugin_group_manager` | 0 | 无 | 2026-08-16 | 违禁词拦截、自动撤回、禁言踢出、定时消息 |
| astrbot_plugin_auto_handler | `crimes2/astrbot_plugin_auto_handler` | 0 | AGPL-3.0 | 2025-07-27 | 好友/群申请审批、邀请者黑名单、互斥成员、最小群人数（**注明"全程使用 AI 生成"**） |
| astrbot_plugin_banned_words | `Suyannny/astrbot_plugin_banned_words` | 0 | 无 | 2026-04-11 | 违禁词增删查 |
| astrbot_plugin_keywords_reply | `MUxiaokalami/astrbot_plugin_reply` | 0 | **MIT** | 2025-11-28 | 自定义关键词回复（文字/图片/正则） |

**为什么它们不能直接搬**：以 ★36 的 `group_guardian` 为例，`main.py` 头部就是
```python
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
@register(PLUGIN_NAME, ...)
class Main(ModerationMixin, ..., Star):
```
它的 `onebot.py` 里 `_get_client()` **三级回退**去 `context.platform_manager` 里翻 AstrBot 的平台实例拿 `client.call_action`。整套是 AstrBot 运行时绑定的。★179 的 `qqadmin` 同理。**必须跑 AstrBot 才能用。**

**能白拿的**：它们调用的 **OneBot action 名**（见 §3），以及业务规则（违禁词表结构、刷屏阈值、权限链语义）。

### 2.2 MaiBot 生态（**全部需要跑 MaiBot**）

来源：MaiBot 官方插件仓库 `Mai-with-u/plugin-repo` 的 `plugin_details.json`（**366 条**插件清单，我按关键词筛出 **59 条**相关）。注意：**这些的 ★ 数我没有逐个取**（GitHub core rate limit 打满，见 §5），只取了 manifest 里的 name/license/repo/version。

| 插件 | 仓库 | 许可证 | 能力 |
|---|---|---|---|
| 群管理助手 | `DeepSeek-V4-Pro/maimai_group_admin` | **GPL-3.0** | QQ 群自动小管理员：识别群主/管理员，广告/刷屏/辱骂自动警告→禁言→踢出；群公告、撤回、设精华、入群审批全支持；**权限决策链可逐条审计**。v2.7.0 |
| 群聊禁言管理（Mute Plugin） | `SengokuCola/MutePlugin` | **MIT** | 智能禁言 + 手动禁言命令。v4.7.0 |
| 群聊静音插件（GroupMuter） | `TAIY2020/group_muter_plugin` | **AGPL-3.0** | 管理员命令或麦麦主动让指定群临时"闭嘴"。v2.6.1 |
| 麦麦闭嘴（Silent Mode） | `khiqwq/silent_mode_plugin` | **AGPL-3.0** | "麦麦闭嘴"→安静一段时间，"麦麦张嘴"解除 |
| 沉默插件 | `A0000Xz/MaiBot-Silence-Plugin` | **GPL-3.0** | 需要时保持沉默（窥屏） |
| 宵禁插件 | `A0000Xz/MaiBot-Curfew-Plugin` | **GPL-3.0** | Bot 是群管理员时自动宵禁 |
| 撤回插件（Recall Manager） | `whitefox20011120/recall_manager_plugin` | **MIT** | 群聊撤回管理 |
| 麦麦撤回 | `cateyemizuki/cateye_maimai_recall` | **MIT** | LLM 工具按 msg_id 撤回；引用一条消息发 `/撤回`；失败给具体原因 |
| 撤回跟随 | `ZhaoCang-QWQ/recall-follow` | **MIT** | 旁路监听 NapCat 撤回通知：别人撤回麦麦回复过的消息→麦麦回复也撤；还没回就中止发送 |
| 撤回消息响应 | `higekibaka/recall_response_plugin` | **MIT** | 检测群撤回事件，按概率让人设化反应 |
| 关键词回复 | `FlandreSatori/maibot_plugin_keywords_reply` | **AGPL-3.0** | 指令触发 + 自动监听、多别名、正则、图文/At/表情/语音/本地视频富媒体回复。v1.2.5 |
| 加群申请处理 | `whitefox20011120/group-request-handler` | **MIT** | 监听加群申请→推管理员资料到指定群→命令一键通过 |
| 群聊申请处理 | `Ling-LA/group-request-handler` | **MIT** | 群邀请审核 + 白名单 + 群列表查询 |
| 麦麦喊新人说话！ | `ji-or-ji/group-probation-plugin` | **MIT** | **入群欢迎** + 考察期（默认 48h 未发言自动移出） |
| 麦麦看到你了！ | `ji-or-ji/group-awareness-plugin` | **MIT** | 感知进群/退群/禁言/改名/管理变动；固定模板 / 人格化 LLM / 注入上下文三档；群荣誉+群员称号 |
| 麦麦不要再看那个了！ | `cateyemizuki/cateye_custom_filter` | **MIT** | 精准过滤指定群/用户（仅黑名单），时间段+星期窗口，按消息类型分别开关 |
| 社区互动增强 | `MCYXG233/community.engagement-plus` | **MIT** | 发言管理、消息优化、氛围监测、安全过滤、隐私保护 |
| 禁言报告(碎碎念) | `A-Dawn/mute_report_murmur` | **MIT** | 监听 NapCat 群禁言通知，Bot 被禁言时生成小报告并阻断后续触发 |
| 群聊投票 | `Heximiao/vote_plugin` | **MIT** | 群聊投票（目前只有投票禁言），自动统计 |
| 群聊定时打卡 | `WaterInk0101/groupsign` | **MIT** | 定时对所有非黑名单群执行打卡 |
| NapCat 影子适配器 | `cateyemizuki/cateye_napcat_shadow_adapter` | **MIT** | **影子中继**：监控 NapCat 适配器易被去重误丢的四类通知（表情回应/群撤回/好友撤回/精华消息），走独立 OneBot WS。**这条对我们特别有参考价值 —— 我们适配器现在正是"丢掉全部 notice"** |
| 群规/知识库 | `bsyj/game-knowledge-plugin` | **GPL-3.0** | 群聊知识自动提取 + AI 预审核 + 图谱 + 检索问答 + WebUI |
| Agent 插件 | `Dreamwxz/oh-mai-agent` | **GPL-3.0** | 后台子代理、定时任务（cron/delay）、MCP 工具集成 |

**为什么不能直接搬**：MaiBot 插件靠 manifest 声明 capabilities（`api.call` / `api.list` / `message.get_by_id` / `person.get_id_by_name` / `send.text` …），运行在 MaiBot 进程里，由 MaiBot 的插件 SDK 派发事件。**必须跑 MaiBot。**

**一个直接相关的事实**：`Mai-with-u/nonebot-plugin-maibot-adapters`（★15）和 `advent259141/astrbot_plugin_maibot`（★10）的存在说明 —— **MaiBot 自带 OneBot 适配器，且社区在把 MaiBot 往 AstrBot 里塞当"agent 执行器"。** 也就是说"能力在协议端，壳可以换"这件事，社区已经在验证了。

### 2.3 `/tmp/qq-bridge` —— **没有群管理能力**

`/tmp/qq-bridge` 是 `Derpyu520/qq-bridge`：QQ ↔ DeepSeek Harness 桥接（Node，SnowLuma OneBot v11 WS → DSH Web API）。它的 `plugins/` 目录下**只有一个 `qq-mode-console`**（控制台切换运行模式 chat / closed-agent / reserved / reserved2），`lib/index.js` + `package.json`。**没有任何群管理插件。**

其有价值的参考点（不是插件、是机制设计）：
- MCP 工具暴露的是 QQ 动作**安全子集**（查状态/查群/查消息/发消息），**发送强制白名单**。
- `src/mcp-snowluma-safe.js` 里发送工具支持可选 `replyToMessageId`，另有专用 `qq_reply` 工具。
- `src/sensitive.js` / `src/slang-learner.js` / `src/sticker-lib.js` —— 敏感词、网络用语学习、表情库，**这些是"文本层"逻辑，语言无关、框架无关，可以照搬思路。**
- `reserved` 模式：观望/活跃/试探/退场状态机、按空格分句、`[SILENT]` 潜水。**我们的 `group_wake.py` 已经有 `[SILENT]` 和门控，方向一致。**

---

## 3. 协议端实测：群管理 action 到底有没有（本机 NapCat 4.18.28）

方法：走 HTTP server `POST http://127.0.0.1:3000/<action>`，头 `Authorization: Bearer <token>`。**全部用空参 `{}` 探测** —— 缺必填参数时协议端返回 `Schema compilation error: Expected required property`（= action 存在且**没有执行任何写操作**）；不存在时返回 `不支持的Api <名字>`。已用 `definitely_not_real` 和 `get_group_list` 校准两种文案。**没有执行任何会改状态的操作。**

| 结果 | action |
|---|---|
| ✅ **存在** | `set_group_ban`、`set_group_whole_ban`、`set_group_kick`、`set_group_card`、`set_group_admin`、`set_group_name`、`set_group_leave`、`set_group_special_title`、`set_group_portrait`、`set_group_remark`、`set_group_todo`、`set_group_add_request`、`set_friend_add_request`、`delete_msg`、`set_essence_msg`、`delete_essence_msg`、`get_essence_msg_list`、`get_group_system_msg`、`get_group_honor_info`、`get_group_member_info`、`get_group_member_list`、`get_group_info`、`get_group_detail_info`、`get_group_at_all_remain`、`get_group_list`、`get_msg`、`get_friend_list`、`_send_group_notice`、`_get_group_notice`、`upload_group_file`、`send_like`、`send_group_sign`、`mark_msg_as_read`、`set_msg_emoji_like`、**`get_cookies`** |
| ❌ **不存在**（名字/替代） | `set_group_title` → 用 **`set_group_special_title`**；`send_group_notice` → 用 **`_send_group_notice`**；`cancel_group_essence` → 用 **`delete_essence_msg`**；`get_group_file_list` → 无 |

> 注：项目里已有的能力探测记录 `/opt/data/skills/devops/hermes-container-ops/references/onebot-protocol-capability-probe.md` 里另有一条硬结论：**QQ空间一族在 OneBot 协议端压根没有接口**（`get_qzone_feed` / `get_qzone_msg` / `publish_qzone` / `upload_qzone_img` 实测全不存在）。这与 §1.3 一致 —— **qzone 只能靠 `get_cookies` + 自己打 HTTP**，协议端帮不了更多。

**含义**：群管的每一个"动作"协议端都给了。我们缺的不是能力，是**适配器里的那层壳**（一个 `_call_action` 封装 + 权限判定 + 命令/工具入口）。我们已有的 `_call_action()`（`adapter.py:957`）已经能发任意 action 帧，**底子是通的**。

---

## 4. 我们适配器里已经有的（读源码确认，别重复推荐）

路径：`/opt/data/profiles/chat/plugins/onebot/`（共 10 个模块 2999 行）

| 已有能力 | 落在哪 | 备注 |
|---|---|---|
| **出站分段发送** | `segmentation.py`（171 行）+ `adapter.py send()` | `⁂` 分隔 → 拆多条，段间隔 1.5~3.5s；容错 `※`/`⸮`；`scrub()` 硬兜底保证符号永不出现在正文 |
| **入站防抖** | `debounce.py`（185 行） | 10s 重置式窗口 / 45s 硬上限 / 默认仅私聊 / 群聊按会话+发送者分桶 |
| **群聊滚动窗口** | `group_window.py`（226 行） | 每群一份，有 msgs/bytes 上限，落 profile 数据目录，**绝不入主库** |
| **唤醒门控（三态）** | `group_wake.py`（447 行） | `collect-only`（默认，0 LLM）/ `mention-only`（@ 必答）/ `full`（未验收）；含 `[SILENT]` 沉默标记 |
| **群门控阈值** | `adapter.py` `group_gate_threshold` | 免费前置闸，0.30~0.45 建议 |
| **唤醒限流** | `group_wake.py WakeLimiter` | 每分钟 2 / 每小时 30 |
| **主人/生人信任档** | `trust.py`（128 行） | `is_owner(uid)` 与 `classify(uid, is_group=)` 两轴分离；群里一律 stranger |
| **心跳 / 自检** | `health.py`（216 行）+ `doctor.py`（340 行） | `health_state` / `health_findings` / `write_state_file` / 看门狗 + 告警 |
| **记忆隔离硬前置** | `adapter.py` `group_memory_guard_required` | 只在 `memory.provider` 指向 `hindsight_guard` 且插件在位时才唤醒 |
| **入站引用解析** | `onebot_proto.reply_message_id()` | 取 `reply` 段的 id，用于"被回复她→唤醒" |
| **通用 action 调用** | `adapter.py:_call_action()` | 发任意 action 帧等 echo。**群管的底子已经在了** |

### 4.1 我们发现的两个真实缺口（做群管前必须先补）

1. **入站只处理 `post_type == "message"`，notice / request 全丢**
   `onebot_proto.should_ignore()` 第一句就是：
   ```python
   if str(event.get("post_type") or "") != "message":
       return "not_a_message_event"
   ```
   而 `adapter.py:_ingest()` 一进门就调它。后果：**入群事件（`group_increase`）、禁言通知（`group_ban`）、撤回通知（`friend_recall`/`group_recall`）、加群/加好友申请（`request`）全部进不来。**
   → **做「入群欢迎」「撤回跟随」「入群审批」「防撤回」的第一步，不是写插件，是先改这个函数 + `_ingest` 的 post_type 分流。** 参考实现思路：MaiBot 的 `cateyemizuki/cateye_napcat_shadow_adapter`（专门补 NapCat 被丢的通知）。

2. **出站只能发纯文本；`reply_to` 形参存在但没被使用**
   `onebot_proto.build_action()` 把 `message` 硬编码成单个 text 段：
   ```python
   params = {"message": [{"type": "text", "data": {"text": text}}]}
   ```
   `adapter.send(chat_id, content, reply_to=None, ...)` 接受 `reply_to`，但函数体里**从头到尾没用到它** —— **引用回复出站没实现**（入站引用解析是有的，方向不对称）。要发图/表情/引用，得先扩 `build_action`。

### 4.2 缺口清单（我们完全没有的）

群管工具集（禁言/解禁/全禁/踢/拉黑/改名片/头衔/设管理/精华/公告/群名/群头像）、入群欢迎、入群审批、违禁词拦截、刷屏检测、AI 内容审核、关键词回复、宵禁、词云/发言统计、投票禁言。

---

## 5. 搜过的关键词 / 查不到的 / 本报告的不确定性

### 5.1 GitHub Search API 搜过的关键词（可复现）
`qzone astrbot` · `astrbot 群管理` · `maibot plugin` · `maimai bot plugin` · `maiBot 群管理` · `maimbot plugin` · `astrbot 违禁词` · `astrbot 词云` · `astrbot 入群欢迎` · `astrbot 关键词回复` · `astrbot 撤回` · `nonebot 群管理` · `onebot 群管理`

### 5.2 查不到 / 没做的

- ❌ **没有找到任何"框架无关、可直接装进 Hermes onebot 适配器"的现成群管理或 qzone 插件。** 找到的全部绑定 AstrBot 或 MaiBot 运行时。
- ⚠️ **MaiBot 那 59 个插件的 ★ 数、最后推送时间我没有逐个取。** 原因：GitHub API 匿名限额 60 次/小时已用尽（`X-RateLimit-Remaining: 0`，reset 在 epoch `1790180118` ≈ 2026-09-23 16:15 前后）。表里的名/仓库/许可证/版本来自 MaiBot 官方插件仓库 `plugin_details.json` 的 manifest，**star 数一栏我不填，不编。**
- ⚠️ `diaomin66/astrbot_plugin_qzone_ultra`（★47 MIT）**只取了元数据，没读源码**，登录方式未核实。
- ⚠️ AstrBot 插件市场（官方索引站）没有直接抓取，用的是 GitHub 全站搜索，**可能漏掉不在 GitHub 上的插件。**
- ⚠️ **AstrBot 侧的插件源码没有本地读取**：`/opt/data/stack/astrbot/data/plugins/` 是 `root:root 0700`，`hermes` 用户读不到。qzone 的代码是通过 GitHub raw / Contents API 读的上游版本（**不能 100% 保证与本地那份同版本**，只是工具名对得上）。其余插件源码**没有全读**，只抽读了 `group_guardian` 的 `main.py` / `onebot.py` 头部来判定耦合度。
- ❌ **没做**：没有实际安装任何插件、没有改动现役配置、没有重启任何服务、没有碰 docker compose、没有发出任何真实消息。

### 5.3 证据来源清单
- 本地源码：`/opt/data/profiles/chat/plugins/onebot/*`（全读）
- 本地残留：`/opt/data/stack/astrbot/data/temp/astrbot_plugin_qzone/`（空）、`data/_apply_boundary.py:57`（工具白名单）
- 本机协议端实测：NapCat 4.18.28 HTTP server `127.0.0.1:3000`（空参只读探测，见 §3）
- 上游代码：`Zhalslar/astrbot_plugin_qzone` 的 `core/qzone/{session,client,api}.py`、`core/scheduler.py`、`core/llm_action.py`、`core/sender.py`、`_conf_schema.json`、`LICENSE`（GitHub Contents API / raw，实读）
- 上游 README：`Zhalslar/astrbot_plugin_qqadmin`、`zcj-ui/astrbot_plugin_group_guardian`、`YUMU1658/astrbot_plugin_qq_tools`
- MaiBot 官方插件索引：`Mai-with-u/plugin-repo` → `plugin_details.json`（366 条）
- 项目内已有记录：`/opt/data/skills/devops/hermes-container-ops/references/onebot-protocol-capability-probe.md`

---

## 6. 给主人的三句话建议（不替他决定）

1. **QQ空间**：上游 ★161 的 `Zhalslar/astrbot_plugin_qzone` 是 GPL-3.0（README 徽章写 MIT 是错的），且是**非官方私有协议**，有真实限流/封号风险（上游自己就发过 rate limit 的 PR，点赞还被自己标"实测无效"）。它**不需要跑 AstrBot** —— `core/qzone/*` 是纯 aiohttp 客户端，只要我们自己补一条 `get_cookies` action 调用（**本机 NapCat 已实测支持**）就能打通。**建议：照接口自己重写，别拷 GPL 代码；且优先小号 + 限频。**
2. **群管理**：AstrBot 侧现成货很多（`qqadmin` ★179 GPL-3.0 / `group_guardian` ★36 MIT 是最强的两个），MaiBot 侧有官方插件仓库 366 条可选。**但它们全都得跑各自的框架** —— 与我们"不打算为一个插件重起一整个 AstrBot"的前提冲突。**好消息是协议端该有的 action 全都有（本机已逐条实测），缺的只是我们适配器里的壳。**
3. **真要动手，顺序应该是**：① 先补 `notice`/`request` 事件分流（现在被整条丢，是入群欢迎/防撤回/入群审批的硬前置）→ ② 扩 `build_action` 支持引用/图片段（现在 `reply_to` 形参是摆设）→ ③ 在 `_call_action` 上包一层带权限判定的群管工具（可复用 `trust.py` 的主人判据）→ ④ 最后才是具体功能（欢迎语/违禁词/关键词回复这类纯文本逻辑，跟框架耦合最低，最好自己写）。
