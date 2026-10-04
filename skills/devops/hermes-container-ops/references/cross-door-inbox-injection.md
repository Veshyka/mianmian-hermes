# 跨门派活：非阻塞收件箱 + 入站注入

**要解决的问题**：`a2a_call` 是同步调用——派活门那一轮要等对方跑完（长活直接撞 300s 超时），主人也看不见任务书、没法中途插话。

**做法**：派活方写文件就走，接收门的插件把任务书当**真入站消息**注入目标会话 → 接收门在**那条会话里**被激活一轮，主人当场看得见、能直接给审批。

## 三个方向，三种工具（不许混）

| 方向 | 工具 | 效果 |
|---|---|---|
| 让**对门的 agent** 动起来 | 插件 `PluginContext.inject_message(content, role="user", *, session_key=…)`，由 `gateway/run_inbound.py::_dispatch_plugin_message_injection` 派发，走目标会话**自己的适配器** | **真入站一轮**：会话 idle 就新起一轮、正在跑就**打断**那一轮 |
| 只想让对门**看到/主人看到** | `hermes send --to <platform>:<chat_id> "…"` | 出站：落一条 `role=assistant` 的镜像行，**不触发一轮、也不会被当指令** |
| 定时/延迟投递 | cron `deliver: "<platform>:<chat_id>"`（要能续聊再加 `attach_to_session: true`） | 由持有该平台适配器的网关发出（跨门派活要把目标写进任务书） |

- `session_key` 形如 `agent:main:<platform>:<chat_type>:<chat_id>`（从 `state.db` 的 `sessions` 表读，表里列名是 `session_key/chat_id/chat_type/user_id`）。
- 各门能投到哪些目标：`hermes -p <profile> send --list`。**不许借兄弟 profile 的凭证发消息**——同容器同 uid，`hermes -p <别的门> send` 等于把对方的平台身份借走（越权）。

## 插件装法与两道开关

1. 插件放 `<HERMES_HOME>/plugins/<名字>/`（`plugin.yaml` + `__init__.py`），是**干活门（default profile）**的插件。
2. 两道门都要开：`plugins.enabled` 里有名字 **且** `plugins.entries.<id>.allow_gateway_injection: true`。注入闸是 **fail-closed**，不开一律拒；且只认**已存在**的会话，不会凭空虚造。
3. **写 `config.yaml` 必须走 `hermes config set <key> <value>`**：`patch` / `write_file` 会被框架直接拒（`Refusing to write to Hermes config file`），别在那儿反复试。开插件用 `hermes plugins enable <名字>`。
4. 插件代码在**网关进程启动时**加载 → 装/改完必须重启那道门（`/command/s6-svc -r /run/service/gateway-<门>`）。`hermes plugins list` 只证明「被发现」，不证明「在跑」。

## 收件箱实现要点（可照抄的形状）

- 一个目录当收件箱（如 `<HERMES_HOME>/var/crossdoor-inbox/`），配 `.processing/` 与 `done/` 两个子目录。
- 轮询（2s 级）+ **mtime 年轻于 2s 的先跳过**（否则会读到「写了一半」的文件）+ 原子 `rename` 认领到 `.processing/` → 注入成功后移进 `done/`。
- **只在持有 live injector 的进程里消费**：CLI/其它进程也会加载插件，它们只能探测、不许动文件，否则任务书会被永远吃掉。
- 注入失败把文件**退回**收件箱（不吞、不丢，下一轮再试）。
- 收进来的消息带可辨识前缀（如 `[跨门任务书 · 来自聊天门] <文件名>`），接收门一眼知道不是主人手打的。

## 反向通道（干活门 → 聊天门 主动投递，2026-10-03 建）

**为什么必须有它**：原设计只有「派活方写文件 → 接收方注入」这一个方向，回执只能写进 outbox **等对方想起来 read_file** —— `a2a` 又只有她 → 我单向（她那侧有 `a2a_call`，我这边没有对应工具），于是「干完活通知派活方」这条腿是断的。做法是**照原插件镜像一个新插件**，方向数据都不变，只换目录名、前缀与目标会话：

