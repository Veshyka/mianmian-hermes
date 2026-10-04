# 长任务「主动回报」机制（干活门 → 聊天门/主人）

日期：2026-09-23 · 执行：棉棉（干活门） · 派单：聊天门 AstrBot 代主人

## 要解决的问题

A2A 跨门派活是**一次性同步调用**（对端 300 秒超时）：干活门跑完长任务后不会自己回来，
派活方（猫猫）只能再派一趟问进度 —— 主人原话「不然我设计这个分工干啥」。

## 通道设计（三道，前一道不通自动退到下一道）

```
干活门跑完（成功/失败）
   │  scripts/run_and_report.sh（长任务包装器，自动回报）
   ▼
scripts/report_to_chat.sh  ──①主通道──►  AstrBot 插件 hermes_report POST /report (127.0.0.1:8098)
   │                                       └─► context.send_message → 主人 QQ 私聊
   │                                       └─► Hindsight retain（tag worker-report，猫猫 recall 得到）
   │                                       └─► plugin_data/hermes_report/reports.jsonl 留痕（幂等去重）
   ├──②兜底──►  宿主侧 OneBot 直发（onebot_msg.py，走 sudo 读 token，不复制凭证到容器）
   └──③最后一手──►  /opt/data/var/report-spool/<task_id>.json 落盘 + 日志，等人/等下次
```

## 组件清单

| 文件 | 位置 | 干什么 |
|---|---|---|
| `hermes_report/main.py` | 宿主 `chat-layer/plugin/hermes_report/` → 容器 `/AstrBot/data/plugins/hermes_report`（compose 挂载） | AstrBot 侧接收端：aiohttp 只绑 `127.0.0.1:8098`，收 POST 后发主人私聊 + 写记忆 + 留痕 |
| `.report_token` | 同目录（600） | 共享令牌，请求头 `X-Report-Token`；只管到人，不落日志 |
| `report_to_chat.sh` | `scripts/`（= 宿主 `chat-layer/../scripts`，容器内 `/opt/data/scripts`） | 干活门侧发送客户端：三通道 + `--verify` 送达核验 |
| `run_and_report.sh` | `scripts/` | 长任务包装器：`run_and_report.sh --title … -- <命令>`，跑完按退出码回报（失败带退出码 + 日志尾部） |
| `onebot_msg.py` | `scripts/`（在**宿主**执行） | OneBot 直发 + `get_friend_msg_history` 送达核验 |
| `verify_toolfix_residue.py` | `scripts/` | 测试记忆残留复核（本日另一件事用） |

## 接口契约

```
POST http://127.0.0.1:8098/report
Header: X-Report-Token: <chat-layer/plugin/hermes_report/.report_token>
Body:   {"task_id":"t-…","status":"ok|fail|partial","title":"一句话",
         "summary":"≤400 字结论","path":"/opt/data/reports/xxx.md","source":"worker-door"}
Resp:   {"ok":true,"sent":true,"duplicate":false,"umo":"aiocqhttp:FriendMessage:<qq>","text":"…"}
GET  /health → {"ok":true,"last_umo":"…","seen":N}
```

- **幂等**：同一个 `task_id` 只发一次（`duplicate:true` 直接返回，不再发第二条）。
- **会话解析**：`target_umo`（配置）→ 最近一次私聊 umo（插件自动记录到 `plugin_data/hermes_report/last_umo.txt`）→ `default_umo`（主人私聊）。
- **只绑回环**：`network_mode: host`，端口只在 127.0.0.1 上，容器外/局域网碰不到；再加令牌。
- **失败也回报**：`status=fail` 时正文带退出码与日志尾部（`run_and_report.sh` 生成）。

## 怎么用（干活门侧）

```bash
# ① 长任务（预计 >3 分钟）：用包装器，跑完自己回来
/opt/data/scripts/run_and_report.sh --title "整库重跑" --path /opt/data/reports/xxx.md -- <命令...>

# ② 已经跑完/手工回报
/opt/data/scripts/report_to_chat.sh --status ok --title "标题" --summary "结论" --path /opt/data/reports/xxx.md

# ③ 确认真的到了（不看发送返回值，去 NapCat 侧查历史）
bash /opt/data/scripts/fygo_ssh.sh "export SUDO_ASKPASS=/vol1/1000/<USER> \
  python3 /vol1/1000/<USER> recent <OWNER_QQ> 5"
```

## 验收实测（2026-09-23 08:14–08:19，真发真收）

| 用例 | 命令 | 结果 |
|---|---|---|
| 短任务冒烟 | `run_and_report.sh --task-id smoke-001448 --title "主动回报机制自测（短任务 8s）" -- bash -c 'sleep 8; echo smoke-ok'` | 插件回 `{"ok":true,"sent":true,"umo":"aiocqhttp:FriendMessage:<OWNER_QQ>"}`；NapCat 侧查到自发送 `message_id=49396052 time=08:14:57` 正文 `【干活门回执】✅ 主动回报机制自测（短任务 8s）…` |
| 失败回报 | `run_and_report.sh --task-id failtest-001508 --title "失败回报自测（命令退出码 3）" -- bash -c '…; exit 3'` | 回 `sent:true`，正文 `【干活门回执】❌ … 耗时 0s，命令退出码 3。失败原因（日志尾部）：…`；脚本自身退出码保持 3 |
| 长任务（>3 分钟） | `run_and_report.sh --task-id longtest-001505 --title "长任务自测（>3 分钟，跑完自动回报）" --path /opt/data/reports/selfcheck-longtask.md -- bash -c '…; sleep 210; …'`（后台跑） | 00:15:05 起跑 → 00:18:35 完工并**同一秒**发出回执；NapCat 侧查到 `message_id=1890224758` @08:18:35 正文 `【干活门回执】✅ 长任务自测（>3 分钟，跑完自动回报）耗时 210s，命令正常退出。长报告：/opt/data/reports/selfcheck-longtask.md`；报告文件含起止时间戳（00:15:05 / 00:18:35） |
| 幂等 | 同一 `task_id` 再发 | `duplicate:true`，未再发第二条 |

> 送达判据一律用 **NapCat `get_friend_msg_history` 里出现的 `message_sent_type=self` 记录 + message_id**，
> 不看 `context.send_message()` 的返回值 —— 那个只表示「找到了平台」，不保证到达。

## 已知边界

- **依赖 AstrBot 活着**：插件没起来时主通道死，自动退到 OneBot 直发；NapCat 也挂了才落盘。
- **`report-spool/` 只落不自动重发**：目前要人工/下一次调用时重发（没做定时重扫，避免和「别刷屏」冲突）。
- **回执进主人私聊**，不是进猫猫的对话上下文：猫猫要知道进度，靠 Hindsight 里 tag `worker-report` 那条
  （主人原话「hms 有纠正机制能看到你发的消息」——回执会 retain 进同一 bank，两个门都能 recall）。
- **端口 8098** 是新增占用，和现有端点无冲突（见 `ops-changelog/README.md` 端点表）。
