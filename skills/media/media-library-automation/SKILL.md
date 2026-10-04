---
name: media-library-automation
description: Use when 部署运维自动追番媒体库（AutoBangumi+qB+飞牛影视）或动漫下载链路出问题。
metadata:
  hermes:
    tags: [autobangumi, qbittorrent, anime, media-library, fnos, 飞牛影视, jellyfin, docker-compose]
    category: media
---

# 媒体库自动化（自动追番 + 下载 + 刮削 + 播放）

飞牛 NAS 上的动漫/影视档案库自动化链路：
**AutoBangumi（Mikan 订阅自动追番）→ qBittorrent（下载）→ 飞牛影视/Jellyfin（刮削海报墙）→ iPad/手机播放（浏览器或 BeeJoy，免签名）**

## 部署架构（2026-08-25 本机落地）

docker compose（`/vol2/docker/bangumi/compose.yaml`）：
- `qb`：superng6/qbittorrentee，端口 8989（WebUI），PUID=1000/PGID=1001（必须匹配宿主媒体目录 owner——本机 shy=1000:1001）
- `ab`：estrellaxd/auto_bangumi，端口 7892（WebUI），depends_on qb
- **下载目录**：`/vol1/1000/<USER>`（宿主）挂到容器 `/bangumi`——飞牛影视（trim.media）扫描该目录自动刮削，目录里有 `动漫/`、`电影/` 等子目录

## 部署步骤

```bash
# 1. 写 compose（见上方架构；PUID/PGID 用媒体目录 owner 的 uid/gid）
# 2. 启动
cd /vol2/docker/bangumi && docker compose up -d
# 3. qB 首次启动临时密码在日志里：
docker logs qb | grep -i password   # → "临时密码：KZXnacdqc"
# 4. qB 登录 + 设置下载路径 + 改固定密码（API）：
curl -c /tmp/qb.cookies -X POST http://127.0.0.1:8989/api/v2/auth/login -d "username=admin&password=<临时密码>"
curl -b /tmp/qb.cookies -X POST http://127.0.0.1:8989/api/v2/app/setPreferences \
  --data-urlencode 'json={"save_path":"/bangumi","temp_path_enabled":false,"web_ui_password":\"<SECRET>\"}'
# 5. 配置 AB 连 qB（见下节 AB API）
```

## ⚠️ AB 配置必须走 WebUI API（关键坑）

**直接改 config.json 会被 AB 覆盖**——AB 3.x 配置持久化在数据库（data.db），config.json 只是初始种子，重启时 AB 按版本迁移逻辑重建（日志可见 `3.1 -> 3.2 migration completed`）。改文件 → 重启 → 回到默认值（host 回 172.17.0.1:8080）。

**正确方式（API）**：
```bash
# 登录（OAuth2 password grant，必须 form 格式，不是 JSON！）
curl -c /tmp/ab.cookies -X POST http://127.0.0.1:7892/api/v1/auth/login \
  -d "grant_type=password&username=admin&password=\"<SECRET>\"   # 响应 {"authenticated":true}，token 在 cookie
# 读配置
curl -b /tmp/ab.cookies http://127.0.0.1:7892/api/v1/config/get
# 更新 downloader（qb 容器名互访用 qb:8989；path = qB 的 save_path = /bangumi）
curl -b /tmp/ab.cookies -X PATCH http://127.0.0.1:7892/api/v1/config/update \
  -H "Content-Type: application/json" \
  -d '{"downloader":{"type":"qbittorrent","host":"qb:8989","username":"admin","password":\"<SECRET>\","path":"/bangumi","ssl":false}}'
# 检查连接
curl -b /tmp/ab.cookies http://127.0.0.1:7892/api/v1/check/downloader   # true = 通
```
改完**重启 ab 容器**让连接器用新配置（否则日志仍报 Cannot connect——API check 可能 true 但连接器缓存旧值）。

## 验证清单

- `docker ps`：ab + qb 都 Up（ab health: starting → healthy）
- `curl 7892` / `curl 8989` 都 200
- AB 日志无 `Cannot connect to qBittorrent Server`
- 下载目录可写：`touch /vol1/1000/<USER> && rm ...`
- 飞牛影视（trim.media）已配置扫描该目录

## 日常使用

- 订阅番剧：`http://<NAS>:7892`（admin/adminadmin）→ Mikan（蜜柑计划）订阅
- 新番自动：Mikan RSS → AB 筛选 → qB 下载 → AB 重命名（Bangumi 规范目录）→ 飞牛影视刮削
- 播放：飞牛影视 App/浏览器（Tailscale 远程）；**BeeJoy**（App Store 外区免费，支持 fnOS 飞牛影视/Emby/Jellyfin——免签名）
- 弹幕：飞牛影视/Jellyfin 都没有——主人已知情接受（本地档案库优先）

## 已知局限

- AB 依赖 Mikan 上有的番剧（字幕组 RSS 聚合）；冷门番可能没源
- 国内 BT 环境：部分资源需要公网种子/PT；下载速度取决于做种
- qB 不建议直连公网管理端口（Tailscale 内网访问即可）
