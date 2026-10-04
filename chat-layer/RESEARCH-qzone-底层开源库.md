# 调研：QQ空间「自动发说说」bot 插件底下到底靠什么 + 有没有能脱离 AstrBot 独立用的开源库

调研日期：2026-09-23 · 调研者：Hermes 子代理（**只读调研**：未改任何文件、未重启服务、未发任何消息、未执行任何写操作）
取数方式：容器内 `curl` 直连 **api.github.com 匿名额度已耗尽**（返回 `API rate limit exceeded for 106.92.48.235`）；
`raw.githubusercontent.com` / `github.com` 在容器内 **curl 返回 000（不通）**。故所有 GitHub 元数据改用
**Hermes 服务端的 `web_extract`**（走 `https://api.github.com/repos/...`、`raw.githubusercontent.com/...`、`github.com/...` 页面）取得；
PyPI / npm 元数据用容器内 `curl` 直连（这两个域在容器内**可通**，实取成功）。
凡未取到的数据点在 §5 明确列出，**无任何编造**。

---

## 0. 结论（先给答案）

**结论 A —— AstrBot 那两个 QQ空间插件，底下没有「某个 QQ空间开源库」**

两个插件都是**自己用 HTTP 客户端裸打 QQ空间私有 CGI**，不是引了某个开源 SDK：

| 插件 | requirements 实测 | 源码里的 HTTP 层 |
|---|---|---|
| `Zhalslar/astrbot_plugin_qzone` | `pillowmd` / `json5` / `apilmoji[tqdm]` —— **零个 qzone 相关依赖** | `core/qzone/client.py` 里 `aiohttp.ClientSession` 直打 `user.qzone.qq.com` / `h5.qzone.qq.com` / `up.qzone.qq.com` 的 CGI |
| `diaomin66/astrbot_plugin_qzone_ultra` | `httpx` / `aiohttp` / `Pillow` / `imageio-ffmpeg` / `pillowmd` / `json5` —— **同样零个 qzone 库** | 自建本地 daemon（127.0.0.1:18999）隔离请求 + Cookie 管理，直打 `emotion_cgi_publish_v6` / `emotion_cgi_update` 等 |

登录也不靠任何库：都是**向协议端（OneBot 实现）要 Cookie**——`core/qzone/session.py` 实测代码：

```python
payload = await self.cfg.client.get_cookies(domain="user.qzone.qq.com")
c = {k: v.value for k, v in SimpleCookie(cookies_str).items()}
uin = c.get("uin"); skey = c.get("skey"); p_skey = c.get("p_skey") or c.get("skey")
```

拿 `uin / skey / p_skey` 算出 `g_tk`，再打 CGI。**没有任何第三方库参与。**

**结论 B —— 主人记忆里「那个插件基于某些开源项目」，最可能指这两个（都在插件 README 鸣谢里）**

`Zhalslar/astrbot_plugin_qzone` README 最后一节「鸣谢」原文列了三个来源：

1. **`idoknow/CampuxBot`** —— 原文：「部分代码参考了 CampuxBot 项目，由作者之一的 Soulter 推荐」。
   CampuxBot 是 **`idoknow/Campux`（QQ 校园墙自动化平台，★167，Apache-2.0，今天 2026-09-23 还在提交）** 的机器人端。
   → 这是「**表白墙/投稿审核**」那半套功能与代码的出处，**不是发说说协议层的出处**。
2. **一篇博客《QQ 空间爬虫之爬取说说》**（kylingit.com）——「感谢这篇博客提供的思路」。
3. **`wwwpf/QzoneExporter`（★637，GPL-3.0）** —— 「一个 QQ 空间爬虫项目」。

也就是说：**「基于某些开源项目」这个印象是真的，但指的是"表白墙/爬虫思路"，不是"某 QQ空间 SDK"**。
发说说那层的 CGI 打法（`emotion_cgi_publish_v6`、`internal_dolike_app`、`unikey = https://user.qzone.qq.com/{uin}/mood/{tid}`）
是这一整族项目（QzoneExporter → GetQzonehistory → Campux → AstrBot 插件）**共用的野生协议常识**，并不来自某个被依赖的库。

**结论 C —— 能脱离 AstrBot 独立用的开源库：有，但都很小、都很新、没有一个是"成熟官方 SDK"**

