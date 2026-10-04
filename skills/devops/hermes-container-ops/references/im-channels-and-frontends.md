# IM 通道能力与前端选型

要判断“某个平台/某个前端能不能做某件事”时先看这里，别重新读一遍适配器源码。

## 一、现有通道能拿到什么（源码核实）

### QQ 官方机器人（`gateway/platforms/qqbot/adapter.py`）

- **群/私聊参与者只有 `author.user_openid`：拿不到昵称、拿不到群名片** → 群里无法按人区分身份。要“认人”只能自建 openid → 称呼 映射（存记忆库），别指望适配器给。
- **长文本=按长度切块逐条发**：`truncate_message(content, MAX_MESSAGE_LENGTH)` → `_send_chunk`（`reply_to` 只挂第一条）。**没有按语义拆多条气泡的能力** → 想让回复变成几条短消息，要么在输出侧做（把回复写成短段），要么换前端。
- 官方平台侧限制（适配器绕不过）：IP 白名单、只能进“自己为群主”的群、沙箱群人数上限、群全量消息要单独申请、机器人上线需人工审核。

### 微信（`gateway/platforms/weixin.py`）

头注释写明：**WeChat personal account adapter over Tencent's iLink Bot API** —— 我们接的**就是个人微信号**，走腾讯官方 iLink 接口（`getupdates` 长轮询收、出站**必须回带对方最新的 `context_token`**、媒体走 AES-128-ECB CDN、`qr_login` 扫码）。

→ “不能主动推送、主人先发才能回”的根因就是 context_token 必须回带，不是配置漏项。
→ **别为了“统一前端”把微信切到第三方个人微信协议端**（Gewechat 那一类）：那是拿腾讯官方接口换灰色协议，换来封号风险，属于降级。

## 二、Hermes 侧作为“被接入方”：三个面，按场景选

**别默认走 `/v1`——外部前端接进来有三条官方路，首选 webhook。**

1. **通用 webhook（首选，配置级、不改源码）**——`gateway/platforms/webhook.py`。收 HMAC 签名的 POST（GitHub/GitLab/Svix/Linear/generic），**把 payload 渲染成 agent 提示词**，跑完再把结果**投递回任意平台**。路由配在 `platforms.webhook.extra.routes`，每路由可单独配：事件过滤、`secret`（**必填**，只有 `INSECURE_NO_AUTH` 可免签且限定 loopback）、提示词模板、挂哪些 skills、投递方式（`deliver` / `deliver_extra` / `deliver_only`=payload 就是消息）、`cron_job`（每个事件触发一个已有 cron 任务）、限流、幂等缓存、体积上限、重放保护（V1 仅 body 签名已废弃）。
   → 文件不走消息体：**上游落盘 + payload 里带路径**，agent 侧直接读。
2. **`api_server`（OpenAI 兼容，默认 `127.0.0.1:8642`，需 `API_SERVER_KEY`）**——适合“就是个 OpenAI 客户端”的场景：
   - **无状态**：每轮都要带全量 messages（外部前端自己维护历史）。
   - 跑的是**带工具的完整 agent** → 从这条路进来的消息天然带 agent 腔。
   - **拒绝 `file` / `input_file` / 非图片 data URL**（400 `unsupported_content_type`）。
   - **有两个官方请求头可用**（源码 `gateway/platforms/api_server.py`）：`X-Hermes-Session-Id` = 会话续接（`/new` 会轮换）、`X-Hermes-Session-Key` = 记忆 provider 的稳定 scope（≤256 字符，官方示例形如 `agent:main:webui:dm:user-42`）。前端插件只发 `model/messages/stream` 就是**漏用了现成机制**：群聊想省 token 就别带 Session-Id（历史线性增长），要按人分 scope 就带 Session-Key。
   - **没有调用者身份**：只有一把共享 Bearer key，请求不带发送者 → 记忆 provider 把所有请求归到同一匿名来源，库里的 `user_id`/`user_name` 是空的。**认人只能由前端把 QQ 号/昵称/群号拼进消息**（或塞进 `metadata`）。
   - 社区有人做过几乎同构的事（NapCat → OneBot v11 → **NoneBot2** → Hermes api_server），成本口径是「群聊默认不带 session id + 收窄 `platform_toolsets.api_server`」；**AstrBot ↔ Hermes 的现成对接没有**（仓库搜 AstrBot 零命中）→ 这套插件是自己写的，别去找现成轮子。
