# Ollama 应用中心 APP_CRASH 循环修复实录（2026-08-25）

## 症状

- 主人反馈「Ollama 疯狂崩溃」
- journalctl 可见 `TRIMEVENT ... APP_NAME=ai_installer ... APP_STARTED` 后 7-40 秒紧跟 `APP_CRASH`，循环
- 面板点「打开」报崩溃；但 `curl 127.0.0.1:11434/api/version` **一直通**（Ollama API 没坏，坏的是应用中心视角）
- ai_installer 日志：`Error: listen tcp 127.0.0.1:11434: bind: address already in use` + `[Errno 98] bind ('0.0.0.0', 11436): address already in use`

## 根因

**ai_installer = Ollama + Open WebUI 捆绑**（应用中心把两个服务当一个应用托管）。

手动 `kill + nohup` 或 trim-cli 启动的 ollama 进程是**孤儿进程**：
- PPID=1（被 init 收养，不在 trim_app_center.service 的 CGroup 下）
- 应用中心用自己的托管状态判断「没在跑」→ 再拉起新实例 → **新实例 bind 11434/11436 端口冲突** → 退出 → APP_CRASH → 循环
- open-webui 的旧实例（python3.12）同样残留抢 11436

## 修复（干净托管启动）

```bash
# 1. 停应用中心（可选，端口释放为主）
trim-cli --host 172.17.0.1 --port 5666 --scheme ws --allow-insecure-ws app stop ai_installer --yes
# 2. 彻底杀残留（注意：要连 open-webui 一起杀！只杀 ollama 不解决 open-webui 抢 11436）
ps aux | grep -E "ollama\.real serve|ollama/lib/ollama/llama-server|open-webui" | grep -v grep | awk '{print $2}' | xargs -r kill
# 3. 确认端口释放
ss -tlnp | grep -E "11434|11436"   # 127.0.0.1:11434 必须消失（172.17.0.1:11434 是 hindsight-ollama-relay.service，正常保留）
# 4. 干净托管启动
trim-cli ... app start ai_installer --yes
# 5. 验证
#    - journalctl 无新 APP_CRASH
#    - ollama.real serve + open-webui 进程都在
#    - 11434 通、11436 open-webui HTTP 200
```

## 教训

1. **启停 Ollama 一律走 trim-cli / 面板，别手动 nohup**——手动绕过托管 = 孤儿进程 → 面板崩溃循环
2. **trim-cli app start 的进程也是 PPID=1**（应用中心 detach 启动）——关键是「干净状态」下启动（无残留抢端口），不会崩
3. 判断崩溃归属看 journalctl `TRIMEVENT`（APP_STARTED/APP_CRASH 事件）——比看进程更直接
4. 主人手动启动容器/服务会跟修复流程打架——修之前先跟主人打招呼
