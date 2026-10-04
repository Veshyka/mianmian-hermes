---
name: chat-channel-switchover
description: Use when 给聊天通道换接入方或回退。最小可逆改法、回退保险、告警随迁、验证三连。
version: 1.0.0
metadata:
  hermes:
    tags: [chat-layer, onebot, napcat, astrbot, switchover, rollback, im-channel, alerting]
---

# 聊天通道切换：最小可逆改法

**一句话**：切换只改「协议端往哪连」的**一个字段**；被切走的一方**一字不动**——它同时是回退保险和回退判据（回退口还在听，就说明回退必然成功）。

本机实例：2026-09-23，QQ 小号(<BOT_QQ>) 的 OneBot 事件从 **AstrBot 直连** 改到 **Hermes 聊天门自写的 `hermes_onebot` 适配器**。整体只有两处改动（NapCat 一个 url + 聊天门 config.yaml），全程可一句话回退。

## 一、四条铁律（先记这个，再看步骤）

1. **只改指向，不在被切走的一侧做任何准备动作**。被切走的一方（旧框架/旧通道）配置与容器保持零改动 → 它天然就是回退点，回退 = 把指向改回来 + 重启协议端。**为什么**：一旦在旧侧也动了配置/装了东西，回退就变成两处改动、要重验，保险失效。
2. **切换动作要能一句话说清、一条命令做完**。说不清就是方案太复杂，回去简化。判据：报告里能写出「回退（一句话）：把 X 改回 Y → 重启 Z」。
3. **告警/看门狗通道绝对不许依赖被切走的一方**（血泪，见第五节）。
4. **换大脑（响应的 agent）必须先迁历史**，否则切换当日失忆（见第七节）。只换协议指向、不换大脑则不涉及。

## 二、最小改法：协议端是「主动外连的客户端」

OneBot/NapCat 这类协议端是**反向 WS 客户端**：它自己往外连，只认配置里的 url。所以切换就是改这一个字段：

```
/app/napcat/config/onebot11_<qq>.json
  network.websocketClients[0].url:  ws://127.0.0.1:6199/ws  ->  ws://127.0.0.1:6700/ws
  （name 可顺手改成新底座的名字，token 与其余字段原样）
```

- **token 不重配**：从协议端自己的配置里程序化搬进新底座的 scoped secret（如 `profiles/<p>/.env` 的 `ONEBOT_ACCESS_TOKEN`），**全程不回显**。
- 改完 `docker restart napcat`（协议端重读配置才重连）。
- 新底座侧才是「配置 + 重启」那一半（本机：`plugins.enabled` 里写插件名 + `platforms.<name>.enabled: true` + `extra` 参数，再重启该门）。**两条闸门都要过**：只开平台不启用插件 = 插件根本不被 import，什么都不会发生。

> 另一形态（外部框架当「纯通道」转发给 Hermes，而不是直连）的源码级落点与得失见 `hermes-container-ops/references/im-channels-and-frontends.md`。

## 三、改前：备份清单 + 一句话回退预案

先备份这三样，路径写进切换记录：

1. **新底座那一侧**的配置（`profiles/<p>/config.yaml` + `.env`）
2. **协议端那一份 json**（`onebot11_<qq>.json`，另存一份带 `-pre-<动作>` 后缀）
3. **端口现状**：`ss -ltnp | grep -E "<旧口>|<新口>"`——确认旧口有人在听（= 回退保险在位）、新口空着

**回退一句话（模板）**：把 `<协议端配置文件>` 的 `network.websocketClients[0].url` 改回 `ws://127.0.0.1:<旧口>/ws` → `docker restart <协议端容器>`（旧侧一个字节没动，改回即恢复）。

## 四、验证三连（真日志，缺一不可）

只看「配置写对了」不算开通。必须按顺序拿到这三条**真日志**：

