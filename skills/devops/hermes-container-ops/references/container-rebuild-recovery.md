# Hermes 容器重建与恢复清单（2026-08-24 实测）

## 场景
手动 `docker run` 部署的 Hermes 容器需要加挂载/改参数时 → **重建容器**（docker 不支持热挂载/热改参数）。

## 重建流程（宿主侧，需管理员 sudo 通道）
1. **先备份容器定义**：`docker inspect hermes --format "{{json .Config}}"` / `HostConfig` / `NetworkSettings` → 存文件（重建失败可对照恢复）
2. **保底改名**：`docker stop hermes && docker rename hermes hermes-old` → 再 `docker run` 新容器（原参数 + 新 `-v`）→ 验证 OK 后 `docker rm hermes-old`
3. 模板参考：`/opt/data/scripts/hermes-rebuild-add-backup-mount.sh`（容器侧路径 = 宿主 `/vol2/docker/hermes-data/scripts/`，同一挂载）
4. ⚠️ **脚本必须脱离 SSH 会话执行**：容器 stop 会杀掉正在跑的 SSH/terminal 进程——若脚本在前台跑会中断在 stop 和 rename 之间（半重建状态！）
   ```bash
   ssh 宿主 'nohup env SUDO_ASKPASS=/tmp/mian_sudo.sh sudo -A bash <脚本> > /tmp/rebuild.log 2>&1 &'
   ```

## 重建后恢复清单（容器 /tmp 是 tmpfs，重启即清空！）
1. **SSH askpass 重建**：`/tmp/askpass.sh` 丢失 → SSH 通道断。密码可从 Hindsight recall 找回（查「SSH 密码 棉棉」）→ `printf '#!/bin/sh\necho <pw>\n' > /tmp/askpass.sh && chmod 700`
2. **gateway 恢复**：s6 槽重建后消失（dashboard 容器跳过 reconcile）→ 跑 `/opt/data/scripts/gateway_s6_migrate.sh`（先 `chmod +x`；脚本内 s6-svstat 不在 PATH，需绝对路径 `/package/admin/s6/command/s6-svstat`）或 `hermes gateway run`
3. **挂载验证**：`ls` 新挂载点——注意容器内 UID 权限（trim_acl 对容器无效，见 fnos-trim-cli-skill references/fnos-platform-pitfalls.md）
4. **browser 通路**：`browser.cdp_url` 在 config.yaml（数据卷）保留；socat 在宿主不受影响 → `curl http://172.17.0.1:16003/json/version` 验证
5. **心跳 cron**：gateway 起来后自动恢复（调度器在 gateway 进程内）
6. 数据卷 `/opt/data` 挂载保留 → skills/config/sessions/memories 全不丢，无需重迁移

## 关键教训
- 容器重建 ≈ 换了个新身体：/tmp 全清、s6 槽全无、SSH 辅助文件丢失——**恢复清单要按顺序走**，别在重建后假设环境还在
- 重建前先确认数据卷挂载正确（`-v /vol2/docker/hermes-data:/opt/data` 是命根子）