3. **程序化协议（给外部程序驱动）**：ACP、TUI gateway 的 JSON-RPC、以及上面的 OpenAI 兼容 HTTP——官方文档 `developer-guide/programmatic-integration`。反方向 Hermes 也能驱动外部 agent CLI。

4. **第二角色/第二道门怎么落地：不要走 `/p/<profile>/`，用独立 sidecar 容器**
   - api_server 确实有 `/p/<profile>/<tail>` 原生多 profile 路由，但它由 `gateway.multiplex_profiles` 控制，而该开关的开机 preflight **明确排除 s6 容器**（我们正是）→ 实测直接 404 `Unknown or unconfigured profile`。要用它必须改活着 gateway 的配置并重启，不值得。
   - 正解：**同镜像 + 独立 HERMES_HOME 的第二个容器**当第二道门——`HERMES_HOME=/opt/data/profiles/<name>`、`hermes gateway run`、只开 `platforms.api_server`（换端口，如 8643）、不配任何 IM 平台。实测 `/v1/models` 返回该 profile 名、对话正常，且**主容器零改动**（不改 bind、不重发布端口、不重启）。
   - 新容器必须与主容器**同为 host 网络**才能连上只绑 `127.0.0.1` 的服务（见 SKILL.md「关键事实」）。
   - 建 profile 用 `hermes profile create <name>`（不加 `--clone-channels`，避免抢同一个 bot token）+ `hermes -p <name> config set platforms.api_server.extra.{host,port}`，密钥只从主 `.env` 复制必要几项。
   - ⚠️ sidecar 起来时 entrypoint 会把 `$HERMES_HOME`（= `profiles/<name>`）**chown 成容器内的 hermes = uid 10000 并以该 uid 运行** → 同挂载里属主 1003、mode 600 的共享文件（如 `auth.json`）它会读不到，日志出现 `PermissionError: '/opt/data/auth.json'`；**别把它当无害噪音**：api_server 照常起（该门 `/v1/models` 有应答），但共享状态文件读不到/写不了 → kanban dispatcher 每分钟 tick 失败刷日志，会话与凭证类写入也可能失败，等于门是“半瘫”。**修法**：给 sidecar 传 `-e PUID=$(stat -c %u <挂载根>) -e PGID=$(stat -c %g <挂载根>)`（= 主容器那套值），entrypoint 会把容器内 hermes 重映射成该 uid/gid 并 chown 数据卷 → 报错归零（本机实测）。`HERMES_UID`/`HERMES_GID` 同义；**不能用 `docker run --user <uid>` 代替**（entrypoint 明确拒绝任意 `--user`）。完整启动器见 `templates/chat-door-run.sh`。
   - 宿主侧给每个门写一个 `run-<门名>.sh`（`--network host --restart always -e HERMES_HOME=... -v <宿主目录>:/opt/data`）。脚本里**不要写容器路径**（`/opt/data/...` 在宿主上不存在），用 `ROOT="${HERMES_DATA_ROOT:-/vol1/1000/<USER>"` 顶住宿主/容器两种视图。

**prompt 成本量级（决定怎么压）**：走 `/v1` 一次闲聊，default profile 实测 **21k prompt tokens**；一个**空 SOUL** 的新 profile 仍要 **13k** → 大头是**工具 schema**，压成本要给该 profile 收窄 toolset，光精简人格文件没用。

**“Hermes 能不能做 X”先查索引**：`https://hermes-agent.nousresearch.com/docs/llms.txt` 一行一个功能并给出对应页面；答案是“能”之前别凭记忆说不能。

## 二·补、原生多角色：Bot Mode（不用引外部框架就能拆“聊天/干活”）

**Bot = 一个 Hermes profile**（独立 config / 模型 / 记忆 / 技能 / 凭证 / 聊天历史），不是新原语：`hermes -p <bot> chat` 进同一个 agent，routine 出现在 `hermes cron list`。Bot 能各自跑例程、**互相发消息**。

