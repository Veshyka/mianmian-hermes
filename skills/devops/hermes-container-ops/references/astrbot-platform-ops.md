# AstrBot 平台运维（provider / LLM 工具 / MCP / 面板口令）

适用：本机容器 `astrbot`（host 网络；数据 `<映射>/stack/astrbot/data`，源码 `/AstrBot`）。按主题分节：① 面板口令 ② 凭证交付规矩 ③ 平台能力事实 ④ 改配置前置 ⑤ 写人格 ⑥ 停用/替换插件 ⑦ 直聊链路验证 ⑧ 记忆双向（retain + recall） ⑨ 连发合并（防抖）插件。

## 一、面板口令（dashboard）

**它存在哪**——`<data>/cmd_config.json` 的 `dashboard` 段：`username`、`password`（**32 位十六进制 = MD5**）、`pbkdf2_password`（118 字符）、`password_storage_upgraded`、`password_change_required`、`jwt_secret`、`host`、`port`（本机 6185）、`auth_rate_limit`、`totp`、`ssl`。

→ **明文不可逆**（只存哈希），没有"找回密码"这条路，只能重置；容器日志里通常也没有初始密码行（首启横幅早被覆盖）。先诊断再承诺：只报字段类型/长度，不打印值。

**重置两条路**：

1. **交给主人自己跑**（交互式，密码不进命令行、不进对话，首选）：
   ```bash
   sudo docker exec -it astrbot astrbot password          # 可加 --username <名>
   ```
   `astrbot password` **没有 `--password` 参数**（就是靠交互输入），所以值不会落在任何命令行里。
2. **程序化重置**（需要棉棉自己掌握新值再交给主人时）：在容器内用**它自己的哈希函数**，别手搓算法：
   ```python
   import sys; sys.path.insert(0, "/AstrBot")
   from astrbot.core.utils.auth_password import hash_dashboard_password, hash_md5_dashboard_password
   ```
   写 `pbkdf2_password` / `password` / `password_storage_upgraded=True` / `password_change_required=False`；明文**只落到容器 `/tmp` 下 0600 文件**，由宿主 `docker cp` 取走后**立刻删容器内那份**。写前把 `cmd_config.json` 备份到 `ops-backups/`。

**生效与验证**：

- `sudo docker restart astrbot` 后生效（数秒级）；**重启免扫码**（NapCat 的 `ACCOUNT` 写对即可，见 `im-channels-and-frontends.md`）。重启后确认 `aiocqhttp(OneBot v11) 适配器已连接`。
- 别把"文件写进去了"当结果——用**读文件、绝不回显**的脚本打 `POST http://127.0.0.1:6185/api/auth/login`（body `{"username":…,"password":…}`），**HTTP 200 且回包含 token** 才算通。容器是 host 网络，直接访问 6185，不必绕 SSH。

## 二、凭证交付规矩（主人要口令/密钥时）

- **不把口令写进聊天正文**，哪怕主人说"以前你都这么发的"——聊天通道会在平台侧与本地日志各留一份，一次就永久。口头说明一句即可，不反复讲道理、不加训导。
- 合规交付：**生成/重置在代码里做、全程不回显** → 明文只落 **0600 文件**，放在主人的映射目录里（他能从文件管理器直接打开）→ 告诉他路径，并建议他改成自己记得住的值、用完删文件。
- 需要棉棉自己登面板时：优先让主人把口令存进密码库（遮蔽输入，值不进对话），棉棉只做"填充"动作。
- 交付类口令跑完即清：容器内临时副本删掉、宿主 `/tmp` 不留痕。

## 三、平台能力事实（4.28.1，源码/实测）

