---
name: astrbot-chatgate-skills
description: Use when 给 AstrBot 聊天门加/改技能或验证加载。
---

# AstrBot 聊天门技能：加装与验证

聊天门 = AstrBot 容器 `astrbot`（代码 `/AstrBot/`，人格 `mianmian`，provider `deepseek/deepseek-flash`）。

## 机制（决定「要不要重启」）

- 技能目录 `<AstrBot>/data/skills/<技能名>/SKILL.md`（新目录默认 `active: true`，自动写入 `data/skills.json`）。
- **`SkillManager.list_skills()` 每轮请求都会调**（`astrbot/core/astr_main_agent.py` 的 `_ensure_persona_and_skills`：`list_skills(active_only=True, runtime=...)` + `build_skills_prompt()` 拼进 system prompt）→ **不用重启，下一条消息就生效**。
- 只把 `name` + `description`（YAML frontmatter）+ 绝对 `File:` 路径给模型；正文由模型自己读（skills prompt 第 3 条要求用 shell `cat <绝对路径>`）。
- persona 的 `skills` 字段（DB `data_v4.db` 表 `personas`）为 NULL = 不限白名单；设成 `[]` 会屏蔽全部技能。
- 读技能文件：`astrbot_file_read_tool(path="/AstrBot/data/skills/...")` 在允许根内（受限环境也允许读全局 skills 目录）；`astrbot_execute_shell` 的 `cat` 也通。

## 部署（宿主挂载点 `/vol1/1000/<USER>`）

本地打包 → base64 → 宿主解码 → `docker cp`（绕开宿主目录权限）：

```bash
# Hermes 侧
cd <stage> && tar czf /tmp/sk.tgz <skill-dirs...> && base64 -w0 /tmp/sk.tgz > /tmp/sk.b64
# 宿主（fygo_ssh.sh）
echo '<b64>' | base64 -d > /tmp/sk.tgz; mkdir -p /tmp/stage; tar xzf /tmp/sk.tgz -C /tmp/stage; sudo -A docker cp /tmp/stage/. astrbot:/AstrBot/data/skills/
```

长 base64 用 `execute_code` 调 `terminal()` 拼命令，别把 30KB+ 字符串写进对话。

## 验证（两层，缺一不算过）

```bash
sudo -A docker exec astrbot sh -c 'cd /AstrBot && python3 - <<"PYEOF"
from astrbot.core.skills.skill_manager import SkillManager, build_skills_prompt
sm=SkillManager(); sk=sm.list_skills(active_only=True, runtime="local")
print(len(sk)); [print(s.name, s.active, s.path, s.description) for s in sk]
print(build_skills_prompt(sk))
PYEOF'
```
坑：heredoc 体落在 `sh -c '...'` 单引号里，**只能出现双引号**（写单引号会 SyntaxError）。

## 改写方向（给聊天门，不是干活工位）

- 她有的工具：`recall/retain/reflect/list_memories` 等（MCP hindsight，bank 固定 `mianmian-history`，禁用 `sync_retain`）、`delegate_to_hermes`、`astrbot_execute_shell/python`、`astrbot_file_read/write/edit_tool`、`astrbot_grep_tool`、`future_task`、`send_message_to_user`。
- 她没有：`skill_view` `terminal` `browser_exec` `cronjob_manage` `delegate_task` `session_search` `read_file` `web_extract` `vision_analyze` → 搬技能时全部改写或删掉。
- 人格硬规矩：**每条短消息末尾 `※` 分段、整条 ≤150 字、禁 emoji、禁 AI 腔**；技能给的动作不能和它冲突。
- cron 心跳轮：**最终回复不会自动发给主人**（只写历史），必须自己调 `send_message_to_user`；`future_task` 的 `note` 要自包含（新会话看不到当前对话）。
- `data_v4.db` 时间戳是 **UTC**，+8 才是北京时间；`platform_message_history.user_id` = umo，`sender_id` = 发送者 QQ（<BOT_QQ> = 棉棉，<OWNER_QQ> = 主人）。
- 已装技能（2026-09-23）：`humanizer` `proactive-messaging` `life-decision-support` `online-dispute-rights-support`。
