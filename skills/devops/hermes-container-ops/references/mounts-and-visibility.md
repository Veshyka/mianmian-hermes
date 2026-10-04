# 挂载与路径可见性（本机实测，2026-09-19）

> 运维总入口（通道优先级 / 端点表 / cron / 坑清单）：`/opt/data/ops-changelog/README.md`
> 属主/权限变更台账：`/opt/data/ops-changelog/ownership-changes.md`

## 容器里直接挂进来的（不需要任何通道，SSH/trim-cli 都不用）

| 容器内路径 | 宿主源 | 类型/标志 | 属主/权限 | 我能干什么 |
|---|---|---|---|---|
| `/opt/data` | `/vol1/1000/<USER>` | btrfs 子卷，rw | `hermes:hermes` 700 | 读写（是我的家目录 / HERMES_HOME） |
| `/opt/data-backup` | `/vol1/1000/<USER>` | zfs，rw | `1000:hermes` 750 | **可读不可写**（组只有 r-x） |

`findmnt` 实测，其余宿主路径（`/vol1/...`、`/vol2/...` 的其它子目录，含 `/vol2/@team/共享文件`）**在容器内不存在**。

⚠️ 常被记错的一点：`/opt/data-backup` 的挂载标志是 `rw`，不是只读挂载——写不了的真实原因是目录权限（属主是宿主 uid 1000，hermes 组只有 `r-x`）。

## 宿主其它路径：走官方 trim-cli 通道（无 SSH）

```bash
cd /opt/data/skills/productivity/fnos-trim-cli-skill
./bin/trim-cli-linux-x64 --host 172.17.0.1 --port 5666 --scheme ws --allow-insecure-ws \
  file ls "/vol1/1000/<USER>"        # 实测返回真实 JSON
./bin/trim-cli-linux-x64 ... file ls "/vol2/@team/共享文件"
```

`file` 模块能力：`ls / ls-dir / search / size / prop / access / calc / compress / extract / mkdir / cp / mv / rm / rename / trash / chown / trimacl / acl / share / fav / recent / upload / download-url / app-dir-list / sys-part-info / is-mount-point / usage`。
读类已实测；写类（mkdir/cp/mv/rm/trash/upload）命令存在但未在本机实跑——首次真要用时先做一次 mkdir → trash 的往返验证。

## 排查路径可见性问题的固定三步

1. `findmnt -rno TARGET,SOURCE,FSTYPE,OPTIONS` —— 先看是不是已经 bind mount 进来（是的话跟通道无关）
2. `ls -ld <路径>` + `id` —— 判断是"看不见"还是"看得见但没权限"
3. 容器内没有的路径 → 不要急着 SSH，先试 trim-cli `file ls`（见 `fnos-trim-cli-skill/reference/known-issues-hermes.md`；报 errno 135168 就先重登）

## 通道优先级（主人 2026-09-19 定）

宿主操作按这个顺序走，前一条能办成就不动下一条：

1. **容器内 bind mount 直读** —— `/opt/data`（映射文件夹）、`/opt/data-backup`（备份）；跟通道无关，最省事
2. **官方 trim-cli** —— `file`（宿主任意路径的读/写）、`docker`（容器查看与启停）、`app`（应用中心启停）、`monitor`、`system`、`storage`、`photos`、`media`、`download`；连接与坑见 `fnos-trim-cli-skill`
3. **服务 HTTP API** —— 搜 SearXNG、记忆 Hindsight、推理 ollama、代理 mihomo、qBittorrent
4. **SSH（兜底）** —— 只在官方通道确实到不了时：`docker logs/exec/run`、宿主日志排查、sudo 级属主/权限写。主人不在场需要自己解决问题时，用它把事办成。

理由：前三条不依赖额外凭证、可复现、不越容器权限边界；SSH 万能但带宿主凭证，用多了就把"万能钥匙"当默认路径了。