- **provider 配置键名不是 `providers`**：`provider_sources`（连接层：`id/type/provider_type/enable/key/api_base/timeout/proxy/custom_headers`）+ `provider`（模型层：`id/provider_source_id/model/enable/provider_type`）。`key` 是**列表**且支持 `"$ENV_VAR"` 间接引用 → 密钥可以不落配置文件。OpenAI 兼容的 `type` 用 `openai_chat_completion`、`provider_type: chat_completion`；DeepSeek 内置模板 `api_base=https://api.deepseek.com/v1`。
- ⚠️ **开 `provider_settings.enable` 不会让插件闭嘴**：插件只 `yield` 结果不算"已发送"（不设 `_has_send_oper`），`star_request` 每轮还 `clear_result()` → LLM 仍会再答一次 = **双回复**。所以"切直聊"与"摘掉转发插件的消息 handler"必须同一批做。
- **LLM 工具（function calling）**：`@filter.llm_tool(name="...")`。⚠️ 它**解析 docstring 的 `Args:` 段**（`参数名(类型): 描述`），**不读类型注解**，显式 `parameters=` 会被忽略 → 改参数必须连 docstring 一起改，否则 LLM 看不到。工具名只允许 `[A-Za-z0-9_-]`。
- **MCP 客户端**：`<data>/mcp_server.json` = `{"mcpServers": {"<名>": {url, transport, headers, active}}}`；远程 HTTP 用 `transport: "streamable_http"`（不写会按 SSE 走）。连上后远端工具**自动进 LLM 工具表**（`list_tools_and_save()` → `func_list`），不用逐个配置。自检：容器内 `curl -X POST <url> -H 'Accept: application/json, text/event-stream' -d '<initialize JSON-RPC>'` 回 200 且带 `serverInfo` 即可用。
- **pipeline 固定顺序**（…ProcessStage → ResultDecorateStage → RespondStage）：`provider_settings.enable` 只决定 ProcessStage 里那步走不走 LLM，**不会跳过分段**；`aiocqhttp` 不在分段排除名单 → 直聊与分段互不冲突。
- **原生历史**：SQLite `<data>/data_v4.db` 的 `conversations` / `platform_message_history`；`compression.max_turns=-1` = **不按轮数截断**，超 ~128k token 才走 LLM 压缩。
- 与 Hermes 互达：host 网络下 AstrBot 容器内 `127.0.0.1:9901`（Hermes 干活门 A2A）**实测可达**。派活契约 = JSON-RPC `message/send`，`params.message = {role, messageId, parts:[{kind:"text", text}]}`；回包取 `result.status.message.parts[0].text`（备用 `artifacts[].parts[].text`）；同步等待上限 `A2A_REPLY_TIMEOUT`（默认 300 秒）。给它的工具写成插件：先按契约 POST，返回字符串即可。
- **分段分隔符定案：用 `※`**（冷门符号；不用 `.`——小数/网址/英文缩写里到处都是点）。**两处必须配套改**：切分正则字符类加它（`.*?[。？！~…※]+|.+$`）＋ `content_cleanup_rule: "[※]"` → 只做分隔、**不出现在消息里**。⚠️ **符号不进切分字符类就永远不切**，只改 cleanup 等于没改（cleanup 是清理层、不是切分层）。实测 3 句 → 3 条气泡、看不到符号；`。？！~…` 仍能切。选符号判据：日常聊天不打出来、不是正则元字符（`] \ ^ -` 要转义）、不会被 JSON/markdown 吃掉；候选 `※ ¦ § †`。符号若漏到消息里是**好事**——等于清理规则失效的告警，比隐形控制符好排查。人格侧对应写「每条短消息末尾写一个 `※`」，示例也用 `※`。
- ⚠️ **别拿「名字看着对」当「能用」**：模型名能不能跑只能实测——用配置里那把 key 直接打 `<api_base>/chat/completions` 试几个候选名。**若多个名字都回 200 且回显同一个 model，说明端点不是官方 API 而是中继/代理**（官方对未知模型名回 400）→ 别据此断定「模型名配对了」，报结论时把端点性质一起说清。
- **「改完到底生效没有」的五条日志判据**（重启后 `docker logs astrbot --tail 80 | grep -iE 'provider|mcp|llm tool|plugin|aiocqhttp'`），比回读配置文件可信：① `Loading model openai_chat_completion(<id>)` + `Selected … as default chat model provider` = provider 生效 ② `Connected to MCP server <名>, Tools: [...]` = 记忆库接上（本机 Hindsight 一次进 32 个工具）③ `Added llm tool: <名>` = 自定义工具注册成功 ④ `Loading plugin <名>` = 插件加载 ⑤ `aiocqhttp(OneBot v11) 适配器已连接` = 小号在线。缺哪条就查那条。

## 四、改它配置的前置

- 任何 provider / 插件 / MCP 改动都**先备份** `cmd_config.json` + `mcp_server.json` + 插件目录，并留一行回滚（`provider_settings.enable=false` + 还原插件 + 重启）。
- `stack/*/` 是 root 属主：容器外改不了目录里"新建"文件，但 `cmd_config.json` 本身归 root → 走 `docker exec` 用容器内身份写（`docker cp` 出/入亦可）。
- 端口/网段：容器与 Hermes 同为 host 网络时，`127.0.0.1:<port>` 两边一致；非 host 网络时容器里要用**宿主 `172.17.0.1`**（反向见 SKILL.md 的 socat 小节）。
- **批量改正则/开关时，把「写配置文件」放在脚本最后一步**：脚本崩在中途时，已经落地的副作用（数据库插入、MCP 文件写入）留着、而配置写入没做，状态半新半旧。写出来的应用脚本要**每个副作用独立、幂等、可重跑**，开头先打印现状（provider.enable / regex / persona_id / personas 行 / 插件入口），跑第二遍不报错。