- **Python 侧唯一 `pip install` 即用、且自带三种登录方式的**：**`Eganchiyu/qzone-sdk`**（PyPI `qzone-sdk` 1.0.1，MIT，**★1**，2026-06-23 建仓 / 2026-08-05 最后推送）。
  它自己 README 明确宣称：`扫码 / NapCat / 手动 Cookie 三选一`、**零第三方依赖**、自带 CLI。
  它的 README 里还有一张横向对比表（对比 `qzone-api` / `aioqzone` / 自己），是本报告最重要的「同族谱系」线索。
- **最活跃 + 星数最高的 Python 库**：**`aioqzone/aioqzone`**（PyPI `aioqzone` 1.9.9.dev1，**AGPL-3.0**，**★53**，2022-01 建仓，**最后推送 2026-09-23 今天**）。
  自述 "A python wrapper for Qzone web login and Qzone http api"，偏**网页登录 + 读**（还带 `pychaosvm` 解腾讯 ChaosVM 验证码）。
- **Go 侧有一个干净的独立库**：**`guohuiyuan/qzone-go`**（**★3**，**AGPL-3.0**，2026-02-10 建仓，最后推送 2026-02-12，**之后停更**）。
  「纯标准库，零依赖」，支持**扫码登录或直接传 cookie**，接口覆盖面是本次见到最全的（发/删/查说说、点赞评论回复、访客、日志、相册、视频、留言板、私密日记…）。
  `guohuiyuan/qzonewall-go`（★12，表白墙服务）就是**基于它**写的 → **这是本次唯一一个「有下游项目真实依赖」的独立 QQ空间库**。
- **TS/JS 侧**：**`Procyon-Nan/qzone-sdk`**（npm `qzone-sdk` 0.3.3，MIT，**★0**，最后发布 2026-08-21），自述 "A framework-agnostic TypeScript SDK for Qzone"。

**一句话给主人**：插件不是"基于某个 SDK"，它是**自己照着一族爬虫项目的野生 CGI 协议写的**；
真要"脱离 AstrBot 独立用"，Python 走 `Eganchiyu/qzone-sdk`（MIT、pip 即装，但 ★1 很年轻），
Go 走 `guohuiyuan/qzone-go`（AGPL-3.0、有下游依赖，但停更），要活跃就 `aioqzone`（AGPL-3.0 传染、偏读）。

---

## 1. 底层协议长什么样（读懂了这个，所有库和插件就都能对上号）

- 域名：`user.qzone.qq.com` / `h5.qzone.qq.com`（前端页）、`up.qzone.qq.com`（图片上传）。
- 鉴权三件套：Cookie 里的 `uin`（去 `o` 前缀）、`skey`、`p_skey`；由 `p_skey` 换算 **`g_tk`（bkn）**（`hash = 5381; hash += (hash<<5) + c`，32 位 …）随每个请求带上。
- 关键 CGI（`Zhalslar` 插件 `core/qzone/api.py` 实测常量，逐条核对过）：
  | 常量 | URL | 用途 |
  |---|---|---|
  | `EMOTION_URL` | `.../taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6` | **发说说** |
  | `DELETE_URL` | `.../taotao.qzone.qq.com/cgi-bin/emotion_cgi_delete_v6` | 删说说 |
  | `DOLIKE_URL` | `.../w.qzone.qq.com/cgi-bin/likes/internal_dolike_app` | 点赞（`appid=311` 表说说） |
  | `COMMENT_URL` / `REPLY_URL` | `.../taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds` | 评论 / 回复 |
  | `LIST_URL` | `.../taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6` | 说说列表 |
  | `DETAIL_URL` | `.../taotao.qq.com/cgi-bin/emotion_cgi_msgdetail_v6` | 说说详情 |
  | `UPLOAD_IMAGE_URL` | `up.qzone.qq.com/cgi-bin/upload/cgi_upload_image` | 图片上传（源码自注「本接口较为脆弱」） |
  | `ZONE_LIST_URL` | `.../ic2.qzone.qq.com/cgi-bin/feeds/feeds3_html_more` | 好友动态流 |
- 发布体（`publish()` 实测字段）：`con`（正文）、`hostuin`、`ugc_right=1`（公开）、`qzreferrer`、图片走 `pic_bo` + `richtype=1` + `richval`（Tab 分隔）。
- **`GetQzonehistory` 那条「历史说说」链路**用的是另一个接口：`feeds2_html_pav_all`（互动消息列表，返回 HTML，用 BeautifulSoup 解析）+ `emotion_cgi_msglist_v6`（未删除说说）双通道。
- `g_tk` 的取法在各家实现里也一致：`ChainlessChain` 的 `qzone-collect.js` 注释亦为「bkn hash over qzone 域 p_skey」（旁证，非本次核心）。