- headless 安装（我们这种没桌面端的）**默认拿不到 agent 侧的 `message_agent` 工具**：桌面插件写的那个标记文件没人写 → 要手动补两步：给 Bot 建 canonical 会话 `hermes -p <bot> chat -c "Bot Chat" --create-if-missing`，再在 `~/.hermes/profiles/<任一 bot>/profile.yaml` 里加一个空块。文档明说“bots message each other fine over the messaging platform”——**互相发消息走的是消息平台，不是 app 内**。
- 推论：**“新建一个 agent 专做干活 + 两边各自独立 + 互通”，Hermes 原生就有**，不必为此引入外部前端；外部前端买的只是**通道体验**（个人号、群内认人、气泡式多条）。
- Bot Mode 的“群聊”载体（app 内 vs 真实 IM 群）**没查透**，别当结论用。

## 三、引入外部聊天前端（AstrBot 类框架）的评估口径

只列**稳定性相关**的事实，完整调研靠临时查（版本号变得快，不写进来）：

- 定位：IM 接入 + 插件市场 + 自带 agent 循环的聊天机器人框架；模型侧原生支持 OpenAI / Gemini / Anthropic 三种 API 格式和 `$ENV_VAR` → **本地 ollama / llama.cpp 可直接挂**；v3.5.0 起支持 MCP。
- IM 覆盖 18+，QQ 有两条路：官方机器人适配器，或 OneBot v11 反向 WS 接 NapCat 类协议端（AstrBot 做 server）。
- **安全下限**：历史版本有 CVSS 9.8 的硬编码 JWT 密钥问题（可伪造 token → 上传恶意插件拿 shell，影响 <3.5.18）→ 版本必须高于修复线；**插件就是全权限 Python**，官方声明插件市场安全自负 → 只装必要的并审源码；WebUI 面板别裸暴露公网。
- 迭代很快 → **钉版本，别追新**（升级踩过“密码正确登不进”这类坑）。

### 能买到什么 / 代价

| 能力 | 官方机器人 | 个人号协议端 |
|---|---|---|
| 群成员身份/群名片 | 否（只有 openid） | 是 |
| 群全量消息 | 需单独申请 | 是 |
| 多条气泡式回复 | 只有按长度切块 | 可以（插件按空行 split 后分次发） |
| 封号风险 | 无 | **有**，风险与号里好友/群价值成正比 |
| 维护 | 官方接口稳定 | 跟客户端更新赛跑，断的是入口 |

- **一个 QQ 号不能同时挂官方 bot 和协议端** → 用**双门面**：老号留官方 bot 当干活门/兜底，新号走个人号当聊天门，两个门通向同一个核心与记忆库。切群渐进：先只放私聊观察风控，稳了再放群。
- **判定口径**：只要 QQ 私聊聊天，这层是纯开销（AI 味来自模型 / 提示词 / 上下文，不来自通道）；值得上的理由是“要群、要认人、要多平台、要气泡式回复”。

### 已验：插件能纯转发，但**接管方式决定分段得失**（源码 + 实测）

