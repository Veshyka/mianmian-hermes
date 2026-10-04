# Gateway 循环重启排障复盘（2026-08-24，每 ~70 秒 SIGTERM 循环）

## 症状
- gateway 每 63-71 秒被 SIGTERM 重启一次（日志 `Received SIGTERM` 计数持续增长），QQ 每 ~70 秒断线重连
- 表面像崩溃，实际是**有监督者周期性 kill + 拉起**
- 期间 gateway 功能可用（消息能收发），只是间歇断线——**「不崩溃但反复重启」的排障比全挂更难定位**

## 排障路径（按有效性排序）
1. **确认周期**：`grep -c 'Received SIGTERM' /opt/data/logs/gateway.log` 计数增长 + 相邻 SIGTERM 间隔稳定（~70s）→ 有固定周期的外部重启，不是随机崩溃
2. **看 SIGTERM 上下文**：`Shutdown context: signal=SIGTERM parent_name=s6-supervise` ——注意 **parent_cmdline 只是「父进程是谁」，不代表 SIGTERM 来自它**（--replace 实例互 kill 时也一样显示）
3. **找谁在拉 gateway**：`ps -o ppid -p <gateway_pid>` → 父进程不是 s6-supervise 时，就是另一个 spawn 源（当时抓到 ppid 203=dashboard）
4. **监控 s6-svc**：轮询 /proc 抓 `s6-svc` 进程（0.02s）——**抓到 0 次** → 不是 s6-svc 命令，是直接 spawn 或别的机制
5. **关键转折——查 s6 槽目录**：`ls /run/service/` 发现 **`gateway-migrate` 槽**！`cat /run/service/gateway-migrate/run` → 跑的是**自建脚本** `/opt/data/scripts/gateway_s6_migrate.sh`（one-shot 迁移脚本，`MIGRATE_DELAY=45`）
6. **根因确认**：迁移脚本挂在 s6 槽上，**s6 把 one-shot 槽当 longrun 服务，脚本每次退出后被重新拉起** → 每 45s sleep + 执行 ≈ 70s 循环：kill 旧 gateway(SIGTERM) → 注册/拉起新槽 → 退出 → s6 再拉起脚本 → 循环
7. **修复**：`mv /run/service/gateway-migrate /run/service/gateway-migrate.bak`（注销循环源槽，s6-svscan 自动扫描注销）→ 清理残留（`rm -rf *.bak`，若 busy 稍后）→ kill 所有 gateway → s6 拉起唯一实例 → 观察 SIGTERM 计数停涨 + `s6-svstat` 稳定

## 根因本质（教训）
- **s6 服务槽没有「one-shot」语义**：`type` 缺省 = longrun，run 脚本退出后会被无限重启。**自建脚本挂 s6 槽 = 自动循环执行**（还带 kill/重建等副作用时就是灾难）
- 我 12:39 写迁移脚本时把它注册为 s6 槽（想 45s 后自动迁移），但没意识到 one-shot 会被反复执行——**上下文一长就忘了自己埋的雷**，排障时绕了很大圈子（怀疑 dashboard、怀疑 Hermes 自愈、怀疑 s6 配置）
- **排障纪律**：固定周期 + SIGTERM 来自外部 → 先枚举**所有** s6 槽和自建脚本（`ls /run/service/` + `ls /opt/data/scripts/`），再怀疑框架 bug。**自己的迁移/自动化脚本是头号嫌疑**（尤其会话中途写的）

## 关联陷阱
- **`pkill -f 'gateway run'` / `pgrep -f` 会匹配到执行命令的 bash 自身**（cmdline 含模式文本）→ 把自己杀了（命令被 SIGTERM 中断，exit -15）。**用正则字符类技巧**：`pgrep -f 'gatewa[y] run'`（方括号让模式文本不再匹配自身）
- `kill -9` 全杀后 s6 会**秒级**拉起新实例（服务 up 中）——要先 `s6-svc -d` 停槽或 mv 注销槽，再杀进程
- `s6-svstat` 不在 PATH：用绝对路径 `/package/admin/s6/command/s6-svstat`
- gateway 日志 `Received SIGTERM` 计数是判断「循环」的硬指标；`/opt/data/logs/gateway-exit-diag.log` 记录每次 start/exit（含 previous_unclean_exit、last_heartbeat）

## 验证
- `grep -c 'Received SIGTERM' gateway.log` 计数在观察期内**不增长**
- `s6-svstat /run/service/gateway-default` 显示 up 且 pid 长时间不变（远超 70s）
- QQ `Ready` 且不再断线