→ 综上：**这是一族稳定的野生协议**，所以每个项目都自己抄一遍，谁也不依赖谁。

---

## 2. 独立可用的 QQ空间 / QQ 协议库横评

★ 与推送时间均为 2026-09-23 实取（GitHub API 经 Hermes 服务端 `web_extract`，HTML 页面兜底）。

| 库 | 仓库 / 分发 | 语言 | ★ | 许可证 | 登录方式 | 维护状态 | 能否脱离 AstrBot 独立用 |
|---|---|---|---|---|---|---|---|
| **qzone-sdk** | `Eganchiyu/qzone-sdk` · PyPI `qzone-sdk` 1.0.1 | Python | **1** | **MIT** | **扫码 / NapCat(WebSocket) / 手动 Cookie 三选一** | 2026-06-23 建仓，末次推送 **2026-08-05**，5 commits | ✅ **能**（`pip install qzone-sdk`，零依赖；仅 NapCat 模式需 `[napcat]` 额外依赖） |
| **aioqzone** | `aioqzone/aioqzone` · PyPI `aioqzone` 1.9.9.dev1 | Python | **53** | **AGPL-3.0** | 网页登录（二维码 / 密码+验证码，自研 `pychaosvm` 解 ChaosVM） | 2022-01-28 建仓，末次推送 **2026-09-23（今天）**，12 forks，默认分支 `beta` | ✅ 能，但 AGPL-3.0 传染；功能偏**读 + H5 接口** |
| **qzone-go** | `guohuiyuan/qzone-go` | Go | **3** | **AGPL-3.0** | **扫码登录 或 直接传 Cookie** | 2026-02-10 建仓，末次推送 **2026-02-12（此后停更）** | ✅ 能（`go get`，纯标准库零依赖）；**有下游真实依赖：qzonewall-go** |
| **qzone-sdk (npm)** | `Procyon-Nan/qzone-sdk` · npm `qzone-sdk` 0.3.3 | TypeScript | **0** | **MIT** | README 未细读，npm 自述 "framework-agnostic" | 2026-08-12 建仓，末次发布 **2026-08-21** | ⚠️ 原则上能，**未读源码核实登录方式** |
| **Qzone-API** | `SmartHypercube/Qzone-API` | Python | **167** | **MIT** | 传 Cookie（含从 curl 命令抄 cookie 的小工具） | 2016-11-17 建仓，仓库页显示末次更新 **2022-05-17** | ✅ 能，但**极老、只读**（解析说说详情，不含发布） |
| **GetQzonehistory（原作）** | `LibraHp/GetQzonehistory` | Python | **7.7k** | （归档不改动） | 扫码登录 | ⛔ **2026-09-04 主动归档**，自述「基于平台规则与合规考虑停止维护，不建议继续安装、运行、分发」 | ✅ 能跑，**但官方已劝退**；`ll0v0ll` fork（**★383，GPL-3.0，末次推送 2026-09-22，活跃**）仍在维护 |
| **QzoneExporter** | `wwwpf/QzoneExporter` | Python | **637** | **GPL-3.0** | **浏览器手抄 Cookie + `g_tk`** | 2018 建仓，仓库页显示末次更新 **2022-06-17** | ✅ 能，只读导出 |
| **Campux** | `idoknow/Campux`（`RockChinQ/Campux` 已改名） | Go（Bun/React/Fastify/Prisma） | **167** | **Apache-2.0** | OneBot v11 WebSocket + Cookie 存储（文档：「QZone 发布依赖有效 cookies，建议配置 cookies 健康检查和可人工介入的扫码登录」） | **活跃**：末次提交 **2026-09-23（5 小时前）**，716 commits | ❌ 不是库，是**整套校园墙平台**；但**不依赖 AstrBot** |
| **CampuxBot** | `idoknow/CampuxBot` | 机器人端 | **10** | 未取 | 同 Campux | 未取末次提交 | ❌ 只是 Campux 的旧机器人端 |
| **QzoneWall-Go** | `guohuiyuan/qzonewall-go` | Go | **12** | **无 LICENSE 文件**（API `license: null`） | 启动异步 `GetCookies`（优先）→ 失败回退**扫码登录**；`GetUserInfo` 校验 | 2026-02-11 建仓，末次推送 **2026-03-14** | ❌ 是服务不是库；但**不依赖 AstrBot**，且自带 NapCat docker-compose |
| **koishi-plugin-qzone** | `lumia1998/koishi-plugin-qzone` · npm 0.0.16 | TS | 未取 | **GPL-3.0-only** | 「支持 OneBot 自动刷新与二维码登录」 | 末次发布 **2026-09-18** | ❌ 是 Koishi 插件，自述「**测试中，不推荐下载**」 |