- **能纯转发**：插件用 `@filter.event_message_type(filter.EventMessageType.ALL)` 注册一个 handler 即接管全部消息（**无 filter 的 handler 会被跳过**）。`event.stop_event()` 后调度器立即 `break`，`ProcessStage` 里那段 LLM 调用**永不执行**（官方文档也写“后续所有步骤不会被执行…比如请求 LLM”）。另有几道保险：`event.should_call_llm(True)`（置 True 反而不走默认 LLM，语义反直觉）、handler 里 `await event.send()` 即满足跳过条件、config `provider_settings.enable=false` 进程级关掉自带 AI。
- ⚠ **`stop_event()` 会连 `ResultDecorateStage`（分段切分）和 `RespondStage`（多条间隔节奏）一起跳过** → 框架自带的「气泡式多条」全失效，等于要自己重写一遍。`event.send()` 同理直连适配器、绕过装饰层。
- ✅ **要白借它的分段层，就别掐断事件**：只关 LLM（`provider_settings.enable=false`），handler 里 `yield event.chain_result([...])` / `plain_result(...)` 把结果交回消息链 → 切分与间隔照常生效。
  - 分段配置在 `platform_settings.segmented_reply.*`：`enable`、`only_llm_result`（**插件回复不是 LLM 结果 → 这一项必须关**，否则不分段）、`split_mode`(regex|words)、`regex`、`split_words`、`words_count_threshold`（默认 150，**超过阈值的长消息直接发、不分段**）、`content_cleanup_rule`；间隔 `interval_method`(random|log) + `interval`（默认 1.5,3.5 秒）+ `log_base`。
  - 分段**按平台名硬排除三家**：`qq_official_webhook`、`weixin_official_account`、`dingtalk` → OneBot 协议端不在名单里，正常生效。
  - 切分**只对纯文本（`Plain`）组件生效**：图片/表情/@/引用等非 Plain 组件原样保留、不参与切分。
  - 切分实现 = `re.findall(regex, text, re.DOTALL | re.MULTILINE)`，默认 `.*?[。？！~…]+|.+$`：遇句末标点断一刀，**剩下的短尾巴也单独成一刀**——实测 `在呢主人，什么事喵？(´･_･\`)` 会被切成两条、颜文字被孤立成一条；要清掉某类片段用 `content_cleanup_rule`（逐段 `re.sub`）。
  - ✅ **想自己控制切法、又不丢发送节奏**：把 `regex` 设成 `(?s).+`（findall 回自身 = 内置切分变 no-op），改由**自己的插件**产出多个 `Plain` 组件。`RespondStage.is_seg_reply_required()` 只看 `enable_seg`（+ 平台排除 + `only_llm_result`），**不要求真的切过** → 随机间隔照旧生效。切法归你、节奏归框架。
  - 间隔算法在 `RespondStage._calc_comp_interval`：`interval_method=random` → `uniform(interval[0], interval[1])`（默认 1.5–3.5 秒）；`log` → `log(word_count+1, log_base)` 到 `+0.5`。`Reply`/`At` 这类头部组件会被 `_extract_comp` 抽出、**只挂第一条气泡**。
  - 另有独立支线：长回复可走 **t2i 转图片**，阈值是另一个键 `t2i_word_threshold`（默认 150、下限 50）→ 开了 t2i 时长回复会变成图片而不是长文本。
  - 代价：不掐断事件 → 同一条消息上**其它插件的 handler 也会跑**（可能重复回复）⇒ 这个实例里别装其它聊天类插件。
- 绕过 LLM 后框架**不再维护会话历史**（历史只在 agent 路径写入）→ 上下文必须由插件自己带给后端。
- 插件写法易错点：`@register(...)` **已废弃**（新版本自动识别 `Star` 子类，别再加装饰器）；回复用 `event.plain_result` / `chain_result` / `image_result`；附件落盘 `await image.convert_to_file_path()`、`await file.get_file(allow_return_url=True)`，临时文件有清理钩子、要留就得 untrack；插件目录里放 `metadata.yaml` + `_conf_schema.json`（WebUI 配置项）并在 `__init__` 收 `config`；HTTP 只能用 `aiohttp`/`httpx`（官方禁 `requests`）。
- **改不动的**：管线 stage 不可插件化（`register_stage` 直接 `ValueError`，stage 列表硬编码）；内置 `astrbot` 星无法禁用（只能在插件层绕开）。

### 已接：个人号协议端 + 纯通道的配置级落点（实测通过）

**先一次改完再重建**：协议端**容器重建**（改挂载/镜像/环境变量）会打废登录态、要主人重新扫码（日志：`正在快速登录 <uin>` → `登录态已失效，请重新登录。` → 回落二维码）；单纯 restart 通常保得住。边接边重建会让主人白扫一遍——NapCat / AstrBot / 插件三处配置一次写完，最后才重启。

