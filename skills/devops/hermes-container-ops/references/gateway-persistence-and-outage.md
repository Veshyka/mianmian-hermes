# Gateway 持久化与故障复盘（2026-08-24 QQ 掉线事件）

## 核心结论：gateway 持久化 = 让 s6 托管

- gateway 崩溃后 Hermes 的重试机制会**自动注册 `gateway-default` s6 服务槽**（`/run/service/gateway-default`，root 监督）——注册成功后 gateway 由 s6 托管：崩溃自动重启。**持久化根治，无需手动拉起、无需改 compose（运行时层面）。**
- ⚠️ **容器重启自愈仅在 gateway 模式容器成立**：官方文档说 02-reconcile-profiles 读 `$HERMES_HOME/profiles/<name>/gateway_state.json` 重建 s6 槽并自动拉起；但**dashboard 容器（CMD=`hermes dashboard`，即本环境）跳过 reconcile**（hermes_cli/container_boot.py:585-596「A dashboard-only container never spawns or supervises per-profile gateways…skip reconciliation in the dashboard container」）→ **容器重启后 gateway 不会自动拉起，需手动恢复**：`hermes gateway run`（自动注册 s6 槽）或跑 `/opt/data/scripts/gateway_s6_migrate.sh`。注意官方路径是 profiles/<name>/ 下，本环境实测根 $HERMES_HOME 的 gateway_state.json 也被 gateway 读写。
- 验证：`ps -o ppid -p <gateway_pid>` → 父进程是 `s6-supervise gateway-default`；`s6-svstat /run/service/gateway-default`（容器内用绝对路径 `/package/admin/s6/command/s6-svstat`）。
- `gateway_state.json` = `{"gateway_state":"running"}`：s6 槽重建的依据（/run/service 是 tmpfs，重启后由 reconciler 重建——dashboard 容器跳过时需手动触发）。
- **别手动 `--no-supervise` 长期跑**：会话级 background 进程会被 Hermes 进程管理回收（SIGKILL 137），QQ/cron 随之下线。`--no-supervise --force` 只用于绕过 s6 槽未注册时的临时启动/诊断。

## 故障复盘（13:40-13:50 QQ 掉线）

现象：QQ 掉线，gateway 每 70-90s 自动重试启动但反复失败（日志 `Refusing to start: qqbot has dm_policy/group_policy set to 'open' but neither GATEWAY_ALLOW_ALL_USERS nor QQ_ALLOW_ALL_USERS is enabled`）。

根因（两个叠加）：
1. 手动 `--no-supervise` 的 gateway 是会话级进程，被回收（SIGKILL 137）
2. **`.env` seed 默认值陷阱**：`.env` 里 seed 自带 `QQ_ALLOW_ALL_USERS=false`（约 509 行），append `true` 到文件尾会被并存/覆盖——实际生效值仍是 false → 自动重试的 gateway 全部被安全闸拒绝

修复：
- 策略改为**不依赖 .env opt-in 的表达**：`dm_policy: allowlist`（只主人）+ `group_policy: allowlist + group_allow_from: ["*"]`（群聊放开，QQ 平台层 @ 才触发所以不会刷屏）——任何启动路径都能过
- 重试机制自动注册 s6 槽 → 持久化根治

## 关键陷阱清单
1. **改 `.env` 别 append**：seed 默认值（QQ_ALLOW_ALL_USERS=false 等）会静默覆盖——用 `hermes config set` 或精确替换（sed 指定行号）
2. **open 策略必须 opt-in**（安全闸，特性不是 bug）：`dm_policy/group_policy: open` 时 gateway 拒启，除非 .env 有 `QQ_ALLOW_ALL_USERS=true`/`GATEWAY_ALLOW_ALL_USERS=true`——优先用 allowlist + `["*"]` 表达「全放开」以规避
3. **cron 表达式按进程本地时区（UTC）解释**：即使 config.yaml 配了 `timezone: Asia/Shanghai`，croniter 仍按容器 UTC 算 next_run——cron 表达式要按 UTC 换算（例：北京 8/11/14/17/20/23 = UTC `0 0,3,6,9,12,15 * * *`）。创建后核对 `next_run_at` 是否等于预期时刻
4. **gateway 自愈重试**：崩溃后 Hermes 自动重试启动（`/opt/data/logs/gateway-exit-diag.log` 记录每次 start/exit），配置错误时表现为反复失败循环——先看这个日志定位
5. **auxiliary 告警噪音**：未配 openrouter/nous 时日志刷 `PAID lane engaged`/`nous unavailable`——`hermes config set auxiliary.free_only true` 消除
