# 升级/重建 Hermes 容器前：持久化体检（2026-09-07 实测）

场景：主人要 `docker pull` 新镜像更新 Hermes → docker 会重建容器 → **容器可写层（非挂载卷文件）全部丢失**。升级前必须体检：什么东西在挂载卷里（安全），什么东西在可写层（会丢）。

## 体检命令（宿主侧）

```bash
# 1. 挂载点清单
docker inspect hermes --format '{{range .Mounts}}{{.Source}} -> {{.Destination}} (rw={{.RW}}){{println}}{{end}}'

# 2. 容器可写层 diff——排除挂载卷前缀(/opt/data)、tmp、伪文件系统后，剩下的才是「重建会丢的自定义」
docker diff hermes | grep -vE '^C /opt/data|^A /opt/data|^C /tmp|^A /tmp|/proc/|/sys/|/dev/|/run/|/etc/|/var/'
```

本机（2026-09-07）实测结果：可写层里**唯一持久自定义 = `/opt/hermes/gateway/platforms/qqbot/adapter.py`**（8/25 打的群消息 sender ID 补丁，镜像内文件，重建必回原始版）。/etc 改动只是 passwd/group（容器内建的 hermes 用户，正常）。

## 会丢项的持久化备份（已就位）

- 补丁备份：`/opt/data/scripts/adapter.py.bak-20260825`（在挂载卷 ✅）
- 重打脚本：`/opt/data/scripts/patch_adapter.py`（幂等，重建后跑 `python3 patch_adapter.py`）
- ⚠️ 新版本镜像 adapter.py 源码可能变化 → patch 的匹配串失效 → 升级后要**实测群消息 sender 标记还在不在**，失效就按新版源码重新适配

## 关键路径事实（8/25 迁移后）

| 项 | 当前值（实测） | 旧值（文档/脚本残留，勿用） |
|---|---|---|
| 数据卷 | `/vol1/1000/<USER>`（rw） | `/vol2/docker/hermes-data` |
| 备份挂载 | `/vol1/1000/<USER>`（ro） | 同左 |
| CMD | `hermes gateway run`（s6 托管 gateway 自启）+ env `HERMES_DASHBOARD=1` | `hermes dashboard --host 0.0.0.0 --port 9119 --no-open` |
| 端口 | `19119:9119` | 同左 |

**血泪教训**：`/opt/data/scripts/hermes-rebuild-gateway-mode.sh` 等旧 rebuild 脚本里写的是**旧数据卷路径** `/vol2/docker/hermes-data`——照旧脚本重建 = 挂错卷 = 全部数据「失忆」（SOUL/MEMORY/skills/会话库全不在新容器视野内）。重建一律从**当前容器** `docker inspect` 导配置，只换镜像 tag。

## 安全升级流程（主人确认后执行）

1. 备份：`docker inspect hermes` 全量（Config/HostConfig/NetworkSettings）存到挂载卷（不要存 /tmp）
2. 保底：`docker rename hermes hermes-old`（回滚点，确认新版正常前不删）
3. `docker pull nousresearch/hermes-agent:latest` → 用当前容器同参数起新容器（CMD 保持 `hermes gateway run`）
4. 重打 adapter 补丁 + 实测群消息标记
5. 验证：gateway 回线（gateway_state.json qqbot state=connected）、skills/记忆/cron 都在（全在挂载卷，无需迁移）
