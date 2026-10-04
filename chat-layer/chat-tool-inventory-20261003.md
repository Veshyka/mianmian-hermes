# 聊天门（profile `chat`）工具清单 — 2026-10-03 实测导出

> 来源：`hermes_cli.tools_config._get_platform_tools(config, platform)` 复算（platform_toolsets 生效值）
> + `toolsets.py::TOOLSETS` 展开 + `model_tools.get_tool_definitions()`（43 个 schema）取原始 description
> + a2a 5 个工具查自 `plugins/platforms/a2a/tools.py` 的注册表。
> 平台工具集是**每轮从盘上读**的（不是启动期），改 `platform_toolsets` 不用重启该门。

## 平台 → 工具集 → 工具

| 平台 | 工具集 | 工具数 |
|---|---|---|
| `onebot` · **私聊** | a2a, cronjob, file, memory, session_search, skills, **terminal**, vision, web | 20 |
| `onebot` · **群聊** | 同私聊，但**摘掉 terminal**（适配器层硬摘，不是提示词） | 18 |
| `api_server`（本机 8643，前端插件打） | a2a, file, memory, **terminal**, vision, web | 15 |
| `a2a`（本机 9900，干活门递回执入口） | a2a, cronjob, file, memory, session_search, skills, vision, web | 18 |
| `cli`（仅 `hermes -p chat` 命令行会话） | 宽面：terminal/browser/code_execution/delegation/todo/clarify/tts/image_gen… | 大量 |

日常只有前三个平台在跑；`cli` 面只在有人用命令行进这个 profile 时出现。

> **2026-10-03 变更**：主人拍板给聊天门私聊执行权（跑飞牛 NAS 的一两步操作，不为此派活）。
> 实现是**代码层分流**：适配器实现 Hermes 的 `toolsets_for_source(source)` 钩子
> （`gateway/run_turn.py::_resolve_enabled_toolsets_for_source`），
> **只有 `chat_type == "dm"` 返回 None**（= 用 config 原表，含 terminal），
> 其余（group/channel/supergroup/guild/认不出来的一律）返回「摘掉 terminal」的表 —— fail-closed。
> 配置层兜底：`approvals.deny` 一串 glob 命中即硬拒（连 `--yolo` 也拦）。

## 逐工具（一句话）

| 工具名 | 干什么 | 在哪些平台 |
|---|---|---|
| `a2a_call` | **派活**：把一段自然语言任务发给远端 A2A agent（干活的在 `a2a_agents.work` = 127.0.0.1:9901），拿回它的答复；带 `context_id` 可续多轮 | onebot / a2a / api_server / cli |
| `a2a_discover` | 先看对方的 Agent Card（它会什么、有什么技能） | 同上 |
| `a2a_list` | 列出已配置的 peer、历史 A2A 会话、统计 | 同上 |
| `a2a_history` | 按 `context_id` 翻回某次 A2A 对话的完整记录（重启/压缩后仍在） | 同上 |
| `a2a_orchestrate` | 按 capability 把一件事群发给多个 peer（all / first / best 聚合） | 同上 |
| `cronjob_manage` | 建/查/改/删/手动触发定时任务 —— **主动开口唯一的路**（cron 投递到她自己的 QQ 通道） | onebot / a2a / cli |
| `read_file` | 读文件（带行号分页），能读整个 `/opt/data` | onebot / a2a / api_server / cli |
| `write_file` | 写/覆盖文件（受保护指令文件除外） | 同上 |
| `patch` | 精确查找替换改文件（9 种模糊匹配策略） | 同上 |
| `search_files` | 搜文件内容 / 按名找文件（她的 grep + ls） | 同上 |
| `memory` | 写长期记忆（工具版；另有 profile 级自动 recall/retain） | 同上 |
| `session_search` | 检索 / 翻阅自己的历史会话（FTS5，可整段读、可定位锚点） | onebot / a2a / cli |
| `skills_list` | 列出技能（名字 + 描述） | onebot / a2a / cli |
| `skill_view` | 打开一个技能，拿里面的步骤/脚本/模板 | onebot / a2a / cli |
| `skill_manage` | 新建/修改/删除技能（会沉淀成她的流程记忆） | onebot / a2a / cli |
| `vision_analyze` | 看图（主人发的图片走这条） | onebot / a2a / api_server / cli |
| `web_search` | 搜网（返回标题/URL/摘要） | onebot / a2a / api_server / cli |
| `web_extract` | 抓网页正文/PDF，转 markdown | 同上 |
| `terminal` | **跑命令（只在私聊）**：主要是 `python3 /opt/data/scripts/trim_cli.py …` 这类一两步的 NAS 操作，和 `df -h`/`date`/`docker ps` 这类只读看一眼 | onebot(私聊) / api_server / cli |
| `process_manage` | 管理后台进程（随 `terminal` 一起来） | 同上 |