**NapCat（协议端）**
- 配置按登录账号命名：`napcat/config/onebot11_<uin>.json`、`napcat_<uin>.json` → 可据此确认到底登的是哪个号；WebUI 配置在 `napcat/config/webui.json`。
- 反向 WS 客户端（NapCat 作 client）：`network.websocketClients = [{"name","enable":true,"url":"ws://127.0.0.1:6199/ws","messagePostFormat":"array","token":"<与 AstrBot 同值>","heartInterval":30000,"reconnectInterval":3000}]`；另可开只绑 `127.0.0.1` 的 `network.httpServers`（同 token）便于自己发消息自测。
- **扫码运维**：二维码在容器内 `/app/napcat/cache/qrcode.png`，日志同时打印 ASCII 码与 `二维码解码URL`。**码约每 2 分钟自动轮换**，日志里那条 URL 可能已过期 → 以**当场 `docker cp napcat:/app/napcat/cache/qrcode.png` 拷出来的图为准**；原图仅 ~147px，用 PIL `NEAREST` 放大 5–6 倍再发才扫得动（本机系统 `python3` 没装 PIL，用 `/opt/hermes/.venv/bin/python`）。
- 登录成功判据（日志）：`二维码已被扫描，等待确认…` → `本账号数据/缓存目录： /app/.config/QQ/NapCat/data`。
- WebUI API 别耗时间：`POST /api/auth/login` 用 webui.json 的 token 常回 `token is empty`，走日志 + 拷 PNG 更快。

### 协议端长期运维：免扫码 / 掉线 / 反检测开关

- **免扫码 = 环境变量 `ACCOUNT`（源码级结论，2026-09-20 实查 + 实测）**：镜像 `entrypoint.sh` 末行是 `gosu napcat /opt/QQ/qq --no-sandbox -q $ACCOUNT` —— 它读的是**环境变量 `ACCOUNT`**，**命令行参数被完全忽略**。曾经流行的写法 `command: ["-q","<uin>"]` **从来没生效过**（`docker inspect` 里 Cmd 明明带着 `-q`，日志照旧打「没有 -q 指令指定快速登录」）。正确写法：compose 里 `environment: - ACCOUNT=<uin>`。**吃到参数的特征日志是 `正在快速登录 <uin>`**，拿这个当唯一判据——出现它才算 `ACCOUNT` 生效。
- **重建/重启都免扫码（2026-09-20 实测，推翻旧结论）**：把 `ACCOUNT` 改对并扫码成功一次之后，`docker restart napcat`（22:22）与 `docker compose up -d --force-recreate napcat`（22:23）**都直接走快速登录、没再出二维码**，AstrBot 适配器同步自动重连。原因是登录态目录（`ntqq`）完整映射 + `ACCOUNT` 传对了号；旧结论「重建必打废登录态」是因为当时 `-q` 根本没生效。⚠️ 仍有例外：登录票据**过期或上一次是回落扫码登的**，重建后会打 `登录态已失效，请重新登录。` → 那就得扫一次；所以会重建协议端容器的改动，仍尽量挑主人在线时做，并备好二维码取图流程。
- **数据统一落在映射目录的 `stack/` 下（分类约定，主人要求「都映射出来 + 合格分类」）**：`stack/napcat/{config,ntqq}`（配置 / QQ 登录态，几百 MB、日志实时落盘）、`stack/astrbot/data`、第二道门的 profile 目录；**代码与文档留在 `chat-layer/`**（compose、脚本、PLAN、`plugin/hermes_forward`、`stickers`——插件与素材仍 bind 进容器，但归位在代码层）。判据：`docker inspect <容器> --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}'` 里**不该出现 `/var/lib/docker/volumes/…`**。本机 Hindsight 曾是这样一块（6.8G 的 `hindsight-data` 卷），**2026-09-20 已迁到 `stack/hindsight/data` 并用 compose 接管**，迁移配方（可复用，含属主坑）：① 记下数据属主 `stat -c '%u:%g' <卷>/_data/<任一文件>`（Hindsight 是 `1000:1000`，不是 1003！）② 停容器 ③ 用镜像自身做容器内拷贝，避免宿主权限纠缠：`docker run --rm --user 0 --entrypoint sh -v <卷>:/from:ro -v <目标目录>:/to <镜像> -c "cp -a /from/. /to/ && chown -R <uid>:<gid> /to"`（6.8G 用了 4.6s）④ **双向校验**：`du -sh` 两侧一致 + `find | wc -l` 文件数一致 ⑤ 写 compose（`network_mode: host` + 原容器的全部 env，用 `docker inspect --format '{{json .Config.Env}}'` 原样搬）⑥ `docker rm -f <旧容器>` → `docker compose up -d` ⑦ 验业务而非只验端口：`/health` → 业务端点（Hindsight 看 `/v1/default/banks` 的 `fact_count`，本次 28469）→ 从 Hermes 侧真发一次 `hindsight_recall` ⑧ 全过之后才删旧卷。容器 `restart: unless-stopped` → 宿主重启自动拉起。删容器不删数据；但**容器被重建**（`compose down/up`、换镜像或 command）**会重置协议端自己的部分配置**（如反检测开关，见下），重建后要按备份还原。
- **扫码要两台设备**：手机扫屏幕 → 显示二维码的设备（电脑开 `http://<宿主>:6099` 面板，或电脑上打开发过去的 PNG）+ 手机。只带一部手机时扫不了——**别把「发链接/长按识别」当替代方案**，部分客户端不认。
- **别开面板里的「反检测开关」**：维护者原话「一般该功能不要修改，**暂不可用**」（有人复现：开了保存、重启后又变回未开），另有「打开反监测中的部分选项导致**启动时崩溃 / 无法正常登录**」。翻近半年 issue 也没有「开了之后风控变少」的证据 → 收益无证据、风险明确，全部保持 `false`，`o3HookMode` 是默认值不动。开关存在 `napcat/config/napcat.json`（`bypass: {hook,window,module,process,container,js}`），**容器重建会被重置** → 值备份进项目 backup 目录。
- **真正的问题是被踢下线 / 掉线，解法是版本策略而不是反检测**：社区实测「最新版几小时被踢、钉到旧版能稳好几天」，且近期仍有「被踢后无法重新登录」「周期性崩溃」的 issue → 上线后**观察掉线频率**，频繁被踢就钉 NapCat/QQ 版本；NapCat 另有「被踢下线时执行自定义 Hook」这类能力可做掉线通知。
- **面板与 token**：`napcat/config/webui.json`（600）里有 `token`（默认 12 位随机）、`enable2FA`、`ipWhitelist`、`accessControlMode`。面板 token 是**涉密值**：只在私聊里给主人一次、不写进任何文件与记忆、必要时轮换；改 `webui.json` 要重启协议端才生效 → **主人正在扫码 / 刚扫码时别动**。

