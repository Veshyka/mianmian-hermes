# 宿主面板与容器进程排查（2026-08-25 实测）

## 1. fnOS 面板连不上 docker：docker-proxy 重建

**症状**：fnOS Docker 界面报「获取容器日志失败：connect ECONNREFUSED /var/run/docker.sock」。

**根因**：fnOS 面板不直连 docker.sock，走 `tcp://docker-proxy:2375` 代理。docker-proxy 容器缺失（历史遗留：曾以为它无关紧要删了）→ ECONNREFUSED。docker 本体（dockerd unix socket）完全正常——**先 `docker ps` 验证再怀疑 docker daemon**。

**修复**（镜像已在宿主 `tecnativa/docker-socket-proxy:latest`）：
```bash
docker run -d --name docker-proxy --restart unless-stopped \
  -p 127.0.0.1:2375:2375 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e CONTAINERS=1 -e IMAGES=1 -e INFO=1 -e LOGS=1 -e NETWORKS=1 -e VOLUMES=1 -e POST=1 -e AUTH_CONFIG=1 \
  tecnativa/docker-socket-proxy:latest
# 验证：curl http://127.0.0.1:2375/version
```
绑 127.0.0.1 防暴露。面板恢复后正常。

## 2. Hindsight 容器进程归属判断（血的教训）

**症状/陷阱**：宿主 `ps aux` 里能看到 `shy 用户 /app/api/.venv/bin/hindsight-api`（父进程 `/app/start-all.sh`）——容易被误判为「OpenClaw 遗留宿主旧版」「冗余服务」而想关掉。

**真相**：**它就是 hindsight docker 容器内的进程**（容器 `--network host --privileged`）。飞牛 docker 的容器进程会出现在宿主 PID 空间（ps/ss 可见 PID），`/app` 是镜像内路径。8888/9999/5433 全是它监听。

**判断归属的可靠方法**：`docker stop <容器>` 后进程消失 = 属于该容器。**不要**只靠 ps 猜。

**后果**：2026-08-25 误判为冗余停了 hindsight 容器 → 8888 断线（Hermes 记忆入口）→ 记忆服务全挂 → 靠 `docker start hindsight` 恢复。**Hindsight 容器是记忆服务本体，绝不能停。**

## 3. 相关补充
- woc（WeChat on Cloud）面板 `192.168.123.110:36080`，API 端点 `/api/auth/login`、`/api/instances`、`/api/admin/*`（admin 权限）；实例容器 `woc-wx-*`（Linux 微信 + websocket API 3000 端口，登录态在 `/config/.xwechat`）。面板 `orphan-containers` 接口偶发 500 是面板自身 bug，不影响实例运行。
- 宿主内存紧张排查：`ps aux --sort=-rss` 看大头；Ollama llama-server RSS 高多为 mmap 可回收页（`available` 才是真实可用）；minicpm 空闲 5 分钟自动卸载（Ollama 默认 keep_alive）。
