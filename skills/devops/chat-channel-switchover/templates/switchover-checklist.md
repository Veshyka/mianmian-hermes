# 通道切换清单（照抄，逐项打勾）

> 用一次就把本文件复制成当天的工作清单。铁律与理由见 SKILL.md。

## 0. 决策
- [ ] 这次切的是什么：协议指向 / 大脑 / both？
- [ ] 大脑要换 → 历史迁了吗（老框架会话库 → 新底座会话）？没迁就写进交付「如实未做」。
- [ ] 主人直接点头了吗（动在线聊天链路 = 要直接点头，不是自己顺手）？

## 1. 改前取证（全只读）
- [ ] `ss -ltnp | grep -E "<旧口>|<新口>"`：旧口有人听（回退保险在位）、新口空着
- [ ] 协议端配置现状：`network.websocketClients[0].url` / token 是否存在
- [ ] 新底座配置现状：`plugins.enabled`、`platforms.<name>`
- [ ] 备份：新底座 `config.yaml` + `.env`；协议端 `onebot11_<qq>.json`；落一个带时间戳的目录

## 2. 改（只改指向）
- [ ] 协议端 url 改到新口；`name` 顺手改名；token 与其余字段原样
- [ ] token 程序化搬进新底座 secret（**不回显**）
- [ ] 新底座：`plugins.enabled: [<name>]` + `platforms.<name>.enabled: true` + `extra{...}`
  - [ ] `read_only` 想发就显式 `false`（安全默认是 true）
  - [ ] 私聊/群聊边界显式配（allowlist / group_enabled）
- [ ] 重启顺序：先起新底座（监听起来）→ 再 `docker restart <协议端>`

## 3. 验证三连
- [ ] 新底座日志：`reverse-WS listener on <host>:<port>`
- [ ] 新底座日志：`client connected from <ip>`
- [ ] 真发真收：`inbound message` → `response ready ... session=...` → `sent N segment(s)`，`consecutive_failures=0`
- [ ] 自检脚本用**带依赖的 python**（`/opt/hermes/.venv/bin/python doctor.py`）→ exit 0
- [ ] 协议端侧：反向 WS 目标 = 新口
- [ ] 全局体检：`python3 /opt/data/scripts/health_all.py` 全绿

## 4. 告警与自检随迁
- [ ] 告警主通道**不在**被切走的一方（旧侧端口打的告警必然失败）
- [ ] 告警分级：能自愈的不出声、同异常签名退避
- [ ] **故意失败一次**，确认告警真到达人（判据：投递台账/主人引用回）
- [ ] 体检项含参数漂移：防抖窗口、白名单项数、补丁在位、常驻开关
- [ ] 旧底座的「平台未连接/平台类 ERROR」按**退役待命**降档（否则 30 分钟一误报）

## 5. 收尾
- [ ] 一句话回退写进报告与 changelog（改回哪个字段 → 重启哪个容器）
- [ ] 备份路径、验证判据、未做项（如历史未迁）如实登记
- [ ] 旧底座容器**保留**，只 stop 不删
- [ ] 沉淀：新坑写进对应技能的 references，别只写在当天报告里