## 五、写人格（persona）

人格存在**数据库**里，不在配置文件：`<data>/data_v4.db` 的 `personas` 表（本机出厂 0 行，默认人格 id 是 `default`）。

- 列与类型**照抄别猜**：`created_at`/`updated_at` DATETIME（写 `'YYYY-MM-DD HH:MM:SS'` 字符串）、`id` **INTEGER 主键（rowid 别名）→ 插入时不要给值**（塞 uuid 会 `sqlite3.IntegrityError: datatype mismatch`）、`persona_id` VARCHAR、`system_prompt` TEXT、`begin_dialogs`/`tools`/`skills` JSON —— ⚠️ **`tools`/`skills` 留 NULL，绝不写 `"[]"`**（语义见下一条）、`custom_error_message` TEXT、`folder_id` VARCHAR（可 NULL）、`sort_order` INTEGER NOT NULL。查类型用 `PRAGMA table_info(personas)`，别拿 `select *` 的列名猜。
- 插完行再把 `agent_runner.config.persona.persona_id` 指向新 id；`provider_settings.persona_pool` 默认 `["*"]` 不用动。
- ⚠️ **`persona.tools` 是工具白名单，`NULL` 与 `[]` 天差地别**（源码 `astrbot/core/astr_main_agent.py`：`persona.get("tools") is None` → `tmgr.get_full_tool_set()` 给全量；是 `[]` → 一个工具都不给）。填成 `"[]"` 的症状是模型**自己说「工具够不着 / 我没权限 / 手被绑着」**——看到这种自述先查 persona 的 tools 字段，别先怀疑模型能力或插件没装。同理 `skills` 留 NULL；只想给部分工具就显式列工具名（等于顺手做能力收窄）。
- **人格正文就是替代整套 Hermes 系统提示词的那份**：从 SOUL 里摘「说话规则/分段/禁止/接话/示例/尺度」，砍掉 Hermes 工具规则（a2a / skill / file / memory 的工具名），改写成 AstrBot 侧的说法（回忆 / 记住 / 派活）。实测这样一份 **3,447 字符 ≈ 1,723 token**，对比整包 14,458 字符 ≈ 7,229 token ≈ **降 76%**——这就是「聊天不该按干活的起步价计费」的落地方式。
- 改完重启才生效；判据不只是配置回显，而是日志里按这份人格回话（或查 `conversations` 表新落的 system prompt）。

## 六、停用/替换插件（目录里有挂载点时）

- **插件目录里可能挂着 bind mount**（本机 `hermes_forward/stickers` 就是）→ 整个目录**搬不动**：`shutil.move` 先试 `os.rename` → `OSError: [Errno 16] Device or resource busy` → 退化成 `copytree + rmtree`，**把原目录删掉一半后抛错**（留下坏状态）。要停用就**只动入口**：`main.py` → `main.py.disabled`（扫不到入口即不加载，还原也是改个名）。
- 停用一个、装一个时**顺序有讲究**：先装新工具插件、再停旧转发插件、最后开 `provider_settings.enable` —— 这三样同一个批次做完，中间任何一步单独生效都会出现「双回复」或「没人回」。
- 停完用 `ls <plugins>/<旧插件名>/` 确认入口真的不在（只剩挂载的素材目录是正常的）。

## 七、直聊链路三个容易漏的验证

- **先验端点支不支持 function calling，再怀疑模型**：用配置里那把 key 打 `<api_base>/chat/completions`，带一个假工具（`tools: [{type:function, function:{name, description, parameters}}]` + `tool_choice: "auto"`），看回包 `finish_reason` 是否 `tool_calls`、参数是否填对。支持 → 「模型不会用工具」这个假设可以划掉，回去查工具白名单与插件注册（本机实测某中继端点：`finish_reason=tool_calls` 且正确返回 `get_weather({"city":"兰州"})`）。
- **记忆不会自动回填**：AstrBot 侧的 Hindsight 只是**一套工具**（`recall`/`retain`/`reflect`…），模型想调才调 → 闲聊基本不会进库；AstrBot 自己的 `data_v4.db.conversations` 会自动存历史（跨轮上下文连续，不按轮数截断），但**不进共享记忆库**。要「自动记住」得自己写插件挂 `on_llm_response` / `on_agent_done` 钩子，把每轮「用户说的 + 回复」按共享 bank 的 source/tags POST 进去（钩子名见 `astrbot/api/event/filter/__init__.py`：`on_llm_response`、`on_agent_done`、`on_decorating_result`）。**Hermes 门的每轮自动 recall/retain 是那一侧自带的能力，切直聊就等于丢掉——要主动向主人说明这是能力损失**，别等他发现「她好像不记得事了」。
- **端到端只能靠主人**：agent 造不出 QQ 入站消息。链路切完请主人从真号发一条，验三件事——① 直聊生效（回复不再经 Hermes）② 多句切成多条气泡且看不到分隔符 ③ 让它干件小事时**真的调了工具**（如 `delegate_to_hermes`）。切之前自己能把这三条验掉的只有：四个日志行（见 §三）、MCP 探针、A2A 直连探针。