**几个关键对比结论：**

1. **登录方式只有三大流派**：(a) 手抄/扫码拿 Cookie（GetQzonehistory、QzoneExporter、aioqzone、qzone-go 扫码）；(b) **向 OneBot 协议端要 Cookie**（AstrBot 两插件、qzonewall-go、Eganchiyu/qzone-sdk 的 NapCat 模式）；(c) 自建网页登录（aioqzone 的 ChaosVM 路线）。
   **`Eganchiyu/qzone-sdk` 是唯一把 (a)(b) 两条都做成可切换 provider 的独立库** —— 这正是「脱离 AstrBot 但复用 AstrBot 已登好的 QQ 会话」最省事的路。
2. `Eganchiyu/qzone-sdk` 的 README 对比表把同族三个库的优劣写得很直白（原文）：`qzone-api` 是 MIT/依赖 aiohttp/二维码登录需自管 Cookie/**不支持图片说说**；`aioqzone` 是 **AGPL-3.0 传染**/依赖 pydantic 等/二维码或密码+验证码/支持图片说说；自己则是 MIT/零依赖/三种认证/带 CLI。**该对比表出自第三方 README，未被我独立验证。**
3. **没有任何一个库是「腾讯官方 API」**：全是非官方私有协议。腾讯开放的是 QQ 互联（分享到 QQ空间算 SDK 能力），**不等于**能读写自己的说说。

---

## 3. 「协议端」那一半：`get_cookies` 是谁提供的

- 它是 **OneBot 实现（协议端）扩展出来的 action，不是 OneBot v11 标准**。
- 本机实测（前一份调研 `RESEARCH-可复用插件-qzone与群管理.md` §1.3，NapCat 4.18.28，HTTP `127.0.0.1:3000` 只读探针）：`get_cookies` **存在且已注册**（空参 → `Schema compilation error: Expected required property`）；真带 `{"domain":"user.qzone.qq.com"}` 调用返回 `getaddrinfo EAI_AGAIN ssl.ptlogin2.qq.com` —— 即 **该 action 依赖协议端能访问腾讯登录域**。
- 旁证（`diaomin66` ultra README）：自动绑定「不限定某一个实现；LLOneBot、LLBot、NapCat、Shamrock 等能接入 OneBot v11 反向 WebSocket 的实现都走同一套解析和兜底逻辑」，取不到 Cookie 时可 `/qzone bind <cookie>` 手填。
- ⚠️ **未核实**：NapCat / LLOneBot / Lagrange / go-cqhttp 各自对 `get_cookies` 的**官方文档与支持矩阵**（本轮关键词搜索未命中官方文档页，只有本机实测 + 该 README 旁证）。**go-cqhttp 早已停更**，其 `get_cookies` 支持情况**本次没查**。

---

## 4. 风险（如实说，不美化）

**4.1 这是非官方私有协议，风控与封禁风险真实存在**