## 硬兜底：`approvals.deny`（2026-10-03 加，系统层）

命中即拒，连 `--yolo` / `mode=off` 也拦（`tools/approval_floors.py::_match_user_deny_rule`，
大小写不敏感，还会先做去混淆变形再匹配）：

`*rm -rf*` `*rm -fr*` `*sudo *` `*mkfs*` `*fdisk*` `*parted*` `*dd if=*` `*docker rm*` `*docker rmi*`
`*docker volume rm*` `*docker system prune*` `*shutdown*` `*reboot*` `*poweroff*` `*passwd*` `*userdel*`
`*crontab -r*` `*chmod -R 777*` `*> /dev/sd*` `*askpass*` `*/.env*` `*secrets/*`

实测（2026-10-03）：`trim_cli.py +status / docker container ls / file ls` → 放行；
`rm -rf` / `sudo …` / `docker rm` / `mkfs` / `cat …askpass.sh` / `cat …/.env` / `ls secrets/` → 拒。
其余没有进 deny 的危险命令仍走默认 `mode: smart`（guardian 判断 + 给她推审批，300s 超时 = 拒）。

## 她"没有"的能力（写边界时按这个来）

| 没有 | 后果 |
|---|---|
| `execute_code` | 不能跑 Python 代码（有 `terminal` 就够跑封装脚本了） |
| `delegate_task` | 不能派子代理，重活只能 `a2a_call` 找干活门 |
| `browser` | 不能点网页、不能登录操作，只有只读抓取（web_search / web_extract） |
| `clarify` | 不能弹确认框，要问就直接说话问 |
| 发消息类工具 | **不能自己主动推 QQ**：回复由适配器发出；主动开口必须靠 `cronjob_manage` 定时投递，或干活门经 A2A 叫她回一轮 |
| 群管理类工具 | 禁言/撤回/欢迎/入群审批/关键词都是适配器代码自己做的，她调不了 |
| onebot 插件自身工具 | `grep register_tool` 在 `profiles/chat/plugins/onebot/*.py` = **0 命中**（她那个 `toolsets_for_source` 是网关钩子，不是工具），它不注册任何工具集 |
| 群聊里的 `terminal` / `process_manage` | 适配器层硬摘 —— 不是提示词警告，是那一轮根本没这两个工具 |

## 写提示词时的三条结论

1. 她的**系统级能力 = `file`（读写 `/opt/data` 全树）+ 私聊里的 `terminal`**。终端只该用来跑 `python3 /opt/data/scripts/trim_cli.py …`（飞牛 NAS）和一眼只读的命令；装东西/动服务/删东西/改配置仍然派活。
2. 她的**唯一重活出口是 `a2a_call` → 干活门**（`a2a_agents.work`，超时 300s）。
3. 其余全是"用嘴"：`web_search`/`web_extract`/`vision_analyze` 只读情报，`memory`/`session_search`/`skills*` 管自己的记忆与技能，`cronjob_manage` 排主动开口。

⚠️ 已知的**没堵住的口子**（同 uid，权限挡不住）：`/opt/data/scripts/askpass.sh`、`/opt/data/.env`、`/opt/data/secrets/`
这些文件她**读得到**（`.env` 走 file 工具会被框架的 read-denied 名单挡住，但 `terminal` 能绕）。
现在只有两层：`approvals.deny` 里那三条 glob（命令层）+ SOUL 里的禁令（提示词层）。硬隔离要动宿主侧权限，未做。
