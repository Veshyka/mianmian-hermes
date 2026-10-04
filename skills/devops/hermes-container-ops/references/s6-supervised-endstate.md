# Gateway s6 托管常态与安全重启（2026-08-25 实测）

## 当前端状态（已稳定）
- gateway 由 s6 槽 `gateway-default` 托管（`s6-svstat /run/service/gateway-default` → up + pid 长时间不变）
- 崩溃/被 kill 后 s6 **秒级自动重启**：`kill -TERM <pid>` → 新 pid ~2s 出现，会话无损恢复（4h+ 稳定验证）
- 迁移脚本：`/opt/data/scripts/gateway_s6_migrate.sh`（可重复跑：杀旧 → 注册/拉起 s6 槽 → 验证 → 失败回滚手动拉起）

## 安全重启语义（读 gateway/run.py 源码确认）
- **无 planned-stop marker 的 SIGTERM 不会把 gateway_state 写成 stopped**——信号触发关机保留 `running` 自启意图（issue #42675 语义），这是「容器重启后能自动拉起」的前提
- `hermes gateway restart` = s6-svc -t（SIGTERM + s6 自动重启），可安全用
- 有 planned-stop marker（`hermes gateway stop` 会先写 marker）→ 才持久化 stopped，下次 boot 不自启——符合操作者显式意图

## s6 命令与权限坑
- `s6-svstat` / `s6-svc` / `s6-svscanctl` **不在 PATH**（容器内）：绝对路径 `/package/admin/s6-2.15.0.0/command/`（或 `/package/admin/s6/command/` 旧路径）
- **hermes 用户对 root 创建的 supervise 目录 `s6-svc` 会 Permission denied（rc=111）**——注册的槽若由 hermes 创建则可控；root 创建的只能 root/s6 自己管（崩溃自愈不受影响，仅手动 s6-svc 受限）
- 02-reconcile-profiles 只 chown 了 /run/service 根 + svscan FIFO，**不 chown 每个服务的 supervise 目录**

## 验证
```bash
/package/admin/s6-2.15.0.0/command/s6-svstat /run/service/gateway-default   # up (pid N)
python3 -c "import json; d=json.load(open('/opt/data/gateway_state.json')); print(d['gateway_state'], {k:v['state'] for k,v in d['platforms'].items()})"
```

## 同容器多门（多 profile）识别与探活

**同一容器里跑多个 profile 的 gateway 是支持的常态**（如主门 + 聊天门：s6 槽 `gateway-default` 与 `gateway-chat`），不需要给每个门单起容器。有人问「是不是为了这个又新起了个 docker」时，先按下面这套现场核对再答，别按印象答。

- **「这个门用哪个 profile、怎么起的」的权威答案在槽目录的 `run` 脚本**：`cat /run/service/<槽名>/run`（例：`gateway-chat` 是 `exec s6-setuidgid hermes hermes -p chat gateway run --replace`）。起新容器之前先看这里——独立容器方案（`run-chat-door.sh`：`docker run --name hermes-chat`）可能只是**历史方案**，早被换掉了。
- **端口归属**：容器内没有 `ss`/`netstat`，用 `/proc/net/tcp` **和 `/proc/net/tcp6`** 拿十六进制端口（如 8643=0x21C3）的 inode，再扫 `/proc/*/fd` 里 `socket:[<inode>]` 反查 pid。**只查 v4 会把 v6-only 的监听判成「没起」。**
- **认门看 `/proc/<pid>/environ` 的 `HERMES_HOME`，不要看命令行**：`hermes -p chat gateway run` 经 venv 重执行后，`ps` 里只剩 `hermes gateway run --replace`，`-p chat` 不见了；同理 `ps | grep -E "gateway.*chat"` 也匹不上（flag 顺序是 `chat` 在 `gateway` 之前）→ 按命令行/按这种 grep 下结论，会把在跑的门判成没跑。
- **判「门在不在跑」= 端口有监听 + 有对应 pid**：`s6-supervise` 进程存在**不等于**被监督的 gateway 进程活着（s6 的 supervise 常驻，被监督进程可以已经退出）。日志 `/opt/data/logs/gateways/<name>/current`：**只有启动横幅、之后再无输出 = 起过但没在跑**。
