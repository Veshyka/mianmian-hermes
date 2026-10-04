---
name: hermes-persistence-check
description: Use when 更新/重建 Hermes 前做固化自检（易失文件、常驻进程、自启项）。
version: 1.0.0
metadata:
  hermes:
    tags: [hermes, persistence, cron, 固化, 自检, fnos]
---

# Hermes 更新前的「固化」自检

主人要给 Hermes 更新/重启/重建时，先跑一遍这个自检——把还躺在易失位置、或没有守护者的东西捡回来，否则更新完就断。

## ⚠️ 镜像层源码补丁清单（重建后必须重打，静默失效）

`/opt/hermes/**`（Hermes 自身代码）在**容器镜像层**：重建/换镜像即消失，且退化是**静默的**——
功能没了不报错，只有用到才发现。本机现有补丁（每条的脚本都幂等、可重复跑）：

| 补丁 | 症状 | 重放脚本 |
|---|---|---|
| hindsight 插件 `_tool_retain` 忘了带 `retain_async` | `hindsight_retain` 返回 **"Failed to store memory: "**（错误信息为空）；本机抽取单批 20+ 分钟远超 180s 超时 | `scripts/hermes_patch_hindsight_retain_async.sh`（宿主侧 `docker exec hermes bash <脚本>`，文件是 root 所有，容器内 hermes 用户写不进去） |

判据（重建后自检用）：`grep -c "retain_async=self._retain_async" /opt/hermes/plugins/memory/hindsight/__init__.py` ≥ 1。
`HERMES_WRITE_SAFE_ROOT=/opt/data` 让 agent 的写工具碰不到 `/opt/hermes`，**必须走宿主 `docker exec`**。

## 核心三问

对每一个「今天动过的东西」，问三句：

1. **它现在放在哪？** 位置决定活多久（见下面的分层表）。
2. **它活到什么时候？** 文件（持久）≠ 进程（易失）。
3. **它死了之后谁把它拉起来？** 没有守护者 = 迟早断，且断了没人报错。

> 最容易漏的是第 3 句。脚本落在持久目录就觉得“已固化”是错觉——进程一死，能力就没了。

## 持久性分层（本机实测事实，2026-09-16）

| 位置 | 容器重建后 | 依据 |
|---|---|---|
| `/opt/data`（含 scripts/skills/memories/cron） | ✅ 活 | `findmnt -T /opt/data` → `btrfs ... subvol=/1000`（= 宿主 `/vol1/1000/<USER>`） |
| 容器 `/`（含 `/etc`、`/usr/local`） | ❌ 丢 | overlay，`upperdir=/vol2/docker/overlay2/...` |
| 容器 `/tmp` | ❌ 丢 | 容器一重建就没 |
| 宿主 `/vol1/1000/<USER>` | ✅ 活 | 宿主 btrfs 卷 |
| 宿主 `/tmp` | ❌ 丢 | 宿主重启就清 |

## 自检清单（照抄）

```sh
# 1) 确认 /opt/data 是挂载卷（不是可写层）
findmnt -T /opt/data

# 2) 今天动过的文件全扫（滤掉 cache/venv 噪声）
find /opt/data /tmp -newermt "$(date '+%Y-%m-%d') 00:00" -type f \
  -not -path '*/cache/*' -not -path '*/.kywork/venv/*' 2>/dev/null | head -60

# 3) 关键载体 mtime 是否今天（skill / MEMORY / 纠正记录）
ls -la --time-style=full-iso /opt/data/skills/*/*/SKILL.md /opt/data/memories/MEMORY.md /opt/data/memory/corrections.md

# 4) 容器里的常驻进程：谁是 Hermes 的子进程（子进程重启就没）
ps -eo pid,ppid,args | grep -E 'cdp_relay|python3 scripts|socat' | grep -v grep
#   ppid=1 → 已被 setsid 抽离，能活过 Hermes 重启
#   ppid=其他 → 那个父进程死它就死

# 5) 容器里有哪些自启注册（本机现状：没有 crontab，s6 槽只有 dashboard/main-hermes/user/user2）
crontab -l 2>&1 ; ls /etc/s6-overlay/s6-rc.d/ ; ls /etc/cron.d/

# 6) 宿主侧自启与备份
SSH '<host>' 'crontab -l; ls -la /vol1/1000/<USER>'
```

## 固化手段（按优先级）

1. **文件** → 放 `/opt/data/**`（持久）或宿主 `/vol1/1000/<USER>`。绝不能只放 `/tmp`。
2. **常驻进程的守护者** → 首选 Hermes cron 的 **watchdog 模式**：
   - 脚本放 `/opt/data/scripts/xxx.sh`（Hermes home 就是 `/opt/data`，所以 cron 的 `script` 参数只写文件名）
   - `cronjob_manage(action='create', no_agent=True, script='xxx.sh', schedule='every 5m', deliver='local')`
   - 脚本约定：**没异常就零输出**（零输出 = 什么都不发）；只有真拉起/失败才 echo 一行
   - `deliver='local'` 避免它往聊天里刷消息
   - 同一个东西**只建一个** watchdog，别重复建（先 `action='list'` 看）
   - 实例：`/opt/data/scripts/ensure_cdp_relay.sh` + job `cdp-relay-watchdog`
3. **宿主侧常驻** → 宿主 `crontab @reboot`；改前先 `crontab -l > .../crontab.bak.<时间戳>`。
4. **不要**在容器里自建 s6 槽（容易把启动流程搞坏）；容器也没有 `crontab` 命令。

## 容器环境事实（别再猜）

- 容器里**没有 `ss`，也没有 `netstat`**——探端口一律用 `python3 -c "import socket;..."`，否则判断会静默失真。
- 容器里**没有 `crontab`**。
- 宿主的 docker socket 对用户 棉棉 **不可读**（`docker ps` 报 permission denied，`docker --version` 却能跑）。要看容器配置就读 `/vol2/docker/containers/<id>/config.v2.json`（可读，能拿到完整 Env）。
- 容器内 `terminal background=true` 起的进程**不是自启**，Hermes 一重启就没。

## 汇报格式

给主人报的时候分两块，别堆流水账：

- **已落盘的**：一行一个路径 + 它的守护者
- **还没固化的**：缺口 + 影响 + 打算怎么补

## 坑

- 把「文件在持久目录」当成「已固化」——进程没有守护者照样断。
- 用 `pkill -f "python3 /path/x.py"` 去重启：`-f` 会匹配承载它的 ssh 命令行自身，把自己那条会话杀掉（实测 SIGTERM）。改用端口探测判断，或先 `pgrep -f` 拿 PID 再杀。
- 扫文件时忘了排掉 `venv/`、`cache/`：会被几千行噪声淹掉，真东西看不见。