## 八、记忆双向：自动回填（retain）+ 自动召回（recall）

- 实现落在宿主侧插件 `hermes_memory`（源码副本 `chat-layer/plugin/hermes_memory/`）。它把**两个方向合成一个模块**：共用一个 Hindsight HTTP 客户端与端点/bank/超时常量、一套守卫函数；配置写在 `<data>/config/hermes_memory_config.json`（`retain_enable` / `recall_enable` / `recall_top_k` / `recall_budget` / `recall_max_chars` / `recall_timeout_s` / 私聊 umo 白名单…），启动打**一行**同时含两方向就绪状态（`就绪 bank=… | retain=on(…) | recall=on(…)`）→ 复核就 grep 这一行。新增能力时**别写成第二个插件**，否则端点/守卫/日志各一份，改一处漏一处。
- **自动召回用的是官方的 `@filter.on_llm_request()`**：每轮请求前拿本回合用户消息当查询 → 召回 → **原地**往 `req.contexts` 追加一块（role=user）；注入点在 `build_main_agent` 之后、真发请求之前，是「最后一手」，所以改 `req` 有效（换绑变量名无效）。同一个钩子也是干活门回执注入用的位置——**一个钩子多用途，合成一个插件**。
- 守卫（共用，两方向都走）：只对主人私聊 umo 生效、跳短噪声与 `/` 命令、**跳过「回执注入轮」**（别给回执本身做召回）、一回合只注入一次；召回失败/超时**一律静默放行**（不注入也正常回话）。同步召回会给每轮加延迟，所以超时值必须是硬上限。
- **延迟量级**：本机实测 0.33~0.50s/次（GPU 尖峰时接近上限 3s 后降级）→ 加在每轮可接受。嫌慢先调小 `top_k`/`recall_max_chars`，别直接关。
- ⚠️ **Hindsight 的 recall 响应里 `score` 恒为 0**（本机实测：连问 5 个无关问题、每个都返满额结果、`score` 全是 `0`）→ **`min_score` 之类的相关性门槛在插件侧是死键**：设 >0 会把结果全筛掉，设 0 等于不过滤。相关性只能依赖库内排序 —— 调 `budget`/`max_tokens`，或在提示词里让模型自己取舍；**别把「按分数过滤」写进方案**。
- 验证分三层，**顺序不能省**：① 离线纯逻辑单测（假 request 对象）② 容器内**真框架**探针（真装饰器 + 真 `ProviderRequest` + 真 `call_event_hook`，断言 `req.contexts` 增长且首行是注入标记）③ 主人发一条真消息后 grep 注入日志。①② 都不能替代 ③——真实 LLM 请求只有真人能触发，子代理的「应该会注入」不算证据。

## 九、连发合并（防抖）插件

- 挂 `@filter.event_message_type(EventMessageType.ALL, priority=100)`；每条消息在 event bus 里是**独立 task**，所以在自己的 handler 里 `await` 一个静默窗口不会堵住收消息。
- 压制靠 `event.stop_event()`：洋葱链在 `pipeline/scheduler.py` 断掉，后续 stage（含 LLM 调用）不跑；模型看到的 prompt 就是 `event.message_str` → 「合并」= 把几条拼进一个 event 再放行。
- 参数在 `<data>/config/<插件>_config.json`（`wait_seconds` / `max_wait_seconds` / `scope`），**插件只在启动时读一次** → 改完必须重启 astrbot。窗口语义要分清：**来新消息重置倒计时**（不是固定间隔），并配一个硬上限（否则一直等不到头）；主人嫌慢/嫌快都调 `wait_seconds`，别动上限语义。
- ⚠️ 它会**重建事件对象** → 任何靠 `event.set_extra()` 传的标记都会丢；别的插件要区分「这条消息是谁造的」时，除 extra 外必须再加一个**正文里的死标记**做第二保险。
- 与别的入站注入共存时要注意：主人正在打字时，外部注入的消息可能被并进他那轮 → 那条注入不会单独跑一轮 agent。要「每条注入都稳拿一轮」就得让注入消息不参与合并。
