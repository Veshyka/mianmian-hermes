# fnOS Ollama 托管启动（血泪记录 2026-08-25）

## 事故：手动 nohup 启动导致面板「打开就崩溃」

**症状**：飞牛应用中心面板点 Ollama（ai_installer）「打开」直接报崩溃；但服务实际在跑（11434 通）。

**根因**：测试时手动 `kill + nohup ollama serve` 启动——进程 PPID=1（init 收养），**绕过了 trim_app_center.service 托管**。应用中心视角认为应用未运行/异常 → 面板操作报错。

**判别方法**：
- `ps -o pid,ppid,cmd -p <ollama-pid>`：PPID=1 且非 systemd cgroup → 手动启动（异常态）
- 托管态：进程应在 `trim_app_center.service` 的 CGroup 下（`systemctl status trim_app_center.service` 可看）
- `trim-cli app list` 中 `ai_installer` 的 `control.isOpen`：false = 应用中心认为未运行

## 正确启停方式（trim-cli 走应用中心托管）

```bash
TRIM=/opt/data/skills/productivity/fnos-trim-cli-skill/scripts/trim-cli
ARGS="--host 172.17.0.1 --port 5666 --scheme ws --allow-insecure-ws"

$TRIM $ARGS app start ai_installer --yes    # Started app ai_installer
$TRIM $ARGS app stop ai_installer --yes
$TRIM $ARGS app list                        # 看 control.isOpen
```

- 托管启动后 **wrapper 同样生效**（日志确认 `library=CUDA compute=8.6`）——CUDA 模式不丢
- `app start` 返回 Started 后，`isOpen` 状态更新有延迟（可能仍显示 false 一段时间）；以 11434 监听为准
- 容器内 curl 宿主 Ollama 用 `172.17.0.1:11434`（容器内 127.0.0.1 是自己的，不是宿主的）

## 规则

- **启停 Ollama 一律走 trim-cli / 面板，不手动 kill + nohup**——托管状态一致是硬要求
- ai_manager（/usr/trim/bin/ai_manager）是 ELF 二进制，子命令不文档化，别试；用 trim-cli 的 app 命令
- 验证服务恢复：`curl http://172.17.0.1:11434/api/version` + `api/tags`（模型列表）
