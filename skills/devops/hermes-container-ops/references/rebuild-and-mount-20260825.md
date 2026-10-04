# 2026-08-25 备份挂载实战全记录

目标：把 `/vol1/1000/<USER>` 挂进 Hermes 容器（只读），让棉棉随时看备份。全流程 + 错误路径，含教训。

## 环境
- Hermes 容器：手动 docker run（无 compose），仅 `-v /vol2/docker/hermes-data:/opt/data`
- 备份目录：`/vol1/1000/<USER>`（owner shy:Users，700）
- 容器用户：hermes uid=1003 gid=1001

## 步骤与坑

### 1. 定位备份路径
- 宿主 `ls /vol2` 顶层无 backup → 查 Hindsight recall「备份 文件夹 路径」→ 命中 `/vol1/1000/<USER>`（主人 2026-08-23 定义过「默认提及备份即指此路径」）
- **教训：环境路径先查 Hindsight，别靠猜**（主人也说过「自己去 H 查」）

### 2. sudo 通道（SSH 已通但 sudo 要密码）
- 宿主没有 `/tmp/askpass.sh`（容器才有）→ 之前 sudo 失败根因：MYPASS 提取在宿主读文件失败
- 正解：容器里 `printf '#!/bin/sh\necho <密码>\n' | setsid ssh 棉棉@172.17.0.1 'cat > /tmp/mian_sudo.sh && chmod 700'`，然后 `export SUDO_ASKPASS=/tmp/mian_sudo.sh; sudo -A <cmd>`
- 密码：SSH 与 sudo 同密码（<口令见 /opt/data/scripts/askpass.sh（700，勿写进文档）>），从 Hindsight recall 可找回

### 3. 重建容器（加挂载）
- 用 detached SSH 跑重建脚本（`nohup env SUDO_ASKPASS=... sudo -A bash <script>`），因为 `docker stop hermes` 会杀掉前台执行中的 shell（实测 exit -15 SIGTERM = 容器已停，脚本中断在中间态）
- 脚本含保底：stop → rename hermes-old → run 新 → 验证 → 手动删保底
- **重建后 /run/service 是 tmpfs，gateway 槽丢失**；`hermes gateway run` 报 `no such gateway 'default'`（槽不存在）→ 需要先跑 `gateway_s6_migrate.sh` 重建槽

### 4. gateway 恢复的坑
- `gateway_s6_migrate.sh` 无执行权限（write_file 默认 644）→ `chmod +x` 后跑
- 脚本验证段用裸 `s6-svstat`（不在 PATH）→ 误判「NOT up」→ 回滚到 manual；实际 s6 已托管（664 父进程 = s6-supervise）。**修：用绝对路径 `/package/admin/s6/command/s6-svstat`**
- 结果：gateway 实际在 s6 下，QQ connected，心跳 cron 恢复

### 5. ⚠️ trim_acl 坑（本场最大坑，两轮排查）
- 第一轮：`setfacl -R -m u:1003:r-x /vol1/1000/<USER>` + 父目录 `u:1003:x` → 宿主 `sudo -u 棉棉 ls` 成功，但**容器内 hermes(1003) 仍 Permission denied**
- 排查证据链：容器内 stat mode 0700（group 位 000）；宿主 getfacl 正常（user:1003:r-x, mask:r-x）但 `ls -ldn` 无 `+`；宿主 listxattr = `['system.trim_acl']`（**不是标准 posix_acl_access**）
- 结论：fnOS setfacl 写私有 xattr，容器内内核不认
- 正解：容器 gid=1001 = fnOS Users 组 → `chmod g+rx /vol1/1000/<USER>` + `chmod -R g+rX /vol1/1000/<USER>` → 容器内立刻可读（owner 不动，可逆）
- **教训：fnOS 的「ACL」= trim_acl，容器访问宿主文件别用 setfacl；先看目标目录 group 能不能匹配容器 gid**

### 6. 收尾
- 验证：容器内 `ls /opt/data-backup/` 全列出（Frp/hindsight历史备份/爱你棉棉…）；深层文件也可读
- 保底容器 `hermes-old`（Exited）保留待确认后删
- browser 通路（socat 在宿主）不受重建影响，curl 16003 正常
- `/tmp/askpass.sh` 重建后丢失 → 用 Hindsight 找回密码重建

## 沉淀
- 挂载/权限/恢复流程 → hermes-container-ops SKILL.md
- trim_acl 坑 → SKILL.md 专节
- 主人授权规则：sudo/root 走管理员账号；无替代可有限提权（默认保守）