- **最强的公开信号**：星数最高的同类项目 `LibraHp/GetQzonehistory`（**★7.7k**）于 **2026-09-04 主动归档**，公告原文：「**基于平台规则与合规考虑，本项目现已停止维护并正式归档** …… 不再提供功能更新、安全修复或兼容性适配 …… **不建议继续安装、运行、分发或基于本项目开展新的开发工作**」，并建议曾运行过的用户「**停止运行相关程序及自动化任务**」、清理登录信息与个人数据。**注意它只是"读"，且只是导出自己的历史说说，都选择退场。**
- 腾讯客服明文规则页（`kf.qq.com/faq/120322fu63YV130422bEv2IF.html`）：「QQ空间仅**供用户个人非商业使用**」，明确禁止「以发送邮件、连锁邮件、短信、即时消息、**日志、个人说说**、礼物等任何方式或利用……传播推广、骚扰」；配套页 `kf.qq.com/faq/130926meqeyA140730F322qQ.html` 说明「QQ空间发布不良信息**被举报核实后**，我司会对空间进行**封闭或切断传播**等控制」。
- **上游插件自己踩到的坑（比第三方说法硬得多）**：
  - `Zhalslar/astrbot_plugin_qzone` PR #22 标题含「**Add Qzone rate limit handling**」→ 存在**现实中的限流**问题。
  - 同仓库 README 的 TODO 里明写：「点赞说说（**接口显示成功，但实测点赞无效**）」→ 接口**返回成功 ≠ 生效**，行为不稳。
  - `diaomin66` ultra README 也自述点赞回读会「**校验不确定**」、「通常是 QQ 空间**读回延迟**」，且视频发布要「先创建、再调权限更新接口做**公开修复**、最后 feed/detail 校验」才敢报成功 —— 说明**发布链路本身经常处于"看似成功实则可疑"的状态**。
  - 源码里 `client.py` 对 401 / `code=登录失效` / 图片过期做了**最多 2 次重登重试**，即「登录态会被踢」是**常态而非异常**。
- ⚠️ **未找到**：**没有找到任何一条公开的、可归属到这类 bot（AstrBot qzone 插件 / Campux / qzone-go）的「因自动发说说被封号」的 issue 或案例报告**。上面那条 `issues#55「对于Bot在QQ动态骂人」`（2026-06-27，已关闭）是**内容风险**（LLM 生成内容在空间骂人），不是封号案例。
  因此**封号在多大程度上发生、触发阈值是多少，本次查不到实证**；能确认的只有：**限流存在、登录态易失效、平台规则明文禁止、最老牌的同类项目已因合规退场。**
- **综合判断（不改口）**：拿**主号**跑自动发说说/自动评论/自动点赞，等于把 QQ空间（及连带 QQ 号）置于**不可控**的处置风险下；且这条链路上游本身"接口成功但无效"的情况已写在作者自己的 TODO 里。若主人坚持要试，应当：**用小号、限频、只读优先、明确作为可撤销实验，并预先告诉主人风险由主人承担。**

**4.2 许可证风险（上次已提，复核后仍然成立）**

| 组件 | LICENSE 文件实测 | 矛盾/影响 |
|---|---|---|
| `Zhalslar/astrbot_plugin_qzone` | **GPL-3.0** | README 徽章写 MIT，**是错的**；拷它的代码会 GPL-3.0 传染我们的适配器 |
| `guohuiyuan/qzone-go` | **AGPL-3.0-only** | AGPL 比 GPL 更狠：**网络服务部署也算分发**，传染面更广 |
| `aioqzone/aioqzone` | **AGPL-3.0** | 同上 |
| `Eganchiyu/qzone-sdk` / `Procyon-Nan/qzone-sdk` / `SmartHypercube/Qzone-API` | **MIT** | 唯一的宽松选项（但 ★1 / ★0 / 2022 年停更） |
| `guohuiyuan/qzonewall-go` | **无 LICENSE**（API `license: null`） | **法律上默认保留全部权利**，不能当开源用 |
| `idoknow/Campux` | **Apache-2.0** | 宽松，且是这次唯一"活跃 + 宽松"的大项目（但它不是库） |

---

## 5. 搜过但没拿到 / 查不到的（绝不编造）

**5.1 明确的数据缺口**

1. **GitHub 匿名 API 额度在容器内已耗尽**（`rate limit exceeded for 106.92.48.235`），且容器内 `curl` 到 `raw.githubusercontent.com` / `github.com` **返回 000（不通）**。本报告的 GitHub 元数据**全部**经由 Hermes 服务端 `web_extract` 代取。若下游要复核，同法可得。
2. **`koishi-plugin-qzone`（lumia1998）的 ★ / 末次提交未取**（只取了 npm 元数据：GPL-3.0-only，末次发布 2026-09-18，自述"测试中，不推荐下载"）。
3. **`idoknow/CampuxBot` 的许可证 / 末次提交未取**（只取了 ★10 与 README 首屏）。
4. **各库的"能不能发说说"没有逐个跑通验证**：`qzone-sdk`(Eganchiyu)、`aioqzone`、`qzone-go`、`qzone-sdk`(npm) 的**登录方式与接口覆盖，只有 README 自述 + 少量源码结构**（Eganchiyu 那个我看到了 `auth/` 目录与三种 provider 类名；其余只读 README）。**未做任何实跑、未做任何 QR 登录、未连任何 QQ 账号。**
5. **协议端 `get_cookies` 支持矩阵**：NapCat / LLOneBot / Lagrange / Shamrock / go-cqhttp 各自**是否有该 action、参数是否一致**，权威文档页**未取到**；只有本机 NapCat 4.18.28 的只读实测（存在）与 ultra README 的旁证。
6. **封号实例**：见 §4.1，**没有找到**可归属的公开封号案例。

