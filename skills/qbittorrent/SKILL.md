---
name: qbittorrent
version: 1.0.0
description: Manage torrents with qBittorrent. Use when the user asks to "list torrents", "add torrent", "pause torrent", "resume torrent", "delete torrent", "check download status", "torrent speed", "qBittorrent stats", or mentions qBittorrent/qbit torrent management.
---

# qBittorrent WebUI API

Manage torrents via qBittorrent's WebUI API (v4.1+).

## Setup

Config: `~/.clawdbot/credentials/qbittorrent/config.json`

```json
{
  "url": "http://localhost:8080",
  "username": "admin",
  "password": \"<SECRET>\"
}
```

## Quick Reference

### List Torrents

```bash
# All torrents
./scripts/qbit-api.sh list

# Filter by status
./scripts/qbit-api.sh list --filter downloading
./scripts/qbit-api.sh list --filter seeding
./scripts/qbit-api.sh list --filter paused

# Filter by category
./scripts/qbit-api.sh list --category movies
```

Filters: `all`, `downloading`, `seeding`, `completed`, `paused`, `active`, `inactive`, `stalled`, `errored`

### Get Torrent Info

```bash
./scripts/qbit-api.sh info <hash>
./scripts/qbit-api.sh files <hash>
./scripts/qbit-api.sh trackers <hash>
```

### Add Torrent

```bash
# By magnet or URL
./scripts/qbit-api.sh add "magnet:?xt=..." --category movies

# By file
./scripts/qbit-api.sh add-file /path/to/file.torrent --paused
```

### Control Torrents

```bash
./scripts/qbit-api.sh pause <hash>         # or "all"
./scripts/qbit-api.sh resume <hash>        # or "all"
./scripts/qbit-api.sh delete <hash>        # keep files
./scripts/qbit-api.sh delete <hash> --files  # delete files too
./scripts/qbit-api.sh recheck <hash>
```

### Categories & Tags

```bash
./scripts/qbit-api.sh categories
./scripts/qbit-api.sh tags
./scripts/qbit-api.sh set-category <hash> movies
./scripts/qbit-api.sh add-tags <hash> "important,archive"
```

### Transfer Info

```bash
./scripts/qbit-api.sh transfer   # global speed/stats
./scripts/qbit-api.sh speedlimit # current limits
./scripts/qbit-api.sh set-speedlimit --down 5M --up 1M
```

### App Info

```bash
./scripts/qbit-api.sh version
./scripts/qbit-api.sh preferences
```

## Response Format

Torrent object includes:
- `hash`, `name`, `state`, `progress`
- `dlspeed`, `upspeed`, `eta`
- `size`, `downloaded`, `uploaded`
- `category`, `tags`, `save_path`

States: `downloading`, `stalledDL`, `uploading`, `stalledUP`, `pausedDL`, `pausedUP`, `queuedDL`, `queuedUP`, `checkingDL`, `checkingUP`, `error`, `missingFiles`

## Pitfalls（实战血泪，2026-08-27）

### 1. 走代理时 DHT 会瘫痪（magnet 永远解析不出元数据）
- **症状**：`metaDL` 状态卡住、`dht_nodes=0`、`connection_status=firewalled`、下载速度 0
- **根因**：`proxy_bittorrent=true` 时 BT 的 UDP/DHT 流量也走 HTTP 代理 → UDP 出不去 → DHT 建不了网 → magnet 无法解析
- **修复**：`proxy_bittorrent=false` + `proxy_peer_connections=false`（BT 流量直连，DHT 就能建网，通常几分钟内 dht_nodes 到 200+）；`proxy_misc=true` 保留（RSS 等走代理）
- **验证**：`transfer/info` 看 `dht_nodes` 和 `connection_status=connected`
- **注意**：只有走代理的 HTTP tracker（如 bangumi.moe）能通；DHT bootstrap 直连（dht.transmissionbt.com:6881 等）必须能通

### 2. 老番种子 tracker 常失效
- 2022 年前的番，RSS 抓来的种子 tracker 可能 Bad Gateway/timed out/unregistered
- **不要囤所有字幕组版本**：RSS 会把同番所有字幕组都抓进来（实测 399 个任务 90%+ 是 0% 僵尸）
- 策略：按番保留 1 个高质量字幕组（LoliHouse/DMG&LoliHouse 内封字幕最佳，Lilith-Raws Baha 源有繁中内嵌），其余全删

### 3. Mikan 的 enclosure 是详情页不是直链
- RSS `<link>` = 详情页 HTML，`enclosure url` = `/Download/xxx` 但**常 404**（老种子被清）
- **正确姿势**：抓详情页 HTML 里的 `magnet:` 链接（`re.search(r'magnet:\?[^"\']+')`），magnet 加 qB 靠 DHT 解析
- 批量添加用 API：`POST /api/v2/torrents/add`，`urls` 参数传 magnet，`savepath` 指定目录

### 4. 删除僵尸任务
- `POST /api/v2/torrents/delete`，`hashes` 用 `|` 连接，`deleteFiles=false` 保留已下文件
- 批量删除分 100 个/批避免 URL 过长