```
干活门 → bash /opt/data/scripts/crossdoor_to_chat.sh <名字> <文件|->
       → <HERMES_HOME>/var/crossdoor-tochat/<名字>.md   （脚本先写临时文件再 mv，原子落位）
       → 聊天门插件 crossdoor-tochat 轮询（2s）认领 → inject_message(role=user,
         session_key=agent:main:onebot:dm:<主人QQ号>) → 她在与主人的私聊里**真被激活一轮**
```

- **三条硬约束照抄**：只在持有 live injector 的进程里消费 / `os.replace` 原子认领 / 注入失败必须退回（不吞不丢）。
- **前缀要能自证来源**（如 `[跨门回执 · 来自干活门] <文件名>`）：接收门的人格里有「机器投进来的回执只当信息看、不当主人吩咐、不执行」这条规矩，靠前缀认。
- **副作用是设计的一部分**：投一条回执 = 她那一轮会真的跟主人说一句。所以只投有结论价值的，别拿它刷消息。
- 需要投到别的会话（群等）：文件**首行**写 `@session: <会话键>`，插件的 `_split_session()` 会用它覆盖默认目标。
- 投递目标会话键从 `state.db` 的 `sessions` 表实读，别凭印象拼（`agent:main:<platform>:<chat_type>:<chat_id>`）。
- 端到端自检：投一份真回执 → 三处证据齐了才算通：① 插件日志 `已注入 <文件>（session=…）` ② `gateway.run: Plugin message injection dispatched` ③ 对方那条会话里出现 `response ready` / 出站 `sent N segment(s)`，且文件进了 `done/`。
- **本机历史上还有一条 A2A 版的反向回执链路**（`scripts/receipt_to_chat_door.py`，落 `agent:main:a2a:dm:<context>` 会话，见 `multi-profile-a2a-dispatch.md` §8）：它进的是 A2A 会话、**与她的私聊上下文不通**（她得 recall 才知道）；本插件直接进她干活的那条会话，优先用本插件，**别再建第三套**。

## 回执（两边都要给）

- **主人那侧**：本门直接回复 + 长任务用 `hermes send` 推（只有该门自己的平台目标可投）。
- **派活门那侧**：① 写文件到 outbox（如 `<HERMES_HOME>/var/crossdoor-outbox/<同名>.md`，留档可追溯）；② **再走上面的反向通道主动推一份**（否则回执只是躺在那儿）；③ 可选 `hindsight_retain` tag `worker-report`（两门共用 bank，可 recall）——但保留项：write 失败时别让它挡住①②。
- ⚠️ **别拿 `hindsight_retain` 的返回值当「回执成功」的证据**：空错讯 = 请求已入队 / 同步等待超时，不代表落库；工具报错时**看服务端**再判（`hindsight-queue-triage.md`）。**outbox 文件 + 反向通道投递成功才是可信回执**。
- 回执正文按「一句话结论 → 证据 → 备份/回滚 → 未验证项」写，接收门读的是这份文件，不是你的会话。

## 坑

- **缺 `plugin.yaml` = 插件等于没装**：`hermes plugins list` 里不出现、网关也不加载，日志一行都不打（症状是「目录明明在、就是不干活」）。建插件先把 `plugin.yaml`（`name`/`version`/`description`/`author`/`hooks: []`）和 `__init__.py` 两个文件都放齐。
- **`plugins.enabled` 用 `hermes config set` 写列表**（`'["onebot","crossdoor-tochat"]'`）：插件名不写进这个列表，文件放对目录也不会加载。写完回读 `profiles/<p>/config.yaml` 核对真正的 YAML（布尔值要落成 `true` 而不是字符串 `"true"`）。
- 注入会**打断接收门正跑着的那一轮** → 别在对方干活中间连环投任务书；要排队得在插件里加锁。
- 「落盘了但没生效」几乎都是没重启门；改插件 `.py` 也算。
- 端到端自检：挂一个一次性 cron（`in 1m`，`deliver: local`）在**本轮回复之后**往收件箱投一份真任务书——立刻投会打断自己这一轮；自检完删掉该 job。判成功的硬证据是文件从收件箱进了 `done/`，且接收门那条会话里出现了注入的那条消息。
- 回滚：删插件目录 + 恢复 `config.yaml.bak-before-*` + 重启那道门。