**5.2 本轮实际用过的关键词（如实列出，含无果的）**

- 直取元数据：`api.github.com/repos/{guohuiyuan/qzone-go, Eganchiyu/qzone-sdk, Procyon-Nan/qzone-sdk, aioqzone/aioqzone, guohuiyuan/qzonewall-go, ll0v0ll/GetQzonehistory}`、`api.github.com/repos/Zhalslar/astrbot_plugin_qzone/issues?state=all`
- 直取源码：`raw.githubusercontent.com/Zhalslar/astrbot_plugin_qzone/{main.py, requirements.txt, core/qzone/session.py, core/qzone/client.py, core/qzone/api.py}`、`.../diaomin66/astrbot_plugin_qzone_ultra/{requirements.txt, README.md}`
- 页面勘查：`github.com/topics/qzone`（44 个仓库全列表）、`github.com/search?q=qzone+sdk&type=repositories`、`github.com/{idoknow/Campux, idoknow/CampuxBot, wwwpf/QzoneExporter, LibraHp/GetQzonehistory, guohuiyuan/qzone-go/commits/main}`
- 包仓库直查：`pypi.org/pypi/{aioqzone, qzone-sdk, pyqzone, qzone}/json`、`registry.npmjs.org/-/v1/search?text={qzone, qq qzone}`、`registry.npmjs.org/{qzone-sdk, koishi-plugin-qzone}`
- 搜索关键词：`QQ空间 说说 开源 python SDK 库 自动发说说 qzone api`、`pyqzone github QQ空间 python API 库`、`GetQzonehistory github 扫码登录 导出说说 开源`、`napcat QQ空间 插件 开源 自动发说说 github astrbot`、`guohuiyuan qzone-go golang QQ空间 api 库 github`、`"qzone" python 库 pip install sdk 说说 发布 pypi`、`QQ空间 自动发说说 封号 风控 限流 反馈 issue 非官方接口`、`QQ空间 说说 发布接口 风控 封禁 issue ... rkey`、`NapCat get_cookies action domain 文档 取 cookie`
- **无果的**：`guohuiyuan qzone-go golang ...`（搜索后端返回一堆 Debian 镜像页，无用；最终靠 web_extract 直取仓库页）；`NapCat get_cookies ... 文档`（返回 selenium 取 cookie 的无关文章，**官方文档页没找到**）；`pyqzone`（PyPI 上确有一个 `pyqzone 0.0.0.1`，许可证字段是一整段 AGPL 文本、**无仓库地址、无描述，疑似占位包，未采信**）。

---

## 6. 若后续要落地（仅建议，本次未执行任何动作）

1. **最省事路线**：`pip install qzone-sdk`（Eganchiyu，MIT）→ `NapCatAuthProvider(ws_url=...)` 复用协议端已有登录态，**不碰扫码、不存密码**，与我们现有 OneBot 适配器天然对接。
   代价：**★1、2026-08 后端停更**，等于自己维护一个 200 行级的私有协议客户端。
2. **要功能全/要 Go**：读 `guohuiyuan/qzone-go` 的接口常量表（**读思路，不拷代码**，AGPL 传染），照我们适配器风格重写。
3. **红线**：`Zhalslar`（GPL-3.0）、`qzone-go`/`aioqzone`（AGPL-3.0）、`qzonewall-go`（无许可证）**都不要拷代码**；只有 MIT 的（`Eganchiyu/qzone-sdk`、`SmartHypercube/Qzone-API`、`Procyon-Nan/qzone-sdk`）在许可证层面可直接用。
4. **先量风险再上**：建议先只做**只读**（看好友动态/查看自己空间）自测，再考虑发布；任何自动写操作都走小号 + 限频 + 可一键关闭。