1. **新底座监听起来了**：`[<name>] reverse-WS listener on 127.0.0.1:<port> (auth=on, read_only=..., debounce=...)`
   —— 顺带确认参数（`read_only` 若还是 `true` = 只收不发，**默认值就是 true，要发必须显式关**）。
2. **协议端真连上来了**：`client connected from 127.0.0.1`
3. **真发真收**（不是模拟）：`inbound message: platform=<name> user=... chat=...` -> `response ready ... session=agent:main:<name>:dm:<qq>` -> `sent N segment(s) to <masked>`，且 `consecutive_failures=0`。

附加两条：

- **自检脚本必须用带依赖的 python 跑**：本机 `doctor.py` 要用 `/opt/hermes/.venv/bin/python`——系统 `python3` 没有 `yaml`，会把配置项**误判成「未启用」**（假红）。
- **协议端侧自查**：它的日志/WebUI 里确认反向 WS 目标 = 新口。

## 五、告警与自检必须随迁（本轮最贵的一课）

**教训**：告警通道当时挂在**被切走的那一侧**（旧框架插件里的一个 `127.0.0.1:8098` 接收端）。平台一搬走，旧框架收不到消息 → 那个接收端发消息必然失败，**线上实测 `HTTP 500`**，告警静默失效——正好在最需要告警的那天。

规则：

1. **告警的主通道用新底座自己的官方机制**（本机：Hermes 官方 cron 的 `--no-agent` script-only 模式，脚本 stdout 原样投递；空 stdout = 静默）。旧通道最多当副保险。
2. **告警分级**：能自愈的不出声；需要人干预才推送；同一异常**签名**（异常项名集合，不含会变的读数）要退避（本机 8 轮≈4h 才二次提醒）。全都告警 = 没有告警。
3. **验收必须包含「故意失败一次」**：真发一条告警并确认它到达主人（判据：投递台账里有 `delivered to ...` / 主人引用回了那条消息），不是只看脚本退出码。
4. **体检项要含「参数漂移」**：防抖窗口、白名单项数、补丁在位、常驻开关——切换后这些最容易在别处被改回旧值。
5. **日志纪律**：状态心跳只写计数、时间戳、错误摘要，**绝不含消息正文与凭据**。

## 六、切换后要同时「降档」的两件

- **旧底座降为「退役待命」而不是判成故障**：它仍在跑、只是收不到事件，会持续产生「平台未连接 / 平台类 ERROR」残响 → 体检脚本必须按「退役待命」降档，否则每 30 分钟误报一次。
- **旧底座的容器先别删**：既是参照样板，也是回退保险（本机 AstrBot 仅 `docker stop`，容器保留、随时可开）。

## 七、迁历史（换大脑的前置，别漏）

- 老框架的会话库（本机 AstrBot `data_v4.db`）里的对话**要迁进新底座的会话**，否则切换当日失忆。
- **记忆库三层不用迁**：共用同一个 Hindsight bank 时，长期记忆两头都看得到。
- **会话 id 一次定死**：只用 QQ 号/群号推导（私聊 `<平台>:dm:<qq>`、群聊按「会话+发送者」分桶），**禁带任何消息级随机量**（`message_id` 不进会话键）。验收：连发 100 句不失忆；重启适配器后会话仍在。
- 群聊/私聊边界要显式配（本机 `dm_policy: allowlist` + `allow_from` + `group_enabled: false` 表示本轮只私聊）。

## 八、本机落点（照抄用）

- 运行手册（启用/回退/排障速查）：`/opt/data/chat-layer/plugin/hermes_onebot/RUNBOOK.md`
- 可行性结论（为什么升级不丢、两条闸门、会话键推导）：同目录 `FEASIBILITY.md`
- 方向清单（陪伴向特调、工具面闭集）：`/opt/data/chat-layer/CHAT-AGENT-DIRECTION.md`
- 当天踩坑与规则一页纸：`/opt/data/chat-layer/LESSONS-2026-09-23.md`
- 可抄的切换清单：本技能 `templates/switchover-checklist.md`
