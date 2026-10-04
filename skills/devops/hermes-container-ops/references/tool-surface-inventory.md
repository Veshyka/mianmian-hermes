# 列「某道门/某平台真实生效的工具面」

回答「她能调哪些工具」「边界该写在哪」时，**不要靠人格/技能里的描述、也不要靠别人给的清单**——
按配置算一遍。工具面决定边界，边界又决定提示词里能许诺什么。

## 取数配方（唯一可用的路）

`hermes tools list` 和 `hermes tools --summary` **都要求交互终端**（管道/非交互 subprocess 直接报
`requires an interactive terminal`），会话内取不到数。能跑的只有脚本 `scripts/dump_platform_tools.py`：

```bash
HERMES_HOME=/opt/data/profiles/chat /opt/hermes/.venv/bin/python \
  /opt/data/skills/devops/hermes-container-ops/scripts/dump_platform_tools.py onebot api_server a2a
```

它内部三步，缺一步就会报错或给出错的东西：

1. `hermes_cli.tools_config._get_platform_tools(cfg, platform)` —— 返回的是**工具集名**
   （`a2a` / `cronjob` / `file` / `memory` / `session_search` / `skills` / `vision` / `web`），
   **不是工具名**。看着像工具名，直接当结论报出去就错。
2. `toolsets.py::TOOLSETS` 展开 `tools` + `includes`（递归）→ 工具名。
3. 描述从 `model_tools.get_tool_definitions(quiet_mode=True)` 的全量 schema 字典里查。
   **插件工具集不在 TOOLSETS 里**（如 `a2a`）→ 读插件自己的注册
   （`plugins/platforms/a2a/tools.py` 的 `register_tool(toolset="a2a", …)`，共 5 个：
   `a2a_discover / a2a_call / a2a_list / a2a_history / a2a_orchestrate`）。

`platform_toolsets` 缺失那个平台的键 ≠ 该平台没工具 = 回落到平台默认（插件平台 = 全套 core tools）。

## 工具面 ≠ 适配器能力

平台适配器插件的**行为**（出站发消息、分段、群管理：欢迎/禁言/撤回/入群审批）**不是 agent 可调的工具**：
`grep -n "register_tool\|toolset=" profiles/<p>/plugins/<适配器>/*.py` 零命中 = 她手里没有这类工具，
那些能力是代码按配置自己做的。写边界/提示词时别把它当成「她有这个工具」。

同样地，**主动对平台说话**没有 agent 侧工具（见 `multi-profile-a2a-dispatch.md` §8）：
回复由适配器发出；要主动开口只能靠 `cronjob_manage` 排定时投递，或由对门经 A2A 叫起来一轮。

## 怎么用这份清单写提示词

先分三类，再写边界：

| 类别 | 工具 | 提示词里怎么写 |
|---|---|---|
| 系统级能力 | `read_file` / `write_file` / `patch` / `search_files` | 唯一能真动文件的口子 → 明确「什么时候才碰、碰哪里」 |
| 重活出口 | `a2a_call`（+ discover/list/history/orchestrate） | 多轮、要中途改方向、登录态操作一律转手派出去 |
| 用嘴的 | `web_search` / `web_extract` / `vision_analyze` / `memory` / `session_search` / `skills*` / `cronjob_manage` | 一步能做完的自己答；预约/提醒靠 cron |

清单里**没有的**才是边界的重点（别把「没有」写成「不做」）：末轮核对一遍缺 `terminal` / `execute_code` /
`delegate_task` / `browser` / `clarify` 这些是否确实不在列表里，再落笔。

导出物照例给宿主映射路径（`<映射>/chat-layer/<名>.md`），别报 `/opt/data/...`。

## 顺带：工具面是每轮读盘的

`platform_toolsets` 每轮从盘上重新读（不同于 `compression` 那类构建期读的设置）→
改她的工具面**不用重启该门**，下一轮就生效；改完照样回读配置 + 用脚本复算一次确认。

## 按「消息来源」分流：私聊给、群聊摘（代码层，比提示词硬）

`platform_toolsets` 是**按平台**配的，一个平台只有一份 → 「私聊有 `terminal`、群里没有」**配置层做不到**。
正解是让适配器实现网关每轮会问的钩子：

```python
def toolsets_for_source(self, source) -> list | None
```

- 返回**列表** = 替换该平台整套工具面；返回 `None` = 回落 `platform_toolsets.<platform>`。
- 网关每轮建 agent 前调用它 → 改完不用重启该门（除非 hook 自己缓存了配置）。
- **fail-closed 写法**：只对明确判定的来源给全表，其余（group / channel / supergroup / guild / 空值 / 认不出的 chat_type）一律返回**摘掉敏感工具集**的那份。别用黑名单式排除——新平台类型冒出来就漏。
- 这是**代码层**分流：模型看不到、提示词绕不过，比「人格里写群里别跑命令」硬得多。人设里仍要写清边界（写给人看的一致性与可维护性），但门闸在代码里。
- **验证**：① 单测钩子——用 stub 对象 unbound 调用，把 `dm / group / channel / supergroup / guild / 空 / thread` 全打一遍；② 再用 `dump_platform_tools.py` 对每个来源各复算一次面，核对差额=你摘掉的那几套（不是「看起来差不多」）。

## `approvals.deny`：命令层硬闸，以及怎么验证它真拦

要「写出来就拒」的命令（不可逆类：`rm -rf` / `sudo` / `mkfs` / `fdisk` / `dd if=` / `docker rm|rmi|volume rm|system prune` / `shutdown|reboot` / `passwd` / `chmod -R 777` / 凭据路径 glob）走 `approvals.deny`，**不要**指望 smart 审批：

- `approvals.deny` 是 glob 列表，**在 smart 模式与 `--yolo` 之下都生效**——性质是「硬拦」，不是「等审批」。放行项仍走默认 smart（guardian 判定 + 推审批，超时=拒）。
- 匹配实现 `tools/approval_floors.py::_match_user_deny_rule(command, cfg)`，可在会话内直接 import 调用做 A/B：`HERMES_HOME=<该 profile>` 起一个 python 进程，拿几条**放行**（真业务命令）与几条**拦截**（各条 glob 各来一发）分别跑一次，两边都符合预期才算配上。
- ⚠️ 它只匹配**命令文本**：子 shell、编码、写进脚本文件再执行都能绕过 → **不是隔离，是兜底**。别把它当安全边界向主人汇报。
- **同 uid 下凭据无法用文件权限隔离**（容器里就是同一个 hermes 用户）：能读就是能读。发现某 profile 读得到 `.env` / askpass / `secrets/` 时，可做的是「命令层黑名单 + 人格禁令」两层软堵，**要真隔离得动宿主侧属主/权限**——那属于要主人拍板的事，**先报告口子、别自己动**。

## 人设/技能里的工具名要对着清单核（假信息的高发区）

人设会过期：实测人设里白纸黑字写着「开网页：`browser_exec`」，而按配置算出来那道门**所有平台面都没有 browser**——她照着写的行为描述去调，只会失败或转述幻觉。规矩：

- 落笔/复核人设与技能时，把其中出现的**每个工具名**对着算出来的清单核一遍，不在清单里的改写或删掉。
- 技能里引用的 Hermes 专属工具名（`terminal` / `browser_exec` / `skill_view`…）必须换成该门**真实有的**等价物，否则技能是废的。
- 老话术也要复查：「你没有 shell」这类否定句会随着工具面变动而过期（今天没有 ≠ 明天没有）→ 改工具面时**同一批**把这类句子一起核。
