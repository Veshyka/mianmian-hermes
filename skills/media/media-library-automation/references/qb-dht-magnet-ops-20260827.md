# qB DHT 瘫痪修复 + Mikan magnet 实战（2026-08-27）

本机实战记录：动漫下载链路（AutoBangumi+qB+飞牛影视）从部署到跑通的完整坑位。

## 1. qB 走代理会瘫痪 DHT（最重要）

**症状**：任务卡 `metaDL`（magnet 永远解析不出元数据）、`transfer/info` 里 `dht_nodes=0`、`connection_status=firewalled`、下载速度 0。

**根因**：qB 的 `proxy_bittorrent=true` 时，BT 的 UDP/DHT 流量也走 HTTP 代理 → UDP 出不去 → DHT 建不了网 → magnet 无法解析（torrent 文件不受影响，因为 tracker 是 HTTP 走得通；所以当时小圆能下、magnet 卡死）。

**修复**（qB API `/api/v2/app/setPreferences`）：
```json
{"proxy_type": "SOCKS5", "proxy_bittorrent": false, "proxy_peer_connections": false, "proxy_misc": true}
```
- BT 流量（peer + DHT）**直连**，DHT 几分钟内建网到 200+ 节点（`connection_status` 变 `connected`）
- `proxy_misc=true` 保留 RSS 走代理
- 验证：`transfer/info` 的 `dht_nodes` 和 `connection_status`

**关键判断**：宿主 `python3` 手搓 UDP ping 到 `dht.transmissionbt.com:6881` 是通的（54B 回复）→ 说明网络层 UDP 没问题，问题在 qB 代理配置本身。BT 流量直连是正解。

## 2. Mikan 链接格式坑

- RSS `<link>` = 详情页 HTML，不是种子直链
- `enclosure url` = `/Download/xxx` 但老种子**常 404**（资源被清，返回蜜柑计划首页 HTML）
- **正确姿势**：抓详情页 HTML，`re.search(r'magnet:\?[^"\']+')` 提取 magnet → 走代理用 `POST /api/v2/torrents/add` 加 magnet（`urls` 参数 + `savepath`），靠 DHT 解析
- magnet 批量添加：`hashes` 用 `|` 连接，分 100 个/批避免 URL 过长

## 3. 老番字幕组清理策略

- RSS 订阅会把同番**所有字幕组版本**都抓进来（实测 399 个任务 90%+ 是 0% 僵尸）
- 策略：按番保留 1 个高质量字幕组，其余全删（`POST /api/v2/torrents/delete`，`deleteFiles=false` 保留已下文件）
  - 内封字幕最佳：`LoliHouse` / `DMG&LoliHouse`（WebRip HEVC-10bit + ASS 内封）
  - 有繁中内嵌：`Lilith-Raws`（Baha WEB-DL，2022 番实测有源）
  - 老番（>2 年）种子 tracker 常失效（Bad Gateway/unregistered）→ 先测 tracker 再决定留哪组
- 参考脚本：`/opt/data/scripts/qb_analyze_groups.py`（按番+字幕组分组分析）、`qb_cleanup_zombies.py`（删 0% 僵尸）、`qb_add_magnet.py`（Mikan 详情页提取 magnet 批量加）

## 4. qB 下载目录 = 飞牛影视子目录

- qB `save_path` 应指向 `/bangumi/动漫`（宿主 `/vol1/1000/<USER>`），否则文件散在飞牛影视根目录刮削不到
- 改默认路径：`POST /app/setPreferences {"save_path": "/bangumi/动漫"}`
- 已有任务迁移：`POST /torrents/setLocation {"hashes": "...", "location": "/bangumi/动漫"}`（qB 后台 moving，机械盘大文件要等几分钟；迁移期间 API 显示 save_path 未变但文件在动，属正常）

## 5. 通用 qB API 要点（本机凭证）

- 地址 `http://127.0.0.1:8989`（宿主），认证 `admin/adminadmin`，`Authorization: Basic base64(admin:adminadmin)`
- `proxy_type` 枚举：字符串 `"SOCKS5"` / `"HTTP"`（不是数字！写数字会变 None）
- `POST` 参数用 `urllib.parse.urlencode` form 格式
- 大批量 JSON 用脚本文件执行，避免 shell 引号嵌套（本机多次踩坑）