**AstrBot（纯通道）**
- `data/cmd_config.json` 是 **UTF-8 with BOM**：读写都用 `encoding="utf-8-sig"`，否则 `json.load` 抛 `Unexpected UTF-8 BOM`。
- `platform` 是**数组**；aiocqhttp 条目（AstrBot 是 WS **server**）：`{"id":"aiocqhttp","type":"aiocqhttp","enable":true,"name":"napcat","ws_reverse_host":"0.0.0.0","ws_reverse_port":6199,"ws_reverse_token":"<token>"}`。
- 接管全部消息：`wake_prefix: [""]`（**空串 = 任何消息都算唤醒**）+ `platform_settings.friend_message_needs_wake_prefix=false` + `ignore_bot_self_message=true`。
- 关自带 AI + 开分段：`provider_settings.enable=false`；`platform_settings.segmented_reply.enable=true`、`only_llm_result=false`。
- 插件配置落盘在 `data/config/<插件目录名>_config.json`（不在插件目录里）。
- **属主**：`data/` 下文件由 AstrBot 容器内 **root** 写 → 从 Hermes 容器（uid 1003）读改都 `Permission denied` → **配置改动走宿主 `sudo python3` 脚本**，别在 Hermes 容器里改。
- 验证：`ss -ltn | grep 6199` 有监听；AstrBot 日志出现 `Loading IM platform adapter aiocqhttp` 与 `Loading plugin <插件>`。

**插件侧两个易错点**
- 清单/配置文件里写“说明 + 示例”（值是对象而非字符串）会让加载器在 `path / value` 上炸：`unsupported operand type(s) for /: 'PosixPath' and 'dict'` → **加载器要跳过 `_` 前缀键与非字符串值**，清单里才留得住注释。
- 插件配置里的后端地址要写**sidecar 门自己的端口**且 profile 留空；写成默认门 `8642` 会把聊天流量打回干活 profile。

### 上线自检：逐段验，别拿“看着像通了”当通

