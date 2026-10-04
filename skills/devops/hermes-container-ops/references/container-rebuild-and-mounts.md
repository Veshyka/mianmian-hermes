# 容器重建与恢复手册（2026-08-24 实测，docker run 部署的 hermes 容器）

手动 `docker run` 部署的 Hermes 容器（非 compose，无 compose labels）重建/加挂载/恢复的完整流程。所有宿主操作走管理员账号（SSH 棉棉 + SUDO_ASKPASS）。

## 1. 容器重建（加挂载等，需改容器定义时）

```bash
# 备份当前定义（宿主，sudo）
docker inspect hermes --format "{{json .Config}}"    > /tmp/hermes-config.json
docker inspect hermes --format "{{json .HostConfig}}" > /tmp/hermes-hostconfig.json
docker inspect hermes --format "{{json .NetworkSettings.Networks}}" > /tmp/hermes-net.json

# 保底流程（可回滚）：stop → rename 保底 → run 新 → 验证 → 删保底
docker stop hermes
docker rename hermes hermes-old          # 回滚点
docker run -d --name hermes --restart unless-stopped \
  -e PUID=1003 -e PGID=1001 \
  -e HERMES_DASHBOARD_BASIC_AUTH_USERNAME=admin \
  -e HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=... \
  -e HERMES_HOME=/opt/data -e HERMES_WRITE_SAFE_ROOT=/opt/data \
  -p 19119:9119 \
  -v /vol2/docker/hermes-data:/opt/data \
  -v "/vol1/1000/<USER>" \
  --entrypoint /opt/hermes/docker/entrypoint-dispatch.sh \
  nousresearch/hermes-agent:latest \
  hermes dashboard --host 0.0.0.0 --port 9119 --no-open
# 验证挂载后删保底：docker rm hermes-old
```
参考脚本：/opt/data/scripts/hermes-rebuild-add-backup-mount.sh（含完整 env）。

⚠️ **重建 = 当前会话中断**（TUI/dashboard 进程被杀）。执行方式：SSH 宿主用 `nohup setsid sudo -A bash <脚本>` 脱离容器进程，否则容器一停脚本就断在中间态（容器 stop 了但没 run 新的）。

## 2. 重建后 gateway 恢复

容器重建后 /run/service（tmpfs）清空，s6 槽没了：
- `hermes gateway run` 会报 `no such gateway 'default': register it with hermes profile create default`（需要槽先存在）
- **正确做法**：手动跑一次 `/opt/data/scripts/gateway_s6_migrate.sh`（MIGRATE_DELAY=0，**绝不挂 s6 槽自动跑**——见 gateway-restart-loop-postmortem）
- 该脚本会注册 gateway-default 槽 + 拉起；`s6-svstat` 在容器内不在 PATH，脚本已改用绝对路径 `/package/admin/s6/command/s6-svstat`
- 验证：`ps -o ppid -p <gateway_pid>` 父进程 = `s6-supervise gateway-default`；QQ connected

## 3. SSH askpass 重建（容器重建后必丢）

askpass.sh 在容器 /tmp（tmpfs），重建即丢。恢复：
- SSH 密码/sudo 密码从 Hindsight recall 找回（主人历史上存过：`<口令见 /opt/data/scripts/askpass.sh（700，勿写进文档）>`，SSH 与 sudo 同密码）
- 重建：`printf '#!/bin/sh\necho <pw>\n' > /tmp/askpass.sh && chmod 700 /tmp/askpass.sh`
- 宿主侧 sudo 通道：把同一脚本内容写到宿主 `/tmp/mian_sudo.sh`（SSH 管道 `cat > /tmp/mian_sudo.sh && chmod 700`），然后 `export SUDO_ASKPASS=/tmp/mian_sudo.sh; sudo -A <cmd>`

## 4. ⚠️ 飞牛 trim_acl 坑（重要）

飞牛 OS 的 `setfacl` 写的是**私有 ACL** xattr `system.trim_acl`——**容器内（标准内核权限检查）不认**！表现为：宿主侧 `sudo -u <uid> ls` 成功、容器内同 UID 却 `Permission denied`，容器内 `ls -ld` 看不到 ACL（无 `+` 标记）。

**判定方法**：`python3 -c "import os; print(os.listxattr('/path'))"` —— 出现 `system.trim_acl` 而非 `system.posix_acl_access` 即中招。

**解法**：不用 setfacl，用 **chmod 组权限**。容器 hermes 的 gid=1001 恰好 = 宿主 Users 组（备份目录 group 就是 Users）：
```bash
chmod g+rx /vol1/1000/<USER>            # 父目录组可进入
chmod -R g+rX /vol1/1000/<USER>     # 备份目录递归组可读（X 只对目录/有 x 的文件加）
```
不改变 owner，可逆（chmod g-rx 恢复）。容器内立刻可读。

## 5. 重建后不受影响的项（无需处理）
- browser 通路：socat 在宿主、`browser.cdp_url` 在数据卷 config.yaml → 自动恢复
- 技能/记忆/配置：全在 /vol2/docker/hermes-data 数据卷
- 心跳 cron：gateway 恢复后自动继续

## 重建容器必须带上时区（2026-10-04 事故）

容器默认跑在 **UTC**（`TZ` 空、`/etc/localtime -> Etc/UTC`）。2026-10-04 的后果：
「北京时间 00:30–07:00 不触发」被算是 08:30–15:00，她凌晨 4-7 点连发 4 条；
日志时间戳也比主人的表慢 8 小时。

现状：已把 `/etc/localtime` 软链到 `Asia/Shanghai` + 写 `/etc/timezone`（容器可写层，
`docker restart` 保留、**`docker rm`/重建会丢**）。台账见
`ops-changelog/ownership-changes.md` 2026-10-04 条。

**重建清单里加这两条中的任意一条**：
1. 建容器时带 `-e TZ=Asia/Shanghai`（首选）；或
2. 重建后补：`docker exec hermes ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime`
   ＋ `echo Asia/Shanghai > /etc/timezone`，然后重启网关。

复核一句话：`docker exec hermes date` 必须是 `CST`。代码层不依赖它
（`plugins/onebot/onebot_time.py` 显式用 Asia/Shanghai），所以丢了不会让她出错，
只是日志时间戳又变 UTC。