**协议端真值只认 OneBot 接口**（`network.httpServers` 那个只绑 `127.0.0.1` 的口，带同 token）：
- `POST /get_login_info` → `data.user_id` + `data.nickname`：这是“到底登的是哪个号”的唯一硬证据（配置文件名 `onebot11_<uin>.json` 只能当旁证）。
- `POST /get_status` → `data.online: true`。
- `POST /get_friend_list`：条目里的 `email`/`phone_num` 可反查身份（如某好友条目带 `<主号>@qq.com` → 确认那个号就是主人的主号）。
- 出站自测：`POST /send_private_msg {"user_id":<对方>,"message":"..."}` 返回 `message_id` = 协议端→QQ 这一段通。

**通道是否真把事件交出来了**：`ss -tnp | grep 6199` 应看到**一对** ESTAB（协议端进程 ↔ python）；AstrBot 日志出现 `aiocqhttp(OneBot v11) 适配器已连接。`。

**两类日志别误读**（都不是消息事件）：
- 协议端日志里的 `Adapter opened: debug-primary` / `Adapter debug-primary 不活跃，自动关闭`——打开 WebUI 面板时出现，与收消息无关。
- 登录前每 ~2 分钟一轮的二维码行。反过来也有用：**日志里不再出现新二维码行** ≈ 已经登录（再配 `get_login_info` 确认）。

**判定“入站链路通了”的唯一证据是后端侧出现转发记录**（插件日志、后端请求日志），协议端日志不算数。

**插件配置是加载期读进内存的** → 改 `data/config/<插件>_config.json` 之后必须重启 AstrBot 才生效；只重启后端（sidecar）不生效。

### 仍未验（没验过就不是结论）

- 个人号协议端在目标号上的风控表现（只能并行跑几天看，至今无权威数据）。
- AstrBot 的「群聊」类能力在真实 IM 群里的载体（app 内 vs 真群）。

## 三·补、QQ 个人号桥接：现成设计可抄，后端契约不可抄

参考项目 `Derpyu520/qq-bridge`（QQ ↔ **DSH** 桥）+ `snowluma`（NTQQ 远程协议框架，OneBot v11，自带 WebUI/TS SDK/MCP，与 NapCat 同类）。

**数据流**：`QQ → SnowLuma(OneBot v11 WS) → 桥接进程 → 后端 Web API`，回复/提问/审批原路回 QQ。
**后端契约是后端专有的**（DSH：launch token 换 Cookie、`/api/<ns>/<method>` 斜杠 RPC、`/api/remote.mux` 事件流）→ **换后端必须重写桥接的适配层**，别指望原样复用；Hermes 侧对应物就是上面第二节的 webhook / OpenAI 兼容端点。

**真正值钱的是它 QQ 侧那套“让 AI 在群里像人”的设计，可直接照抄**：

- **引用消息解析成 `[引用 某人：原文]正文` 再注入** → AI 知道这句对谁说；**引用机器人自己则视为必回**；顺带治“把群友之间引用的第三方对话误当成对自己说”。
- **会话映射**：每个 QQ 会话（私聊/群）对应一个独立 agent 会话，持久化（`state/sessions.json`）——别把所有群塞进一个上下文。
- **气泡式多条**：输出侧按分隔信号拆（一代用空格分句、二代传数组给发送工具）→ 这正是官方机器人拿不到的“多条短消息”。
- **`[SILENT]` 潜水**：AI 可显式表示这轮不接话，桥接静默不发 → 群聊里“不是每句都要回”的能力。
- **自主收发工具化**：给 AI 一组 MCP 工具（取未读/发消息/引用回复/取群历史），由它决定何时看、何时发、何时设唤醒或潜水，**不是被动一问一答**。
- **仿真状态机**：观望 / 活跃 / 试探 / 退场，选择性参与、主动收尾。
- **交互回传**：agent 的提问（`ask_user_question`）与工具审批都转发到 QQ，回“通过/拒绝”即可决策。
- **安全面**：发送工具强制白名单、web 抓取带 SSRF 防护、角色设定只由管理端注入（群友改不了）、本地控制台带 token。

## 四、接外部前端时的记忆隔离

两个前端都往同一个记忆库写会污染事实召回：**约定 tag 区分（聊天只写日常流水、干活写事实），召回按类型过滤**；人格一致性要么共享同一份人格提示词，要么明确承认是两个角色。
