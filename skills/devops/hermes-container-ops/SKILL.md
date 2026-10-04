---
name: hermes-container-ops
description: Use when 运维 Hermes 容器/网关（重建、挂载、权限、平台接入、重启恢复）或 AstrBot 聊天门接入层（provider/persona/插件/MCP）。
version: 1.0.0
metadata:
  hermes:
    tags: [hermes, docker, mount, acl, trim_acl, socat, fnos, gateway-recovery, gateway, qqbot, credentials]
---

# Hermes 容器运维

本机 Hermes 是**手动 docker run 部署**（无 compose、无应用中心 labels），部署环境为飞牛 OS（fnOS）。本 skill 覆盖：容器识别、重建加挂载、容器→宿主文件权限、端口转发、重启恢复。

## When to Use
- 要给 Hermes 容器加挂载（备份目录、共享文件夹等）或改环境变量
- 容器访问宿主文件报 `Permission denied`（挂载了但读不了）
- 容器重建/重启后的恢复（gateway、服务回线）
- 需要把宿主 127.0.0.1 服务暴露给容器访问
- 排查「容器里看不到/读不了宿主某目录」

## 关键事实（本机环境）
- 容器名 `hermes`，镜像 `nousresearch/hermes-agent:latest`，restart=always，**host 网络**（实测：`docker inspect hermes --format '{{.HostConfig.NetworkMode}}'` = `host`，无发布端口、`docker port hermes` 为空；旧版本曾是 bridge/172.17.0.4）→ 两个后果：容器内 `127.0.0.1` **就是宿主 loopback**（自己绑 127.0.0.1 的服务同宿主容器可直接互通），容器自己的监听端口**直接落在宿主上**；**要与它互通的 sidecar 容器也必须用 host 网络**
- 唯一数据挂载：宿主某目录 → 容器 `/opt/data`（rw）。**挂载源路径要现场确认**：`docker inspect hermes --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}'`——技能里写的路径可能与宿主实际不符，别照抄。`/opt/data` 与宿主 `/vol1/1000/<USER>` 是同一目录，两边文件可互看。**容器内 `/run/service` 是 tmpfs，重建即清空**
- 容器内用户 hermes **uid=1003, gid=1001**（gid 恰好 = fnOS Users 组！）。⚠️ **例外（profile 级 sidecar）：必须显式传 `PUID`/`PGID`，否则它跑成 uid 10000** —— entrypoint 会把 `$HERMES_HOME`（`<挂载>/profiles/<name>`）chown 成**容器内 hermes 的 uid（镜像默认 10000）**并以该 uid 运行 → 同挂载里属主 1003、mode 600 的文件（`auth.json`、`kanban.db`…）读不到、写不了，日志刷 `PermissionError` 且 kanban dispatcher 每分钟 tick 失败。**修法**：`-e PUID=$(stat -c %u <挂载根>) -e PGID=$(stat -c %g <挂载根>)`（即主容器那套值；`HERMES_UID`/`HERMES_GID` 是同义键），entrypoint 会把容器内 hermes 重映射成该 uid/gid 并 chown 数据卷 → 权限与主容器一致、报错归零。**别用 `docker run --user <uid>` 顶**：entrypoint 明确拒绝任意 `--user`（它需要 root 才能做 UID 重映射与数据卷 chown，会直接报错退出）。启动器模板见 `templates/chat-door-run.sh`
- **宿主侧属主对照（判断「容器里能不能写」就看这个）**：`990 = trim.openclaw`（组 `901 AppUsers`，旧 OpenClaw 遗留）、`1003 = 棉棉`（= 容器内 hermes）、`1001 = fnOS Users` 组。判据：`ls -ldn <dir>`，属主是 1003 或组是 1001 才可能写。**迁移残留会炸在 Hermes 自己的目录上**（实测 `cache/`、`image_cache/`、`audio_cache/`、`plugins/`、`platforms/`、`backups/` 等 10 个顶层目录仍是 990，其中 cache 类还是 600）——症状是 `Permission denied: '<HERMES_HOME>/cache/vision'` 这类。属主是 990 就是迁移残留。**处置口径：先确认文件不属于主人**（是 Hermes 自身缓存/运行态，或棉棉写的文档）**→ `chown 1003:1001` → 登记进 `/opt/data/ops-changelog/ownership-changes.md`**（from→to、理由、回滚命令、验证结果）；**属于主人的数据不动**（如 `backups/` 里主人自己的备份），只报告。不便 sudo 时可先绕（Hermes 的 `get_hermes_dir()` legacy 回退：旧名目录只要有内容就用它，如 `mkdir /opt/data/temp_vision_images` 顶替 `cache/vision`），但属主修好后**要撤掉回退**（否则永久遮蔽正规路径）。属主是 1003 却仍失败才是真问题。细节见 `references/migration-ownership-residue.md`
- 无 compose 文件：`docker inspect hermes --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}'` 为空 = 手动部署
- 入口：`--entrypoint /opt/hermes/docker/entrypoint-dispatch.sh` + CMD **`hermes gateway run`**（实测现状；dashboard 由 env `HERMES_DASHBOARD=1` 打开，不再靠 CMD 传参、也不再发布端口）；env 含 `HERMES_HOME=/opt/data`、`HERMES_DASHBOARD=1`、`HERMES_DASHBOARD_BASIC_AUTH_*`、`HERMES_DISABLE_LAZY_INSTALLS`、`PLAYWRIGHT_BROWSERS_PATH` 等。**重建前先 `docker inspect` 现状、按现状复刻**——下面「重建模板」里的 dashboard CMD + `-p 19119:9119` 是旧形态，照抄会得到与现网不一致的容器。
- **不要拿 `rm` 删 profile**：`hermes profile delete` 在 Python 3.13 下会报 `open() missing required argument 'flags'` 而删不掉目录（注册表已删、目录还在）→ 手动把残留目录 `mv` 进 `/opt/data/.trash/`，别硬删。

## 宿主访问通道优先级（主人 2026-09-19 定，常驻规则）

宿主上的活按这个顺序走，前一条能办成就不动下一条：

1. **容器内 bind mount 直读**：`/opt/data`（映射文件夹）、`/opt/data-backup`（备份目录）——跟通道无关，最省事；判断某路径是不是已经挂进来用 `findmnt -rno TARGET,SOURCE,FSTYPE,OPTIONS`
2. **官方 trim-cli**（用封装 `python3 /opt/data/scripts/trim_cli.py [--json] <子命令>`——自动带连接参数，遇 `135168` 自动重登重试）：`file`（宿主任意路径读写）、`docker`（容器查看/启停）、`app`（应用中心启停，含 `app restart ai_installer` 修 GPU 回退）、`monitor`、`system`、`storage`
   - 报 `failed to get docker data ... errno 135168` 时**不是权限问题**：`135168 = 0x21000 = E_INVALID_TOKEN`，即 CLI 拎着过期会话 → 用封装跑（自动重登重试），或手动 `login -u 棉棉`。免 token 端点（`system info`、`monitor`）在无会话时也能通，所以它不能当「鉴权正常」的证据。
   - **别从错误码直接推根因（通用做法）**：遇到自定义 errno 先查**服务端自己的 errno 表**，再看**服务端日志有没有收到这条请求**——「日志里根本没有」就把范围缩到鉴权/传输层，不要去猜权限模型。实测曾把 135168 猜成「权限不足」并据此给了建议，复现一次才发现是会话过期。
   - **别从「空输出」「没命中」推结论——先怀疑探针**（同上一条同一类病）。探针本身就是最常见的假阴性来源，三个实例：① `SUDO_ASKPASS` 误用了容器里的 `askpass.sh`（**该用宿主 `mian_sudo.sh`**）→ 命令静默失败、输出为空，于是被记成「sudo 不通」，其实 sudo 一直可用；② `docker logs --tail 500` 够不到几小时前的启动行 → 据此断言「日志里没有」，其实只是窗口太短；③ 探某能力存不存在时，协议端用**一段错误文案表达两种意思**（「action 不存在」与「参数名/类型写错」同形）→ 拿到失败返回值就直接宣判「没这个能力」；④ **探针脚本自己写错**——用带 `shift` 的 shell 函数包 curl 时，`shift` 之后位置参数整体前移、参数被拼进 URL 路径，返回的报错与 ③ 同形，于是「探针 bug」被读成「协议端拒绝了这个参数」，一条假结论就这么进了报告。**要断言某能力/某条日志不存在，先换更长窗口、换正确凭证路径、换端点、并拿一个已知不存在的目标做基线校准，各自复现一次**，再下结论；**探针本身改过就要重校**——一次性校准只证明当时那个版本能读对；⑤ **裸 `grep <端口>` 会匹到别人的端口**：`grep 8888` 命中的其实是 SearXNG 的 **18888**，据此断言「8888 在听」而真服务正在崩——查端口用精确匹配（`ss -tln | grep -E ':<端口>\b'`，或 `/proc/net/tcp` 反查），最硬的判据仍是发一次真实业务请求。协议端能力探测的完整校准法、错误三态表与探针写法坑见 `references/onebot-protocol-capability-probe.md`。
   - 边界清单（哪些 docker 操作官方通道做不了、要 SSH）见 `fnos-trim-cli-skill/reference/known-issues-hermes.md`
3. **服务 HTTP API**：SearXNG / Hindsight / ollama 或 llama.cpp / mihomo / qBittorrent
4. **SSH（兜底）**：只在官方通道确实到不了时——`docker logs/exec/run`、宿主日志排查、sudo 级属主/权限写。主人不在场、需要自己把问题解决掉时照用，不必等确认。**SSH 一律走连接复用**：`~/.ssh/config` 写 `ControlMaster auto` + `ControlPath ~/.ssh/cm-%r@%h-%p` + `ControlPersist 30m`。飞牛对**每次** SSH 登录/断开都推一条手机通知，而自检类 cron 一轮要开好几条 → 不复用等于按次骚扰主人（实测 `*/30` 的自检让他一晚收到 30~50 条「连接/断开」）。验证两点：第二条连接耗时从 ~0.2s 掉到毫秒级；跑完一轮自检后宿主通知**零新增**。磁盘休眠那类通知是正常省电，可在飞牛「事件通知」里按规则单独关，别当故障查

**报文件位置一律给宿主映射路径（主人明确纠正过）**：他拿着映射文件夹去找文件（`/vol1/1000/<USER>`），容器路径 `/opt/data/...` 对他没用。答「某文件在哪」时**第一列就给映射路径**，容器路径只在描述容器内操作时附带说明（对应关系：宿主 `<映射>` = 容器 `/opt/data`，现场可核 `findmnt -rno TARGET,SOURCE` 或 `/proc/self/mountinfo`）。别只报容器路径让他自己换算——他会直接把这条当成答错了。

**分工（主人 2026-09-21 定）**：宿主上容器的**重启/删除由主人自己做**（他会自己动手，也会自己收紧 `mem_limit`），棉棉这边的活是**诊断 + 停服务**。被指派「先停掉」时按 取证 → 停 → 交代 走：①取证（`docker stats --no-stream` 逐容器占用、`docker inspect --format '{{.HostConfig.Memory}}'` 看上限、`pgrep -x llama-server` + `/proc/<pid>/status` 的 `VmRSS`/`VmSwap`/`RssAnon`、整机 `free -m` + swap——用 `-x` 精确匹配，`pgrep -f` 会把 SSH 命令自己那条 bash 也匹上）②停③**交代「停了什么、没停什么」**。红线：`hermes` 自身容器（停了会话就断）与 QQ 通道（napcat/astrbot）不停；主人点名自己要重启的（如 hindsight）别抢着停。停完顺手把内存回收量报出来（used/available/swap 前后对比），主人靠这个判断停对了没有。

**判「内存上限够不够」看 `oom_kill`，不看占用百分比**：`RssAnon`（KV cache + 计算缓冲）才是真正钉住的量，权重是 mmap 的文件页缓存、内核随时可回收——所以调紧 `mem_limit` 后占用仍停在 85~99% 是**正常现象**（缓存会一路填到上限附近），不代表太紧（实测同一容器：上限 10G 时用 8.19G，收到 4.13G 后仍正常跑在 3.9G/89%，`oom_kill`=0）。判据：`sudo -A cat /sys/fs/cgroup/system.slice/docker-<id>.scope/memory.events` 的 `oom_kill`/`max` 保持 0 + `docker inspect` 的 `RestartCount`/`OOMKilled` 不涨 = 够用；一旦涨就说明 anon 顶到了上限（KV 随 `-c`/`--parallel`/并发涨），该给回余量或降 ctx。压紧上限后**先跑一次真实业务**（一条 retain / 一次 LLM 调用）再下结论，别只看数字。

理由：前三条不依赖额外凭证、可复现、不越容器权限边界；SSH 万能但带宿主凭证，用多了会把「万能钥匙」当默认路径。挂载清单与三步排查法见 `references/mounts-and-visibility.md`。

## 重建容器加挂载（无 compose 环境）

**先备份当前配置**（回滚依据）：
```bash
docker inspect hermes --format '{{json .Config}}' > /tmp/hermes-config.json
docker inspect hermes --format '{{json .HostConfig}}' > /tmp/hermes-hostconfig.json
docker inspect hermes --format '{{json .NetworkSettings.Networks}}' > /tmp/hermes-net.json
```

**重建模板**（加新挂载示例：备份目录只读）：
```bash
docker stop hermes
docker rename hermes hermes-old          # 保底容器 = 回滚点（确认新容器正常前别删）
docker run -d --name hermes --restart unless-stopped \
  -e PUID=1003 -e PGID=1001 \
  -e HERMES_DASHBOARD_BASIC_AUTH_USERNAME=admin -e HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=<pass> \
  -e PYTHONUNBUFFERED=1 -e PYTHONDONTWRITEBYTECODE=1 \
  -e PLAYWRIGHT_BROWSERS_PATH=/opt/hermes/.playwright -e npm_config_install_links=false \
  -e HERMES_WEB_DIST=/opt/hermes/hermes_cli/web_dist -e HERMES_TUI_DIR=/opt/hermes/ui-tui \
  -e HERMES_HOME=/opt/data -e HERMES_WRITE_SAFE_ROOT=/opt/data \
  -e HERMES_DISABLE_LAZY_INSTALLS=1 -e HERMES_LAZY_INSTALL_TARGET=/opt/data/lazy-packages \
  -p 19119:9119 \
  -v /vol2/docker/hermes-data:/opt/data \
  -v "/vol1/1000/<USER>" \
  --entrypoint /opt/hermes/docker/entrypoint-dispatch.sh \
  nousresearch/hermes-agent:latest \
  hermes dashboard --host 0.0.0.0 --port 9119 --no-open
```
⚠️ **执行注意**：重建脚本必须**在宿主侧 detached 跑**（`nohup setsid` / 后台），因为 `docker stop hermes` 会杀掉容器内正在执行命令的进程——前台跑会中断在中间状态（容器停了但没 rename/没起新的）。

**重建后恢复清单**（/run/service 是 tmpfs，全丢）：
1. gateway：`MIGRATE_DELAY=0 /opt/data/scripts/gateway_s6_migrate.sh`（自动注册 s6 槽+拉起；脚本里 s6-svstat 必须用绝对路径 `/package/admin/s6/command/s6-svstat`）或先 `hermes gateway run`（槽不存在时会报 `no such gateway`，需先跑 migrate）
2. 验证 QQ：gateway_state.json platforms.qqbot.state == connected
3. 宿主侧服务（socat 等）不受影响，不用动
4. askpass：**durable 的只有挂载里的两份 700 脚本** `/opt/data/scripts/askpass.sh` 与 `/opt/data/scripts/mian_sudo.sh`（宿主同路径 `/vol1/1000/<USER>`），重建后**不用重建**；丢的只有曾被复制到容器 `/tmp` 的临时副本。⚠️ **别为了 sudo 往宿主 `/tmp` 再传一份口令副本**：宿主上已经有 durable 那份，SSH 命令里直接 `export SUDO_ASKPASS=/vol1/1000/<USER>`（容器内路径视图则是 `/opt/data/scripts/mian_sudo.sh`）就能 `sudo -A docker …`；多传一份就是多一个口令落点，还得事后专门去清（若确实传了，用完立即删并告知主人）。**宿主 `/tmp` 会被清空**（这条本机重复犯过）：sudo 报 `unable to run /tmp/mian_sudo.sh` 之类时**直接换上面那条 durable 路径**，不要重新往 `/tmp` 传一份——那只是把同一个坑再挖一遍。内容格式铁律：`#!/bin/sh` + `echo <口令>`（可执行脚本，**不是裸口令**；覆盖成裸口令会让 SSH 拿空口令当场登录失败）。取值 `sed -n '2s/^echo //p' <file>`，**口令只住这两个文件，绝不从记忆库或对话里回读、全程不打印**。**要交付口令给主人时同样不走聊天正文**（平台侧与日志各留一份，一次就永久）：生成/重置在代码里做、不回显，明文只落 0600 文件进主人的映射目录，再实测一次真登录、把路径告诉他，并建议改密后删文件——做法与 AstrBot 面板口令的重置/验证一并在 `references/astrbot-platform-ops.md`。

5. **容器时区**（活在可写层，重建回 UTC）：先 `date` 看是不是 CST；不是就
   `sudo -A docker exec hermes sh -c 'ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && echo Asia/Shanghai > /etc/timezone && date'`。
   影响面要说准：只影响**日志时间戳**与容器内 `date` 类脚本；她的时间判断走 `onebot_time.py`
   （`ZoneInfo("Asia/Shanghai")` + 硬编码 +8 兜底）→ 唤醒/主动起头/静默窗**不受影响**。汇报时别夸大“功能会坏”。
6. **把「重建后会丢的」交给自愈看门狗，不要写成“人需要记得的待办”**（人一定忘）：本机
   `scripts/hermes_selfheal_after_image_update.sh`（cron `*/30 * * * *`、`no_agent`、`deliver='origin'`）查两件——
   ① 镜像层补丁判据（`grep -q "retain_async=self._retain_async" /opt/hermes/plugins/memory/hindsight/__init__.py`）
   ② `date +%Z` 是不是 CST；不在位就走宿主修复：
   `/opt/data/hspr_ssh.sh 'export SUDO_ASKPASS=/vol1/1000/<USER> sudo -A docker exec hermes bash <脚本>'`。
   约定：**健康零输出**（上线前先空跑一次确认真的零输出，再单独验一次修复命令）、幂等、**真修了才开口**——
   所以 `deliver` 用 `origin`（修了啥主人看得到）而不是 `local`。

**重建前盘点的固定交付形状**（主人问「更新完东西会不会没」时直接照这个答，分两块）：
① **安全的**：列今天动过的关键文件 + mtime（都在挂载 `/opt/data` 里就一句说清）；
② **会丢的**：逐条列 + 影响面 + 恢复方式（本条就把「只有容器时区」这一个风险说清楚了）。
先扫 `/opt/data`，**也扫 `/opt/hermes`**
（`find /opt/hermes -newermt "$(date '+%Y-%m-%d') 00:00" -type f -not -path '*/.venv/*'`，期望输出为空）；
镜像层补丁要报「现在在不在位」的 grep 计数，不只报「登记过」。

## ⚠️ 飞牛 trim_acl 坑（容器读陿主文件 Permission denied）

**症状**：宿主 `setfacl` 给 u:1003:r-x 后，`sudo -u 棉棉 ls` 能读，但**容器内同一 UID 仍 Permission denied**。

**根因**：fnOS 的 `setfacl` 写的是**私有 xattr `system.trim_acl`**（不是标准 `system.posix_acl_access`），容器内内核不认。

**正解**（三选一，按优先级）：
1. **chmod 组权限**：容器 gid=1001 恰好 = fnOS Users 组 → `chmod g+rx <父目录>` + `chmod -R g+rX <目标目录>`，容器内即通（2026-08-24 实测备份目录）
2. 父目录也要能进：`/vol1/1000/<USER>` 是 700（shy 私有），访问 `/vol1/1000/<USER>` 必须先 `chmod g+rx /vol1/1000/<USER>`（或 setfacl 加 x——注意 trim_acl 只在宿主有效）
3. 别 chown 主人文件（会破坏归属）

**验证**：宿主 `sudo docker exec hermes ls /opt/data-backup/` 能列出 = 容器内可读。
**判断工具**：`getfacl` 显示正常但容器内仍拒 → `ls -ldn` 看 group 权限位（有 ACL 时 group 位显示 mask）；宿主 `python3 -c "import os; print(os.listxattr('<path>'))"` 出现 `system.trim_acl` 而非 `system.posix_acl_access` = 私有 ACL，容器内无效。

## socat 端口转发（宿主 127.0.0.1 服务 → 容器）

场景：宿主服务只绑 127.0.0.1（如 fygo-browser CDP 16002），容器经 172.17.0.1 访问不到。SSH 隧道被宿主 sshd 禁（`Could not request local forwarding`）时用 socat：
```bash
# 宿主侧（棉棉账号即可，>1024 端口无需 root；必须 detached 脱离 SSH 会话）
nohup setsid socat TCP-LISTEN:16003,fork,reuseaddr TCP:127.0.0.1:16002 >/tmp/socat-cdp.log 2>&1 &
# 容器侧验证
curl -s http://172.17.0.1:16003/json/version
```
- socat 在 SSH 断开后存活（setsid 新会话 + nohup 忽略 HUP）
- 宿主重启后 socat 会丢，需重跑（记忆里有命令）
- 容器侧持久配置：`hermes config set browser.cdp_url http://172.17.0.1:16003`

**反向同理：宿主侧进程/代理 → 容器端口，地址一律用 `172.17.0.1`，不用 `127.0.0.1`。** 容器端口是发布在 docker 网桥网关（`172.17.0.1`）上的，宿主 `127.0.0.1:<port>` 不会有人监听；宿主侧脚本（systemd 单元、代理、cron）里写 `127.0.0.1` 会**静默断链**。
- 症状长这样：容器内工具报 `502 backend error: … [Errno 111] Connection refused`，而中间代理/服务的 `/health` 仍返 `{"status":"ok"}`（它只报配置、不探后端）→ **判定链路要发一次真实业务请求**（如 `POST /v1/embeddings` 看有没有回向量），别拿 `/health` 200 当通。
- 排查顺序：`docker port <容器>`（或 `compose config` 里看的 ports）拿到真实发布地址 → 对比宿主脚本里写的地址 → 改脚本/单元文件（先 `cp -a` 备份）→ `daemon-reload` + `restart` → 真实请求验证。宿主访问**宿主自己**的服务才用 `127.0.0.1`。

## 共享文件夹通路（不挂载方案）

`/vol2/@team/共享文件`（飞牛团队空间，owner root 000，靠 fnOS 私有权限模型放行）——**挂载进容器读不了**（root 000 + trim_acl），走官方 trim-cli 的 `file` 模块（以宿主 `棉棉` 身份执行，实测 `file ls "/vol2/@team/共享文件"` 直接出真实清单）：

```bash
cd /opt/data/skills/productivity/fnos-trim-cli-skill
./bin/trim-cli-linux-x64 --host 172.17.0.1 --port 5666 --scheme ws --allow-insecure-ws \
  file ls "/vol2/@team/共享文件"
```

`file` 模块还带 `mkdir / cp / mv / rename / rm / trash / upload / download-url / compress / extract`（读类已实测；写类首次用前先做一次 mkdir → trash 往返验证）。

**SSH 兜底**（官方通道确实到不了时才用；宿主 1003 身份有权限）：
```bash
# 写
cat <file> | SSH_ASKPASS=/tmp/askpass.sh SSH_ASKPASS_REQUIRE=force setsid ssh 棉棉@172.17.0.1 'cat > /vol2/@team/共享文件/<name>'
# 读
SSH_ASKPASS=/tmp/askpass.sh SSH_ASKPASS_REQUIRE=force setsid ssh 棉棉@172.17.0.1 'cat /vol2/@team/共享文件/<name>'
```
- 团队空间权限模型：`ls` 显示 root 000 不代表没权限，fnOS 用私有 ACL 控制（宿主侧 `sudo -u 棉棉 ls` 实测为准）
- 判断能不能走挂载：owner 组权限能覆盖（gid 匹配）→ 挂载+chmod 组权限；owner root 000 → 走 trim-cli `file` 模块（SSH 兜底）

## 备份自动清理（每周一次，防备份越堆越多）

脚本：`/opt/data/scripts/backup_prune.py`（宿主 `/vol1/1000/<USER>`）
- 识别：`*.bak*` + `*.good.*`（后者是 Hermes 自己写的 `config.yaml.good.<时间戳>`），按「同目录 + 标记前的主名」**分家族**（如 184 个 `gateway_state.json.bak.<epoch>` 算一个家族）
- 策略：**每家族只留最新 5 个**（`--keep N` 可改）→ 备份总量天然有界
- 删除走**官方标准工具 trash-cli**（freedesktop 回收站规范），**不自建 .trash 目录**：
  - `trash-put` → `$XDG_DATA_HOME/Trash`（脚本固定 `XDG_DATA_HOME=/opt/data/.local/share`，避免 cron 里 HOME 不同导致回收站飘走）
  - `trash-empty 7` 清 7 天前内容；恢复用 `trash-restore`，查看用 `trash-list`
  - 装法（**无需 root、无需 chown**）：`HOME=/opt/data/home uv tool install trash-cli --index https://pypi.tuna.tsinghua.edu.cn/simple` → 落在 `/opt/data/home/.local/bin/`
- cron：job `cb2171d492f5`「备份清理（每周日谷电时段）」= `30 4 * * 0`，`no_agent` + 脚本；stdout 为空就不发消息，非零退出才报警
- 手动：`--dry-run` 预览；`--verbose` 明细；`--keep N` 改保留数

**硬规矩（主人明确要求过，别再犯）**：

> **开关 / 参数类建议先查权威来源再给结论**：主人问「这个要不要开」时，先查维护者说法、官方文档、issue 区实证，**再**给结论——不要凭面板印象或直觉推荐。实测有功能维护者直接回「暂不可用」，还有「开了会崩 / 登不上」的 issue；凭印象推荐就翻车。
>
> **查证顺序（常驻规则）**：**源码／官方文档／网搜 → 才轮到实测**。**问「某能力到底有没有」时，先在本机源码里找证据再开口**：插件目录 `/opt/hermes/plugins/`、`hermes_cli/tools_config.py` 的 `CONFIGURABLE_TOOLSETS`、`tools/` 下的实现、`hermes tools list` 里的 `✗ disabled` 行。外部模型或旧文档给的 Hermes 架构结论经常是错的或过期的（实测：一份断言「profile 之间没有第一方派活机制、只能靠 tmux」的分析，被本机 `plugins/platforms/a2a/` 与 `tools/bot_mode_dm.py` 当场证伪，而它引用的 issue 号根本无从核实）——**别在别人的结论上直接搭方案，先验一次真伪**。查询成本远低于测试，别把「跑一次试试」当默认手段。
>
> **第三方（含另一个 AI）给的仓库名/模型 id 对不上时，先搜再宣判**：这类材料里的名字**经常缺组织名前缀，或者干脆是编的**——直查 `api.github.com/repos/<名>` 返 404 只说明**这个写法**没命中，不代表东西不存在；先按关键词**搜**（`/search/repositories?q=<片段>`）再下结论（实测：一份材料把某 4789★ 项目写成裸名 → 直查 404、一搜就出来；同一份材料里另一个模型在 HuggingFace 上则确实查不到）。**通道事实**：容器内 GitHub API 直连可通（用 `curl`，`gh` 未必装）；**HuggingFace 直连不通**（要宿主代理或镜像），别把「连不上」当成「不存在」。分界要认清：源码能定「机制是什么、为什么」（占九成）；**「在本机是否真生效、真实数字是多少」只能实测**（例：某 profile 记忆压根没落库、本地 Hindsight 实例的 reflect 返 500、收窄后 token 降幅）。每类问题花一次实测即可，别反复烧。给结论时标明依据层级（源码／实测／社区），社区来源还要核对版本与环境是否对得上。

1. **先搜 Skills Hub 再动手**：新任务先 `hermes skills search <关键词>` → 再搜网上 → 确认都没有才自己写。删除/回收这类基础能力尤其先搜——Hub 里本来就有现成的（如 `trash-cli`），自建属于造轮子。
2. **chown 要有授权、且必须留台账**：先分类——**确认文件不属于主人**（Hermes 自身缓存/运行态、棉棉写的文档）才可 `chown 1003:1001`，改完登记进 `/opt/data/ops-changelog/ownership-changes.md`；**属于主人的数据不动**（如 `backups/` 里主人自己的备份），只报告。能不动属主就优先走组权限，或新建一个自己拥有的子目录（fnOS ACL 私有，见上文 trim_acl 坑）。
   - 历史教训：曾为让 `backups`「可写」把父目录 chown 成 hermes(1003:1001)——属主一改就可能影响原应用与 fnOS 权限模型，已还原 990:901/755；真正需要写的只是**新建的** `backups/config`，归 hermes 合理。
   - 本例真正需要写的只有 `backups/config`（Hermes 写 `config.yaml.good.*` 用）——那是**新建**目录，归 hermes 合理；父目录保持原样，配置备份照常工作（实测无 "Could not back up" 告警）
3. 保留策略是「家族最新 5 个」→ **回滚点认最新的那个备份**（旧的可能已被收走，回收站里还能捞 7 天）
4. 真正会长成堆的是「每次状态变更就留一份带时间戳快照」的家族（gateway 状态、配置备份）——按家族限量正好治这类
5. 脚本曾把"尝试数"当"成功数"报假成功——现已按成功数统计、失败即 exit 1（**排查这类脚本一定要看退出码，且别用管道后的 `$?`：`cmd | head` 的退出码是 `head` 的——实测因此把「不可写」报成「可写」；要判成败就不接管道，或用 `PIPESTATUS`**）
6. 数据类备份（Hindsight `export_*.zip`）**不在自动清理范围**，删前问主人

## 长跑任务：别前台盯着，挂出去 + 留一个「稍后复查」

主人明确要求过：耗时几分钟到几十分钟的修复/重跑，**不要在会话里轮询盯着**，也别让他等（原话意图：「就不要盯着了，晚点再看有没有出错」）。两步：

1. **现在把活挂出去**：`terminal(command=..., background=true, notify=true)`（前台 `nohup`/`setsid` 会被运行时的安全包装拦下，别硬来）。脚本自身要把进度**落盘**（状态 JSON / 日志文件），别只靠 stdout——进程没了 stdout 就没了。
   - 之所以敢挂：**很多「提交型」操作只是入队，真正的活在服务端进程干**（如 Hindsight `POST /operations/{id}/retry` 只入队，worker 自己跑）。这类活即使本地跟踪进程中断，**已入队的任务照样跑完**——所以挂出去是安全的，不必为了「跑完」而让本地进程常驻。
2. **稍后用一个一次性 cron 复查**：`no_agent` + `script`（脚本无输出就不投递），`schedule: "in 2h"`，`deliver: origin` 把结果发回原对话。cron 调度跑在 gateway 进程里，**比会话/子代理长寿**，这才是「晚点再看」的正确载体。

别用「我待会儿再查」这类**没有载体**的承诺——会话一结束就没了。

## 复核「某个修复到底生效没有」（回报前必须做，不许复述登记文档）

被问「XX 修好了没有」时，**补丁登记文件、自己上一轮的结论、子代理自述都只是线索，不是答案**。三路独立证据各自跑一遍，三路都过才敢报「已修好」：

1. **源码实况**（真身，不是备份、不是打补丁的脚本）：SSH 进容器 `grep -n` / `sed -n 'A,Bp'` 看改动行还在不在。⚠️ 多行签名（`async def call(\n  self, context…`）用 `grep -n "def call(self, context"` 会静默漏掉 → 多行签名一律 `sed -n` 取行段。
2. **修复时刻之后的日志**：`docker logs --since <修复时刻> <容器> | grep -c "<原始错误签名>"` 期望 **0**；同时 `grep -n "started"` 确认**重启时刻晚于补丁落盘时刻**（否则跑的仍是旧代码）。`--tail N` 够不到几小时前的启动行，别拿它下否定结论。
3. **线上真实业务（最硬的一路）**：找一条「这条链路修好了必然会留下的下游痕迹」。例：AstrBot 的 MCP 工具链修好后 `hermes_memory` 每轮自动 retain 就会持续产出 → 查 `GET /v1/default/banks/<bank>/documents?limit=6` 的 `created_at` 晚于修复时刻、`metadata.via=astrbot`，且 `/operations` 对应条目 `status=completed` + `error_message=null`。**工具返回和文档自述都不算证据，下游落库才算。**

回报时把「修复前复现 → 修复后通过 → 上线时刻 → 三路复核」分开写，并标明哪些是实测、哪些是复核时新跑的。顺手提示「补丁在 `/AstrBot/`（非挂载卷）→ 重建/升级即丢」这个遗留风险。

## 改动节拍与验证纪律（改代码/配置时的固定做法）

主人对「一堆重复回执」有明确反应（原话意图：「最近怎么这么多重复回执」）——那不是系统重复投递，
是**改动期间把全量回归跑了六七轮、每轮完成都弹一条通知**。固定做法：

- **小改只跑目标单测**：`python -m unittest <单个 check_*.py>` 走**前台**（几秒、无声、结果就在眼前）；
  全量 `unittest discover -s . -t . -p 'check_*.py'`（几十秒）**只在交付前跑一次**。
- **后台 + `notify=true` 只给真长任务**（分钟级 / 需要挂着等）：通知是给「我不知道它跑完没有」的场景用的，
  短测试用它等于把噪音推给主人。中间态跑完也**别再逐条解释**，只在给结论时回一句。
- **回归红了先怀疑环境污染，再怀疑代码**：同一条命令干净环境下全绿、在你这儿全红，八成是 shell 里遗留的 env
  （实测：`export HERMES_HOME=<真 profile>` 会让「记忆隔离硬前置」的判据翻转 → 用例全红、代码没错）
  → `env -u HERMES_HOME …` 重跑对比，别急着改代码。
- **单测不许碰真数据**：夹具里把 `HERMES_HOME` 指到临时目录（`tearDown` 还原），并把每个 `*_dir` / `*_path`
  配置键（库、群窗口、游标、状态文件）都指到临时目录。踩过两次：用例把 `/tmp` 假图写进了真表情库索引；
  测试的假账号/假群号写进真游标文件，于是**线上每次启动都去查不存在的会话**、刷一屏重试警告。
- **改「行为分支」前先 grep 测试树里钉着它的用例**：把只在群聊生效的条件（`if is_group and …`）顺手删成无条件，
  会静默改掉一条早已定案的行为（本机：私聊里「不许拿静默标记挡话」正是被一条用例钉着的），全量回归立刻变红。
  改法：先 `grep -rn "<关键函数/常量>" tests/` 看有没有人钉着，再决定动不动；被钉着就还原，并把「为什么这条不能在私聊里也生效」写进注释，而不是只回退代码。
- **静默标记（`[SILENT]` 一类）是 Hermes 的通用机制，不是适配器的**：她在某个**人回合**上只回静默标记时，
  Hermes 会把正文换成一段 ⚠️ 提示文案（`gateway/run_turn.py` 的 `_UNEXPECTED_SILENCE_REPLY`，日志记
  `silence marker rejected on a user turn`）——这就是主人突然收到一条“机器人式告警”的来源，先认出来再改。
  要让主人看不到它：在适配器**出站层按特征串吞掉**（别依赖 emoji 前缀，emoji 在日志里容易被吃），
  但**只计数 + 记一行日志**，不做成黑洞（否则以后真“该答不答”就查无此事）；机器回合
  （`internal=True` 的补投/主动开口）要在源头就标成机器回合。
- **批量改多个文件的脚本：每个文件用独立变量名 + 写前断言 + 写完复核**。教训形状：脚本里 `s` 先后装两个文件的内容，
  最后 `p.write_text(s)` 把 B 的路径写成了 A 的内容 → **B 文件被静默覆盖**，而脚本自己还打了「成功」。
  落地：改完立即 `ast.parse` 语法检查 + `grep` 确认新函数/新行真在目标文件里，再往下走。

## 验证清单（重建/挂载后）
```bash
# 容器内
ls /opt/data-backup/ | head          # 备份挂载可读
curl -s http://172.17.0.1:16003/json/version | head -c 100   # socat 通路
pgrep -f 'bin/hermes gatewa[y]' && echo GATEWAY-UP
python3 -c "import json; print(json.load(open('/opt/data/gateway_state.json'))['platforms']['qqbot']['state'])"
# 宿主侧
sudo docker ps --format '{{.Names}} {{.Status}}' | grep hermes
```

## 健康自检：一条命令（`python3 /opt/data/scripts/health_all.py`）

覆盖：双侧 gateway / A2A 9900·9901 / Hindsight 8888 / dashboard / AstrBot 容器·两个 provider·MCP·适配器·六个 hermes_* 插件的**配置值**·补丁在位·md5 同步。约 20 秒（项数随检查项增删变化，别把数字当固定值），异常时告警（不是只写日志）：**主通道 = magicpush（容器 `:818`，`POST /api/push/<token>` → 主人 iPhone），失败回落 `hermes send` 投 QQ 私聊**，两条链路都实测过。⚠️ 曾经的 `POST :8098/report` 是已退役的 AstrBot 插件 `hermes_report`，**别再往那儿接**（端口已作他用、插件已不在，接了必然失败）。
**适配器自己的运行告警也走 magicpush（别再让它默默只落本地日志）**：平台适配器默认 `alert_report_url` 指向那个已退役的
`:8098/report`，实际后果是每次告警只写本地 `onebot-alerts.log` + 一行 `alert delivery failed`（看着像已经告警了，其实没人收到）。
正确配置（`platforms.<平台>.extra`）：`alert_magicpush: true` + `alert_report_url: http://172.17.0.1:818/api/push`
+ `alert_token_file: /opt/data/secrets/magicpush.token`（token 走 URL 路径、不进配置文件；magicpush 体是 `{title, content, type}`）。
改完用**假接收端**端到端验一次（真发真收、逐项断言 URL/体形状），别只靠单测。通道细节与凭据保管见 `references/alerting-design-rules.md` 十一/十二节（「接口必须绑了渠道才能推，否则 400」与 token 只落 0700 文件两条必读），阈值该怎么分阶段见同文件十三节。

- **官方入口优先**（脚本只封装解析，不重写）：`hermes gateway list`（PID）、`hermes doctor`（s6 2/2、hindsight provider）、`docker exec astrbot python3 /opt/astrbot-patches/verify_patch.py`（补丁，exit 0）、`GET :8098/health`（插件活体）。脚本只补官方**没有**的：端点真实可达性、AstrBot 运行态读数、插件配置值核对、md5 一致性。
- 端口真相：dashboard 在 **9119**（host 网络后不再有 19119），gateway api_server 8642/8643 返 **401** 就是活的（要鉴权）。
- `--selftest` 把真实读数逐项改坏，验证检查器**真的会报 fail**（11/11，不动线上）；改动判据后先跑它，别让自检退化成橡皮图章。
- 检 `hermes_report`/`hermes_forward` 的 md5 无意义：它们是 compose 直接挂载（源码副本=加载副本，同一文件）。
- 要「突然坏了能马上知道」需挂 cron（`no_agent` + `script`，无输出即静默）。**告警策略不需要问主人**：他把这件事划给棉棉自己把握（原话「这个你随便吧，反正这是你自己的检查」），只要求**分级**——能自愈的不吵人，需要人干预的才推送（全都告警 = 没告警）。
- **告警去重与「刚启动宽限期」已固化成规矩**：手动/循环调用走 `health_all.py` 的签名去重闸门（同一签名未恢复前只发一次；`HEALTH_ALERT_COOLDOWN_S` 调冷却、`HEALTH_ALERT_STATE` 指定状态文件），cron 路径走 `health_cron.py` 的同签名 8 轮（≈4h）再提醒；两个回归脚本 `scripts/check_health_dedup.py`（8 项断言）/ `scripts/check_health_cron_dedup.py`（6 项）**改完必跑**。「对端会自己重连」的判据必须带启动宽限期（见 `references/alerting-design-rules.md` 七/八/九节）。

## 消息网关（gateway）接入与运维（原 hermes-gateway-ops）

gateway 是消息平台连接器，也是 **cron 调度器、kanban dispatcher、api_server 的宿主进程**（日志 `In-process cron scheduler started`）——**gateway 停 = cron 停**。「cron 不触发 / 平台没反应」的头号根因就是 gateway 没起，先 `hermes gateway list` 再查别的。

- **默认不启动**：容器里 `gateway_state.json` 不存在时 gateway 从未拉起（只有 dashboard/TUI 在线）。`hermes gateway list` / `hermes profile list` 看状态。
- **配置改动需重启 gateway 进程才生效**（adapter 启动时读取配置）。适配器源码 `/opt/hermes/gateway/platforms/`，配置键以源码为准（README 头注释常有示例）。
- **别从对话会话里重启 gateway 自身**：会话跑在 gateway 进程内，停止/重启类命令会被运行时拦下（就算绕过，SIGTERM 也会先杀死承载命令的自己，停在新旧都不运行的中间态）。正确姿势：**配置先写盘**（`hermes config set`，原子写入，不依赖重启），然后让主人在**外部 shell**跑 `hermes gateway restart`；需要自己动手时走**宿主侧 detached**（`nohup setsid`）而不是容器内的前台命令。写盘后若想先验证，用**另起一个 python 进程**加载磁盘配置直接跑真实代码路径（配方：`local-llm-ops/references/hermes-image-routing.md` 的「不重启 gateway 也能先验证」），不要为了验证去重启。
- **重启「另一个 profile 的门」是安全的**（它不承载当前会话）：`/run/service/` 下的槽名 = 权威（本机 `gateway-default` = 默认门、`gateway-chat` = chat 门），`/command/s6-svc -r /run/service/gateway-<x>` 即可，实测 20 秒内回 `up`。**只剩「重启承载当前会话的那道门」没法在会话内做**——要动它按这个配方：① 配置先写盘 ② 用 `terminal(background=true)` 挂一个延迟脚本（运行时会拦 `nohup`/`setsid` 包装，别硬来）③ 脚本先 `sleep` 留出取消窗口（轮询一个标志文件；主人说「先别重启」就创建它，脚本退出）④ 再 `s6-svc -r` → `sleep` 后读 `s6-svstat`，若 `down` 立刻 `s6-svc -u` 兜底（对 exitcode 78 有效）⑤ 全过程写日志文件（会话一断 stdout 就没了）⑥ 把宿主兜底一行也交给主人：`docker exec hermes /command/s6-svc -u /run/service/gateway-<x>`。
- ⚠️ **容器里 `hermes` 不在 PATH**：`hermes: command not found` 会让 `hermes … | grep …` 这类检查**静默假阴性**（被误判成「没有这个能力/没有这条日志」）。一律用 `/opt/hermes/bin/hermes`，或先 `export PATH=/opt/hermes/bin:$PATH`；拿不准落点先 `hermes config path`。
- **启动（容器内无 systemd）**：`hermes gateway run --no-supervise --force -v`；不加 `--no-supervise` 会走 s6，槽未注册时报 `no such gateway 'default'` → 先跑 `/opt/data/scripts/gateway_s6_migrate.sh`（脚本内 s6-svstat 必须用绝对路径）注册槽再拉。日志确认在线：`Access token refreshed → WebSocket connected → Identify sent → ✓ qqbot connected`。
- **持久化**：会话级进程会被清理，别把「gateway 在跑」当持久状态。写 `gateway_state.json` = `{"gateway_state":"running"}`（官方路径 `$HERMES_HOME/profiles/<name>/`，本机根 `$HERMES_HOME` 亦实测有效）→ 网关模式容器下次启动 02-reconcile-profiles 会注册 s6 槽并拉起；⚠️ **dashboard 模式容器例外**（container_boot 检测到 dashboard 角色跳过 reconcile）→ 重建后 gateway **不会**自动拉起，必须手动恢复（见「重建后恢复清单」第 1 条）。
- **凭证写入**：`hermes config set platforms.<name>.extra.<key> <value>`（官方 CLI 原子写入，嵌套键支持），env 类放 `/opt/data/.env`（600）。**全程不打印密钥值**：`hermes config get` 会回显完整值，验证输出注意脱敏。⚠️ **别指望 `profiles/<p>/.env` 的变量出现在 agent 子进程的 env 里**（实测：`.env` 里有 `DEEPSEEK_API_KEY`，她 terminal 子进程的 `env` 里没有）→ 要给自己人的封装脚本传配置，就让脚本自己 `setdefault` 默认值、**一处为准**；往 `.env` 里加一份「以为会继承」的变量是双份真相，发现就撤。跨平台键位映射（OpenClaw → Hermes）见 `references/platform-credentials-migration.md`。
- **QQ 安全策略**：`dm_policy` = open | allowlist | pairing（默认 pairing）；**open 要「两层同时开」才真生效**：① 平台层 `dm_policy: open` + `allow_from: ['*']`；② **网关层还有一道 allowlist 闸**——没配任何 allowlist 时启动日志会打 `No env user allowlists configured … will deny unknown senders` 并**默认拒收未知来者**；要放开必须在 `profiles/<p>/.env`（600）里写 **`GATEWAY_ALLOW_ALL_USERS=true`**，写后重启该门，**启动日志里那句 deny-unknown 消失才算生效**（只看平台侧配置会以为开了、其实陌生人还是进不来）。⚠️ 键名以网关自己打印的名字为准，早先技能里写的 `QQ_ALLOW_ALL_USERS` 是错的。**放开前先算钱**：私聊 open = 任何陌生人来一句就起一轮付费回合；主人的口径是「全开，但陌生人按群聊生人档对待」——信任档按 uid 在**适配器/工具层**分（含群内一律生人档、群里不吐私聊），别只写进提示词；拿到主人 openid（要用户先发一条消息才拿得到）后收紧为 `dm_policy: allowlist` + `allow_from: [<openid>]`；`group_policy: disabled` 可关群聊。
- **投递与验证**：`hermes send --list`（平台已知目标）、`hermes send -t qqbot:<chat_id> "msg"`、`hermes cron list`（scheduler 在 gateway 里）；建一个 `no_agent` + `schedule: 1m` 的脚本 job，到点看 `/opt/data/cron/output/<job_id>/` 有输出 = cron 链路通。⚠️ `hermes send` 与 cron 投递都是**出站**：消息会以 **`assistant` 角色**镜像进目标会话（`--json` 的 `mirrored: true`），下一轮读历史能“看到”、但**不触发一轮、也不被当对方的指令**——要让对面动起来只能走真实入站消息 / A2A / api_server（三方向分界见 `references/home-channel-and-cron-delivery.md`）
- **平台线「静默掉线」先查 DNS，别只盯 WS**：Hermes 出站请求过 `tools/url_safety.py` 的 SSRF 防护，它**自己先解析域名**，失败即抛 `SSRFConnectionBlocked: Blocked request - DNS resolution failed for: <host>`；adapter 侧只看到 `send retry N/3`，**日志里没有任何 WS 关闭/重连记录**，看起来像「无缘无故断线」。容器 `/etc/resolv.conf` 只有路由器一个 nameserver（宿主跑 mihomo 时典型）→ 宿主 DNS 抖一下整条线就被掐。修法：容器 resolv.conf 补公共 nameserver（`223.5.5.5`/`119.29.29.29`），并写进宿主 `/etc/resolvconf/resolv.conf.d/tail` 让 DHCP 重下发也不丢。
- **QQ 取 access token 的端点是 `https://bots.qq.com/app/getAppAccessToken`**（凭据在 `config.yaml` 的 `platforms.qqbot.extra`，`hermes send`/探针都从这儿读）；`api.sgroup.qq.com/app/getAppAccessToken` 实测 **404** —— 写探针别用错，否则把「端点错」误判成「凭证坏」。
- **线健康探针做成一次性 `no_agent` cron 脚本**（本机 `scripts/qq_line_watchdog.sh`，每 5 分钟）：① 解析平台域名 ② 取 token ③ 读日志判断 WS 是否静默超时 → **只在「正常→异常」翻转时输出**（no_agent 无输出即静默，不打扰主人），静默超时自动 `s6-svc -r` 那道门。脚本读凭据**只打印布尔/HTTP 码，不打印值**。
- **坑**：无凭证平台会拖慢启动并刷错误日志（weixin 报 `WEIXIN_TOKEN is required`、dingtalk 报 config validation failed）→ config.yaml 里 `enabled: false` 关掉；改了 dm_policy 等策略不重启不生效；QQ 收不到消息先查开放平台侧可对话名单 + dm_policy + gateway 日志连接状态；旧框架时代的 c2c/会话 ID 不一定能被新框架 send 解析（openid 是「用户+应用」维度，实测为准）。

## profile 级成本/工具收窄（sidecar 门、任何新 profile）

新建 profile 默认是「全量工具 + 没接记忆」的胖配置，给聊天/专用门用之前先收一遍。三条硬事实：

1. **`memory.provider` 默认是空** → 记忆引擎完全不工作（无工具、无自动召回、无落库）。要记忆就 `hermes -p <p> config set memory.provider hindsight`，并按 `references/chat-layer-segmentation-and-cost.md` 写 `<HERMES_HOME>/hindsight/config.json`（profile-scoped；不写则回退 env 默认 `mode=cloud/bank=hermes`，对本地实例等于没接）。
2. **`tools.tool_search.enabled: auto` 在源码里就是 `on`** → 它把工具藏进目录、只留 `tool_search/tool_call`，模型批量取会被「本地工具必须一条一个」拒，然后反复重试烧 token（实测单条消息 33 万 prompt 的真凶）。不是真需要就用 `off`。
3. **`platform_toolsets.<platform>` 收窄是成本与安全同一把刀**：默认 14 套工具全开；输入来自 IM（群/陌生人）的 profile 尤其要收，否则一句群消息就可能走到终端/写文件。**关键细节：某个平台没有这个键 ≠ 该平台没工具，而是回落成整套 core tools（含 `terminal`/`execute_code`/`browser`）**——实测发现聊天门新接的平台就是这样，而人格里还写着「你没有 shell」= 提示词说一套、手里有一套（最容易出事的组合）。所以：**每接一个平台就显式写一份列表，并读回配置文件核对真落盘**：
   ```
   hermes -p <profile> config set platform_toolsets.<平台> \
     '["a2a","cronjob","file","memory","session_search","skills","vision","web"]' --force
   # 然后读 profiles/<p>/config.yaml 确认列表在盘上（该键每轮从盘上读 → 免重启即生效）
   ```
   验证不止看配置：让 agent 自报一次工具面（或看它这一轮的工具列表日志），确认数与内容都等于列表。**要机器复核就跑 `scripts/dump_platform_tools.py`**（`hermes tools list` / `--summary` 都要求交互终端，管道里直接报 `requires an interactive terminal`）——注意 `_get_platform_tools()` 返回的是**工具集名**不是工具名，必须再用 `toolsets.py::TOOLSETS` 展开（`tools` + `includes` 递归），插件工具集（`a2a` 等）不在 TOOLSETS 里、要从插件的 `register_tool` 读；适配器插件的**行为**（发消息/分段/群管理）不是 agent 可调工具，别当「她有」。配方与「拿清单写提示词」的读法见 `references/tool-surface-inventory.md`。
4. **收窄会连带掐掉「派活」**：一次性子代理**继承父级工具面、拿不到父级没有的工具**（`tools/delegate_tool_toolsets.py`）→ 只剩看图/记忆的 profile 派出去的子代理也只剩看图，等于派不动活。要真跨门派活得另配通路——`a2a`（官方 A2A 插件，**默认关；没配对端则工具不出现**，此时人设里写「走 agent 间通话」必然变成幻觉）或前端插件层路由。工具名、入参与同步等待的坑见 `references/chat-layer-segmentation-and-cost.md`，**两条第一方通路的区别与完整启用/验证流程见 `references/multi-profile-a2a-dispatch.md`**（A2A = 协议级、与桌面无关；Bot Mode `message_agent` = 会话级、schema 只注入 canonical Bot Chat，headless 普通会话拿不到；跨机还有 `hermes peer dm`）。

5. **给某个门加技能：放 `profiles/<p>/skills/<类目>/<技能名>/`**（该门的技能搜索目录 = `profiles/<p>/skills` + `shared-skills`，用 `agent.skill_utils.get_all_skills_dirs()` 实算，别猜）。四条注意：① **带大二进制的技能只拷文档，副本里 `bin/`、跨平台 wrapper 一律不放**——软链指回真身目录看着省空间，实则多一个悬空断点（真身被搬/改名、技能库整合就断），而那道门真正跑的入口是本机绝对路径的封装，副本的 bin 根本用不上（实测副本自带 wrapper 连不上：缺连接参数，报 `saved session is required`）；② 照搬后把副本 SKILL.md 里「用 `./scripts/xxx`」这类入口改写成**本机入口**（如 `python3 /opt/data/scripts/trim_cli.py …`）并注明本副本无 wrapper/二进制，否则她照文档跑必报错；③ 搬完用 `rglob("SKILL.md")` 数一遍确认搜索目录命中、且 `find <技能根> -type l` 为 0，别只看文件拷过去了；④ 技能里引用的工具名必须是**那扇门真实有的**（见 `references/tool-surface-inventory.md` 末节），否则技能是废的。

**诊断顺序**（不要凭感觉调）：`/v1/toolsets` 看现在到底暴露了什么（响应键是 `data`）→ 建量尺（「在吗」探结构成本，「我们昨天在弄什么」探记忆/工具回路）→ 改一项测一次 → 改完重启该 profile 容器再复核。三项支点键都可用 `hermes -p <p> config set <key> <value>` 写（`platform_toolsets` 会报「unrecognized key」但照写有效，回读确认即可）。

## 容器数据落盘约定：`stack/` 与 `chat-layer/` 分层（主人 2026-09-20 要求）

映射目录里要能一眼看出**哪个目录是谁的**：

- `<映射目录>/stack/<服务>/…` = **容器运行时数据**（`napcat/{config,ntqq}`、`astrbot/data`、`hindsight/`…）——删容器不删数据，迁移与备份只动这一块。
- `<映射目录>/chat-layer/…` = **我们的代码与文档**（compose、启动脚本、PLAN、插件源码 `plugin/hermes_forward`、`stickers`）——插件与素材照旧 bind 进容器，但归位在代码层。
- 归属判据：`docker inspect <容器> --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}'` 输出里**不该有 `/var/lib/docker/volumes/…`**；有就是没映射出来的数据卷（本机 Hindsight 就是这样一块，6.8G）。
- 迁移顺序：停容器 → `mv` 数据（属主保持 1003:1001 不变）→ `docker compose config -q` 验语法 → `up -d` → 验链路。改挂载路径会触发**重建**；协议端（NapCat）只要 `ACCOUNT` 环境变量写对了，重建/重启**都免扫码**（2026-09-20 实测），票据过期才需重扫——仍建议挑主人在线时做，备好 `references/im-channels-and-frontends.md` 里的二维码取图流程。
- **数据卷迁出（docker volume → bind mount）可复用配方**：记下数据属主（`stat -c '%u:%g' <卷>/_data/<文件>`，**是数据自己的属主，不是 1003**——Hindsight 是 1000:1000）→ 停容器 → 用**镜像自身**当搬运工具（免宿主权限纠缠）：`docker run --rm --user 0 --entrypoint sh -v <卷>:/from:ro -v <目标>:/to <镜像> -c "cp -a /from/. /to/ && chown -R <uid>:<gid> /to"` → **双向校验 `du -sh` + `find|wc -l` 两侧一致** → 写 compose（env 用 `docker inspect --format '{{json .Config.Env}}'` 原样搬 + `network_mode: host`）→ `docker rm -f` 旧容器 → `up -d` → **验业务**（端口 `/health` 之外，要看业务端点：Hindsight 的 `/v1/default/banks` fact_count、再从 Hermes 真发一次 recall）→ 全过才删旧卷。
- ⚠️ **用 patch 改 compose 的某个属性时，`old_string` 只圈到那一行**：曾把相邻的 `network_mode: host` 一起吞掉（服务靠它才与主容器同宿主网络）→ 改完必须 `docker compose config -q` + 回看服务段关键键（`network_mode`/端口/`restart`）是否还在。
- ⚠️ **`stack/*/` 目录是 root 属主（755），容器内改不了里面「新建」的文件**：文件本身常归 hermes（可直接改内容），但原子写工具要在同目录建临时文件 → `Permission denied`（整个写入失败，不是半成功）。做法：**在可写目录（如 `/opt/data/tmp/`）生成新版本 → `docker compose config -q` 校验 → 再覆盖真身**：容器侧先落到 `/opt/data/tmp/`，宿主侧 `sudo -A cp` 覆盖（SSH，`SUDO_ASKPASS=/vol1/1000/<USER>`）。校验别用本机 `pyyaml`（未必装）。事后 `diff`/`grep` 回看，确认只多/少预期的那几行。
   - **若文件本身已属 hermes，最省事的是就地写、连 sudo 都不用**（目录不可写但文件可写时 `open(path,'w')` 是允许的）：同目录 `cp` 做备份同样会被拒 → 备份落 `/opt/data/tmp/`（**带 md5 记下**），改动用一个**带前置断言**的小 python 覆盖真身（断言：当前内容 md5 == 备份 md5、待替换片段命中数 == 1、改后关键行仍在），改完 `diff` 回看。代价是丢了原子性，用断言补偿。
- 备份口径（主人定）：**改动验证通过后立刻删掉临时备份**，不留长期副本（长期备份另由周清理 cron 管）。

## 本机推理栈连带故障：记忆写入（Hindsight retain）失败怎么查

症状：`hindsight_retain` 报 `Failed to store memory: `（错讯为空），或 recall 正常但写入一直不落库。**工具返回不是证据**（同步等待超时也会报失败，而任务其实已入队）→ **读目标库才算证据**。

**那个「空错误」的根因（已定位、已修）**：Hermes 插件的工具入口 `_tool_retain` 调 `_retain_batch` 时**漏传 `retain_async`** → 客户端默认 `False` → **同步**等整篇抽取完成；本机单批抽取 1300+ 秒，远超 `HINDSIGHT_TIMEOUT=180` → `_run_sync` 的 `future.result(timeout=…)` 抛裸 `TimeoutError`（无参数 → `str(e)` 为空串）→ 工具层 `f"{failure}: {e}"` 拼出那句空错误。**判据**：同一个文件里别的调用点是带 `retain_async` 的，只有工具入口漏了。

- **修法**：宿主侧 `SUDO_ASKPASS=/vol1/1000/<USER> sudo -A docker exec hermes bash /opt/data/scripts/hermes_patch_hindsight_retain_async.sh`（幂等；自带锚点断言 + `py_compile` + 失败回滚；旧文件备份到 `framework-patches/`）→ **重启那道门**（模块在网关进程启动时加载）。
- ⚠️ **agent 的写工具碰不到 `/opt/hermes`**（`HERMES_WRITE_SAFE_ROOT=/opt/data` 拦下，且该文件 root 所有）→ 只能走宿主 `docker exec`。**补丁活在镜像层：容器重建即丢、且静默退化** → 打完把那条判据同步进 skill `hermes-persistence-check` 的「镜像层源码补丁清单」，重建后照单重打。
- **别把「慢」读成「坏」**：修完之后 `retain` 是**入队 + 后台抽取**（毫秒返回），刚写完的事实要等队列消化才召回得到 —— 「刚聊过的它记不住」先看 pending 积压，别急着怀疑数据。

1. **判是否真写进去**：`curl -s --noproxy '*' "http://127.0.0.1:8888/v1/default/banks/mianmian-history/documents?limit=3"` 看最新 `created_at` 有没变（比 `memories/list` 直观）。
2. **看任务卡在哪**：`docker logs hindsight --since 10m | grep -aiE 'op=|WORKER_TASK|STUCK'` → `stage=llm.openai.retain_extract_facts.attempt=N/4`、`payload_null=`、`pending=` 是否堆积。
3. **看抽取后端为什么供不上**：`docker logs llama-extract | grep -aiE 'error|cuda|exceeds the available context'`。两条典型：`HTTP 503 {"message": "Loading model"}` = 抽取容器刚重启在重载模型，**这期间所有抽取都失败**（Hindsight 报 `1/1 chunks failed`）；`request (NNNNN tokens) exceeds the available context size (8192 tokens)` = 单批请求超过抽库 ctx，**硬失败**且重试到 4/4。
4. **看后端本身**：`docker inspect llama-extract --format 'restarts={{.RestartCount}} started={{.State.StartedAt}}'`；再用**小请求计时**（16 token + `chat_template_kwargs.enable_thinking:false`）判饱和——小请求也超时就是后端在加载/饱和，不是 payload 的错。`docker stats` 的内存数含 mmap，别当真实占用。
- **收尾**：失败任务可 `GET /operations` → `POST /operations/{id}/retry`；**别反复重发同一条**（只会再叠一份进队列）。**「僵尸态」要两条同时成立才算**：`processing` 且 `updated_at` 停在很久以前 **且** 近期 `/llm-requests` 一条 `success` 都没有——长抽取期间那行根本不写、`updated_at` 可以几十分钟纹丝不动，而子调用还在 50~500s 一次次成功（实测），只凭 `updated_at` 或 poller 的 `[STUCK?]` 下判会**误杀正在干活的 op**。确认是真僵尸才动手救活——主人把「任务一直挂着耗着」当必须解决的故障，但把「正在跑的慢任务」报成故障同样不行。

**可操作结论**：抽库 `llama-extract` 的 ctx 只有 **8192**（mem_limit 2g），而 Hindsight 会发出远超它的 prompt（日志原话 `request (NNNNN tokens) exceeds the available context size (8192 tokens)`）→ 那批抽取**必失败**，与数据/bank 配置无关。**写入失败按这个顺序定位**：① extract 日志里的 ctx 报错 ② ops 的 `error_message`（`503 Loading model` = 后端在重载模型，此刻 retry 必再失败；`502 batch embeddings` = 嵌入链路；`JSONDecodeError` = 8b 输出截断）③ 最后才怀疑数据。⚠️ 修法（调大 ctx 吃显存、或从 Hindsight 侧限制每批输入）**尚未验证**：先核 GPU 余量、把代价交主人拍板，不要直接改。队列体检用 `scripts/ops_triage.py`（只读）；端点、僵尸判据、错误分类表见 `references/hindsight-queue-triage.md`，**积压口径 / 「卡住何时属预期」的阶段模型 / 心智模型刷新机制见 `references/hindsight-queue-semantics.md`**。（领域细节另在 `hindsight-memory-engine`，但那是主人自有 skill，不可自动改。）

- ⭐⭐ **MCP 默认 bank 是空库 `default`**（源码 `/app/api/hindsight_api/api/mcp.py:36`：`DEFAULT_BANK_ID = os.environ.get("HINDSIGHT_MCP_BANK_ID", "default")`）→ 模型不传 `bank_id` 就会 `recall` 0 条、`get_bank_stats` 报 node=0，看上去像“记忆库是空的”（实际是查错库）。**修法**:把 AstrBot 的 MCP 地址写成 **按 bank 分路径** `http://172.17.0.1:8888/mcp/mianmian-history`（实测：不传 bank 也能召回 8 条 ✓；且 bank 级工具 `list_banks/create_bank/get_bank_stats` 不再暴露 = “0 node”误判自然消失）。彻底的做法是给 Hindsight 容器加 `HINDSIGHT_MCP_BANK_ID=mianmian-history` ——但要**重建容器，需主人确认**，且必须写进重建脚本否则重建即丢。
- **改 AstrBot 插件源码不会自动生效（改完先问「生效了吗」）**：AstrBot 只从 `data/plugins/<插件名>/`（= 宿主 `stack/astrbot/data/plugins/`）加载，**不是** `chat-layer/plugin/<插件名>/`（那是源码副本；只有 compose 显式挂载的 `hermes_forward`/`hermes_report` 例外）；改完必须同步过去并用 md5 双向核对。**热重载默认关着**（源码 `astrbot/core/star/star_manager.py:231` `if os.getenv("ASTRBOT_RELOAD","0")=="1"` 才起 watchfiles），否则插件在启动时 import 一次、之后永远跑旧代码（症状：日志里找不到新的启动行，旧行为照常跑）。本机 compose 已加 `ASTRBOT_RELOAD=1`（2026-09-23），**下次容器重建才带**；在那之前改插件要重启 astrbot 容器才生效——重启聊天门属于主人决定的事，别自己动。
- **写「容器内真框架」探针时，handler 必须按 AstrBot 的方式绑定**：`star_manager` 入库后会把每个 handler 变成 `functools.partial(raw_handler, 插件实例)`；探针若只 `importlib` 加载插件文件、不建实例不绑定，真 `call_event_hook` 会抛 `missing 1 required positional argument: '<第二参>'` —— 那是**探针没绑定**，不是插件签名错，别据此改插件。**同理：另起一个 python 进程去查 star handler 注册表永远是空的**（注册表只活在运行中的 AstrBot 进程里）→ 拿「注册表里没有」当作「钩子没挂」= 假阴性；要验钩子就用在容器内按 AstrBot 方式绑定后真跑 `call_event_hook` 的探针。
- **`astrbot_execute_shell` 等本地工具是「每轮强制注入」的，人格白名单挡不住**（2026-09-23 源码级实测）：`computer_use_runtime: local` 时 `astr_main_agent.py` 的 `_apply_local_env_tools()` 在**人格工具注入之后**无条件 `add_tool` 进来 `astrbot_execute_shell / astrbot_shell_session / astrbot_execute_python / astrbot_file_read|write|edit_tool / astrbot_grep_tool`，还往 `req.system_prompt` 追加一段英文「You have access to the host local environment…」。所以**只改 `personas.tools` 白名单是剪不掉它们的**（实测：白名单 18 项，真轮工具面仍是 24 个）。要收就得在 `@filter.on_llm_request()` 里按白名单 `req.func_tool.remove_tool(name)` 逐个摘，并处理 `req.system_prompt` 里那段英文提示。**处理那段提示要「按本轮真实白名单逐句校正」，别整段删**：整段删会连带删掉「文件工具的相对路径基于 workspace」这类描述（实测：把文件四件套加回白名单后，她拿不到路径规则），而留着不改又与「你没 shell/python」的边界自相矛盾。落地在插件 `hermes_lookup` 的 `enforce_boundary`（可关），改完用一条真轮日志核对「摘掉了几个、剩下的工具面等于白名单」。
- **「人格白名单是闭集」这个维护坑**：`personas.tools` 一填列表，新插件注册的工具**默认她够不着**（每轮被剪掉）→ 每装一个新插件工具都要把名字补进白名单。自检做法：启动后（MCP 是启动后才注册，要延迟 30s 再打一次）把所有注册工具名和人格白名单各打一行日志、把「注册但未列入」的点名，就不用人肉记。

- **改容器内源码前必须查全调用点，且不能信被 head 截断的 grep**：给 `call_local_llm_tool` 的 `context` 形参加 `/`（仅位置）却没同步改唯一调用点（用 `context=` 关键字传）→ **本地工具全线报 `missing argument: 'context'`**（闹钟都定不了），立刻回滚。正确改法：②处一起改 + 用 `inspect.signature().bind(..., context='工具参数')` 做机器自检（不许靠眼看）。完整登记：`/opt/data/chat-layer/astrbot-patches.md`
- **容器内源码补丁要「重建即自愈」，别只写进登记文档**：补丁活在镜像可写层，`compose up --force-recreate` / 换镜像 / 手搓 `docker run` 都会丢，且**静默退化**（不报错、功能没了）。AstrBot 的做法（2026-09-23 落地，可直接照抄）：补丁载荷放**挂载卷** `chat-layer/patches/`（合并版 `patch_astrbot_context.py` 幂等重打 + `verify_patch.py` AST 自检 + `astrbot-entrypoint.sh` 启动先打再 `exec "$@"` 承接镜像原 CMD），compose 里 **`entrypoint` 覆盖 + `./patches:/opt/astrbot-patches:ro`**；再加宿主兜底 `apply-from-host.sh`（容器非 compose 建的时用）。验收必须**跑全新容器模拟重建**（`docker run --rm -v <patches>:/opt/astrbot-patches:ro -e PATCH_ONLY=1 ...`，看 `[VERIFY] 补丁已生效 ✓`）+ 同容器连打两次看幂等，别只看线上容器「现在是好的」。**完整配方、模拟重建的三段验收命令、以及坑（分批补丁脚本必须合并版 / `ast.AsyncFunctionDef` / 模拟容器必须挂 data 目录 / 线上旧容器与新 compose 暂时不一致时不要擅自重建）见 `references/container-source-patch-persistence.md`**
- **AstrBot 本地工具参数与形参重名（两层，缺一层就还炸）**：`retain`/`sync_retain` 自带 `context` 参数 → ① `call_local_llm_tool(context, …)` 形参撞名（PEP 570 `/` 修）② `get_full_tool_set()` 把**每个**工具都包一层 `_PermissionGuardedTool`，其 `call(self, context, **kwargs)` 又撞一次（`MCPTool.call` 同理）→ 症状是 `_PermissionGuardedTool.call() got multiple values for argument 'context'`，再被 `call_local_llm_tool` 的 `except TypeError` 换成一句和真因无关的「Tool handler parameter mismatch … Handler parameters: kwargs: Any」（`params[1:]` 把 `context` 跳掉只剩 `kwargs`）。**修法：两处首参都加 `/`**；完整登记与脚本在 `/opt/data/chat-layer/astrbot-patches.md`（补丁 1+2）。**`sync_retain` 会同步等本地 27B 抽完（必 120s 超时）→ 人格里禁用，只用 `retain`；自动回填走 REST+async=true 不卡。**
- **容器内跑 AstrBot 源码级验证，不必重启服务**：写脚本到宿主 `/vol1/1000/<USER>`（容器内 `/AstrBot/data/mianmian-tmp/`，该目录已挂载可写），再用 `python3 /opt/data/scripts/astr_run.sh <脚本> [参数]` 执行（内部 `docker exec -w /AstrBot -e PYTHONPATH=/AstrBot astrbot python3 …`）。**真类真连接、只借空壳 `FunctionTool` 的 manager / star Context**，就能把 `FunctionToolExecutor.execute → _execute_local → call_local_llm_tool → _PermissionGuardedTool.call → 真 MCP / 真内置工具` 整条链路跑通；样例脚本 `/AstrBot/data/mianmian-tmp/verify_tools.py`。（本轮即用它复现出上面两条报错、又在打补丁后拿到 retain/读图/读文本的真实返回。）

## AstrBot 直连接入层（2026-09-23 落地，聊天门链路）

架构：`QQ ⇄ NapCat ⇄ AstrBot ─┬─ provider 直连 LLM（省掉 ~7k tok/轮 Hermes 系统提示词）
                                   ├─ LLM 工具 delegate_to_hermes → A2A 127.0.0.1:9901
                                   ├─ MCP hindsight → 32 工具
                                   └─ 插件 hermes_memory → 每轮对话自动 retain`

**关键坑（都踩过）**
- ⭐ **persona 的 `tools` 字段是白名单**：`NULL` = 全部工具；`[]` = **一个都不给**。源码 `astr_main_agent.py`：`if (persona and persona.get("tools") is None) or not persona: → get_full_tool_set()`。填 `[]` 会让猫猫「说自己没权限、工具够不着」。`skills` 同处理。
- **开 provider 不会让老插件闭嘴** → 双回复。必须同批「开 provider + 摘 hermes_forward 入口」。停用它别搬目录（`stickers` 是 bind mount，`shutil.move` 报 EBUSY）→ 把 `main.py` 改名 `main.py.disabled`。
- **statuses 表结构**：`id` 是 INTEGER 主键（塞 uuid 报 `datatype mismatch`）、`created_at/updated_at` 是 DATETIME（传 `'YYYY-MM-DD HH:MM:SS'`）、`persona_id` 是逻辑 id；默认 persona 在 `agent_runner.config.persona.persona_id`。
- **钩子签名**：`@filter.on_agent_done()` 收三个参数 `(self, event: AstrMessageEvent, run_context, response: LLMResponse)`；回复文本在 `response.result_chain.get_plain_text()`；群号 `event.get_group_id()`、发言人 `event.get_sender_name()`、`event.unified_msg_origin`。
- **分段方案（主人习惯）**：切分由通道层做：正则 + 清理规则 `content_cleanup_rule="[⁂※⸮]"` → 模型在**要断开的地方**写 `⁂`，分隔符只分隔、**不进消息**。**规则必须只有一条：出现分隔符就切、没有就整段发。不要长度阈值**——曾经用「超 N 字就不切」，实测失效链是「不切 → 也就不清理 → 分隔符原样露在正文里」，主人原话「英文多的时候就老看到分隔符不舒服」。配套四条：① 清理挂在**最后一道出站口**并覆盖全部路径（含 `segment_enabled: false`、正则写坏、异常降级），实现要有**字符级兜底**；② 整条只剩分隔符 → **返回空、不发**（退回原文等于把 `⁂⁂⁂` 发出去）；③ 切分字符类**只留分隔符**（`.*?[⁂※⸮]+|.+`）——带上句末标点或 `$` 就做不到「不含分隔符的长文一条不切」（长文必有句号/换行，`$` 配 MULTILINE = 按行切）；④ 心跳里暴露版本（如 `segmentation = sep=⁂ interval=(1.5, 3.5)`），**改了代码不重启就还是旧版在跑**，用状态字段判断跑着的进程是哪一版。**长内容（清单/文档/报告/代码）靠人格里不写分隔符来保证整段，不靠阈值拦。**「符号定案 `⁂`（U+2042 三颗星）」：原用的 `※` 在中文/日文文档排版里也会出现 → 主人让她发一段文档就会被误切成多条，所以要换成「中文文档里基本不出现」的；选它因为 BMP 标点类（一字一 token 量级）、非正则元字符、不会被 JSON 吃、万一清理失效会露出来（自带告警）。**切分规则同时认 `⁂`/`※`/`⸮` 作为容错集合**（模型偶尔输出变体或残留旧习惯时不会把正文切碎）。换分隔符要改三处：切分正则、清理规则、人格措辞。
- **`astrbot plug search` 在容器里报 `/AstrBot is not a valid AstrBot root directory`**（加 `-w /AstrBot` 也一样）→ 找插件改用网页搜索。
- **记忆插件选型**：`astrbot_plugin_mnemosyne` 自带 Chroma/Milvus/Qdrant = **独立记忆筒子**，不写 Hindsight → 两门记忆分家，**不用**。要「两门共用同一 bank」就自己写：`hermes_memory`（`/opt/data/chat-layer/plugin/hermes_memory/`，钩子 on_agent_done → `POST http://172.17.0.1:8888/v1/default/banks/<bank>/memories`，`async=true` 不阻塞，tags `hermes,chat`（群聊 +`group`）、metadata `source=hermes-chat`）。写入是异步的，落库要等本地抽取（单条几十秒~几分钟）。

**AstrBot 侧文件**：`/AstrBot/data/cmd_config.json`（provider/provider_settings/platform_settings）、`data/data_v4.db`（conversations/personas）、`data/mcp_server.json`、`data/plugins/{hermes_delegate,hermes_memory,hermes_report,hermes_debounce}/`；**插件配置在 `data/config/<插件名>_config.json`**（`_conf_schema.json` 只定义默认值）；备份在 `/AstrBot/data/_setup_backup_<ts>/`。

**给它配「看图」（本地模型，不出门不花钱）**：主聊天模型是纯文本时的正确接法——把本地多模态端点注册成**第二个 provider**，再让图片走描述（AstrBot 内置机制）。三处改动：
1. `provider_sources` 追加一条 `{type: openai_chat_completion, provider_type: chat_completion, api_base: <本地端点>/v1, key: ["<占位>"], timeout: 900}`（本地 llama.cpp 不校验 key，但 timeout 要给足——它常与记忆抽取抢同一张 GPU，单张图可能要几分钟，给 300 秒会被掐断）
2. `provider` 追加 `{id: local/<model>, provider_source_id: <上条 id>, model: <model>, modalities: ["text","image"], max_context_tokens: 16384}` —— **`modalities` 里带 `image` 才是多模态**
3. `provider_settings.default_image_caption_provider_id` 指向它 → 图片由它描述后交给主聊天模型，主模型不必是多模态
- 本机可直接复用 Hermes `auxiliary.vision` 的那套（`config.yaml` 里查 base_url/model），**不必另起模型**：显存通常已被抽取+嵌入占满，另起一个 VLM 挤不进去
- 验收方式：容器内 `python3` 读一张真图 → base64 data URL POST 到该端点的 `/chat/completions`，看回答是否描述正确（图走本机路径，别往公网发）

**它的「技能 / 知识库 / 记忆」是三件不同的事（别混）**：
- **技能**：AstrBot **支持**，格式与我们一致 —— `<skills_root>/<技能名>/SKILL.md`（插件也能自带 `skills/` 子目录）。persona 的 `skills` 字段是白名单，与 `tools` 同语义：`NULL`=不限、`[]`=全禁、列表=只允许这些。可以把我们的 skill 拷给她，但**别全给**（会撑提示词）。
- **知识库**：**你主动上传的文档** → 检索后拼进提示词（RAG，存自己的 `data/knowledge_base/kb.db`；全局配 `kb_names`，也能按会话 `kb_config.kb_ids` 覆盖，`top_k` 默认 5）。**与 Hindsight 不是一回事**：KB = 读过什么材料，Hindsight = 经历过什么事，不能互相替代。默认空也能正常聊天；要放只放无敏感资料（群聊里可能被问出来）。
- **独立记忆目录**：她本来就有多层——`data_v4.db`（对话历史）、`platform_message_history`（群聊历史）、`data/workspaces/<umo>/`（会话级工作目录）、`kb.db`。**只把 Hindsight 当唯一真相源**，其余当缓存，别往里面放需要长期保留的东西。

**干活门长任务「主动回报」接收端（插件 `hermes_report`）**：aiohttp 绑 `172.17.0.1:8098`（**不是 `127.0.0.1`**），`POST /report`（头 `X-Report-Token`，令牌在 `chat-layer/plugin/hermes_report/.report_token`）→ `context.send_message` 发主人私聊 + Hindsight retain（tag `worker-report`）+ `plugin_data/hermes_report/reports.jsonl` 留痕，同 `task_id` 幂等。`GET /health` 返回 `{ok,last_umo,seen}` 可当活体探针。
- **判「真送到了」只看接收端历史，不看发送方返回值**：`context.send_message()` 返 `sent:true` 只表示「找到了平台」，不保证到达。判据是 NapCat `get_friend_msg_history` 里出现 `message_sent_type=self` 的记录 + `message_id`（`python3 scripts/onebot_msg.py recent <QQ> <n>`，宿主侧跑、走 sudo askpass）。这条同样适用于任何「发了消息就算成功」的断言。
- **回执进的是主人私聊 + Hindsight，不进聊天门（猫猫）的对话上下文**——派活方要知道进度得 `recall` tag `worker-report`；别以为回执会自动出现在对方上下文里。
- ⚠️ **接收端必须绑 `172.17.0.1`，绑 `127.0.0.1` 等于对干活门关门**：AstrBot 是 host 网络，`127.0.0.1` 只落在宿主 loopback 上 → Hermes 容器（docker 桥接网）够不着 ✗；`172.17.0.1`（docker 网关）容器可达、**不进局域网**，再配 `X-Report-Token` 即可。**验证要双向各测一次**：宿主 `curl http://172.17.0.1:8098/health` ✓ 且**容器内**同样 200 ✓ 才算通（只在宿主测会漏掉这个坑）。
- **「让猫猫自己知道任务做完了」= 把回执当一条「入站消息」推进她的 pipeline**（主人最终拍板，2026-09-23 已上线并实测）：回执触发一轮**完整 agent（带 41 个工具，含 `delegate_to_hermes`）**，她自己分析、自己决定下一步、**自己说给主人**。
  - 实现路径（**公开 API，没用私有属性**）：合成 `AstrMessageEvent`（适配器 `create_event` 挂 `self.bot`，回复能真发出去）+ `context.commit_event()` 入队 —— 官方封装是 `StarTools.create_event()`（`core/star/star_tools.py`，由 `api/star` 导出），官方先例 `core/cron/events.py` 的 `CronMessageEvent`。别走 `active_reply`（只在已有群消息上决定搭话，不会自己触发）或 `context.get_event_queue()`（源码里标了「不推荐」）。
  - **两个开关要分清**：`inject_enable`（把未读回执排进她的**下一轮 LLM 请求**上下文 `req.contexts`，走官方钩子 `@filter.on_llm_request`，调用点在 `process_stage/.../internal.py` 的 `build_main_agent` 之后 → 原地改 `req` 有效）与 `proactive_inject_enable`（入站消息触发整轮 agent）。**主人要的是后者**：他明确否掉注入——「回执应该由她自己说出来，不是塞进我的对话里」→ 现在 `inject_enable=false`（**不排队、不落盘**，pending 恒 0）、`proactive_inject_enable=true`。两个开关各打一行就绪日志，`/health` 也回显，便于复核。
  - **必须配闸门 + 防循环**：入站注入按 60s 间隔、12 次/小时封顶；防循环要**双保险**（`extra` 标记 + 正文死标记）——因为 `hermes_debounce` 会把事件重造一遍、`extra` 会丢。给她的入站消息带上可辨识的发件人/前缀（本机为「干活门回执(自动)」），她自己一看就知道不是主人发的。
  - **攻击面（主人已知且接受）**：注入那一轮她有 shell/Python/文件读写工具 → 回执内容若被污染就能在本机执行命令。口径：回执只从 8098（token + 只绑 `172.17.0.1`）进来，**永不开公网**。
  - **与防抖插件的交互**：主人正在打字时，回执会跟他的话并进同一轮 → 那条回执不会单独跑一轮 agent（她自己也发现了并报了）；要「每条回执都稳拿一轮」就得让回执消息不参与合并。
  - 通用要求仍在：日志只打条数/字数、绝不打回执正文；队列/缓冲落盘与否按主人取舍（他明确说过防抖那 10 秒缓冲**不落盘也行**：「十秒能差几句话，不怕」——重启恰卡窗口会静默丢 1~2 条，属**已接受**行为，别当 bug 查）。

**「跑的到底是哪一版插件」用日志行号判，别用文件时间戳**：插件自己的日志带 `[<模块>:<行号>]`，把那个行号跟**磁盘上文件**的行号对一下就知道加载的是新版还是旧版（本机实测：日志 `hermes_memory.main:55 / :82` = v1.0.0，而新文件对应 `:241` → **盘上有新代码 ≠ 在跑新版**）。判据顺序：① 日志行号 vs 磁盘行号 ② 日志里有没有新版才有的就绪行（如合并后的单行 `就绪 bank=…`）。**改完不重启 = 白改**，别拿「文件已更新」当生效。

**并发施工 AstrBot 时，重启后要把「全部」关键参数逐项核一遍**：同一个 AstrBot 上可能有多个会话/子代理同时改插件、各自重启，**别人会把你的参数覆盖回旧值**（本机现成教训：`hermes_debounce` 的 `wait_seconds` 刚被主人从 7 改成 10，另一个子代理重启就可能带回去）。所以固定核对清单要含**各插件的配置值**，不只是「加载成功没有」：provider 默认项 / `MCP 1/1` / `适配器已连接` / `hermes_memory 就绪 bank=` / `hermes_report` 两个开关行 / `hermes_debounce … wait=10.0s` / `ERROR|Traceback` 计数 0。派子代理动 AstrBot 时把这份清单写进任务书，并要求它改完照单核。**并发也会改到「文件」本身**：一个跑几十分钟的任务期间，目标文件可能被另一个会话就地重写（实测：接单时读到的那份与落笔时的不是同一版，行数与体积都变过）→ 开工先记下**每个待改文件的 sha256 与行数**当基线，动笔前再核一次；版本对不上就停下来重新读，别按记忆里的结构下 patch。改完的**回滚脚本要真的演练一次**：把目录整份 `cp -a` 到 `/tmp` 当靶子跑回滚 → 核对源文件 sha256 精确还原 + 测试套件全绿，只写不跑的回滚等于没有回滚（演练会把「备份漏了哪些文件」「断言没跟着回退」这类问题提前暴露出来）。

**聊天门的能力边界（主人定的分工，写进她的提示词/技能）**——**2026-09-23 已落地并实测**，实现见 `chat-layer/plugin/hermes_lookup/`（README 有全套证据）+ 人格里那段《工具与边界（硬规矩）》：
- **单次查询她自己做**：网页搜索（走自建 SearXNG `http://172.17.0.1:18888/search?q=…&format=json`）、单页正文抓取（直连→失败走宿主代理一次；公众号自动换微信 UA）、B 站信息（`x/web-interface/view` 元数据+官方分章、`search/type` 关键词搜，需先取首页 buvid3）这类一问一答的活。**封装成插件工具**，别让她自己拼 curl/shell——那又变回多轮活。
- **边界要两层**：① 人格白名单（`personas.tools` 显式列表）② 每轮剪枝（`on_llm_request` 里 `remove_tool`，因为 local runtime 会强制注入，见上文）——只做一层必漏。取舍：白名单是闭集，**新插件工具必须补名单**。
- **多轮 / 长活 / 需要中途调整 / 登录态操作 → 派给干活门**。理由要一起写清：**她无法中断任务**，一旦开跑就从头干到底，中途改不了方向。
- 分寸：不是收走工具把她变成传话筒 —— 「啥都干不了」和「啥都自己扛」都是错的，边界写在「哪类活不碰」上。
- **文件类工具是给回的，别一刀砍掉**（主人 2026-09-23 改判）：**给** `astrbot_file_read/write/edit_tool` + `astrbot_grep_tool`；**仍不给** shell / shell_session / 解释器。理由原话——助理连主人发来的文件和报告都看不了，就不叫助理；「我在提示词里写严了就够了」。配套三处：① 白名单（+每轮剪枝的摘除名单去掉这四个）② 人格里写行为约束（只在「主人明确给了文件」或「明确要你写东西」时碰；不动系统/配置目录与别人的数据；不确定先把路径和打算做的事说出来等同意再动；要跑命令/装东西/动服务 → 派活）③ **不要加路径 allowlist**（主人明确否掉那层「只许在自己目录里动」的保险，理由是提示词写严足够）。
- **搬技能只搬聊天向的**（说话风格、派活任务书格式、记忆用法、决策辅助这类），**工具/运维类不搬** —— 她有 shell/Python/文件写工具，再教她碰基础设施等于同时放大风险面。技能里引用的 Hermes 专属工具名（`terminal` / `browser_exec` / `skill_view`…）必须改写成她手上的等价物，否则她照着做只会失败。
- **装技能不用重启 AstrBot**：技能是**每轮请求扫描**的（`astr_main_agent.py` 每轮调 `SkillManager.list_skills(active_only=True)` + `build_skills_prompt()` 拼进 `req.system_prompt`），新技能落进 `data/skills/<名>/SKILL.md` 即生效；换完顺手核 `data/skills.json` 里 `active:true` + 跑一次 `build_skills_prompt()` 看拼出的块（本机四个技能 ≈2,194 字符 ≈731 token/轮，记得算进每轮固定成本）。

**聊天门「通道层 / 大脑」分工（选型硬事实，别凭直觉设计）**——主人纠结「Hermes 有能力但不真人 / AstrBot 像人但没工具」时，正确切法是**只换大脑，节奏留在通道层**：
- **分段与防抖天生是通道层的活**：Hermes 侧**没有**这两件东西——出站唯一带 segment 语义的是「先说话再调工具」的 preamble 边界（`on_segment_break()`，一回合正常终点只发一条），防抖只有「会话已忙」时约 0.35s 的缓冲，**没有空闲期连发合并**。所以「把分段/防抖搬进大脑」= 重写两件已经跑通的东西 ✗，留在通道层（AstrBot）成本 0 ✓。
- **Hermes 接不了个人号协议端**：它唯一的 QQ 平台是官方机器人 API v2（`app_id`/`client_secret`），全树 `grep -rni "onebot|aiocqhttp|napcat"` 零命中、官方文档同样零命中 → 要用小号只有两条路：① 通道层仍放 AstrBot（转发给大脑）② **自己写 OneBot v11 适配器**（适配器天然管消息流：分段 = 出站按分隔符切多条发、防抖 = 入站先攥 N 秒，都是小函数）。另外官方**平台锁按 app_id 全机生效**，两个门不能共用同一个 app_id。
- **转发给大脑必须带固定会话 id**：不带就是「每条消息 = 一条新会话」，每轮重付人格+记忆+技能那一大坨固定成本（旧 `hermes_forward` 正是这么写的）。带上固定 id 后大脑自己留历史，固定前缀还能吃**前缀缓存**——所以常驻块一律**追加在提示词最末尾**（往人格/技能前面插会破前缀、每轮全价）。
- **连续性靠「常驻记忆块」，不靠召回**：召回是按当前这句话去搜（一问一答），给不了「一直都在」的底。Hermes 的做法是每轮把 `MEMORY.md`/`USER.md` 拼进系统提示词（有字符预算，超限**拒写并要求先合并**、原子写+文件锁）；AstrBot 侧等价物是 `hermes_memo` 插件（`req.system_prompt` 尾部追加 + `memo_view/add/replace/remove` 让她自己维护，**写权限只给主人私聊**，群聊拒写）。**两层都要有**：常驻块答「你是谁、我们怎么合作」，召回答「昨天聊到哪」。
- 选路线的权衡（主人拍板用）：**通道留在 AstrBot** = 保住小号与多气泡，代价是长期背一个中间层 + 跟它的升级补丁打架；**给 Hermes 写 OneBot 适配器** = 一个栈、小号与多气泡都在适配器里实现，代价是自己写并维护；**换官方机器人** = 零自建全原生，代价是换号且暂时没有多气泡。
- **「切到 Hermes 大脑」的最小切换动作：改 NapCat 的一个 url**（已落地）：NapCat 是**主动往外连**的反向 WS 客户端，配置在 `stack/napcat/config/onebot11_<QQ>.json` 的 `network.websocketClients[0].url`。本机现状是 `ws://127.0.0.1:6199/ws`（AstrBot 的监听口）→ 改成 Hermes 适配器的 `ws://127.0.0.1:6700/ws` 即完成切换。**AstrBot 一行都不用动**：没人连它它就静默（不双回复、不重复调模型）——所以它就是**回退保险**，回退 = 把 url 改回 `6199/ws` + 重启 napcat，一分钟。要点：① 两侧 token 必须一致，且 token 只能**程序化搬运**（NapCat 配置 → Hermes 的 `ONEBOT_ACCESS_TOKEN`），**全程不回显**；② 适配器源码 `chat-layer/plugin/hermes_onebot/`、部署副本 `profiles/chat/plugins/onebot/`，启用/回滚/排障全在它自己的 `RUNBOOK.md`（`deploy.sh` 是幂等同步、`doctor.py` 是只读自检）——⚠️ **两份会「反向漂移」：线上比源码新**（线上迭代后没人回写源码）→ **跑任何同步/部署脚本前先 `diff` 两侧**，线上新就先把线上同步回源码再谈别的；盲跑 `deploy.sh` 会把线上功能打回旧版。判线上跑的是哪一版看行数与其 `[模块:行号]` 日志，不看文件时间戳。**同步时还要核 sync 脚本自己的拷贝清单**：实测线上独有的模块文件不在 `deploy.sh` 清单里 → 盲跑不只是回退版本，还会把那个功能静默降级成「模块缺失→停用」；把线上拷回源码时**逐文件 `cmp` 全绿 + 清单补全**才算真身唯一（旧源码先 `cp -a` 到 `.bak-stale-source-<ts>/`）；③ 启用三处配置：`plugins.enabled: [onebot]`、`platforms.onebot.enabled: true`、`extra.read_only: false`（**默认 `true` = 只收不发，是安全默认**）；④ 群聊先把 `enabled: false` 或只私聊，别一上线就接群。
- **改动「主人的日常聊天通道」时，把回退步骤写在报告最上面**：这类切换动的是他每天用的东西，出岔子他立刻有感。规矩：先备份（NapCat 配置 / profile config / secret）、保持旧通道原样不动当保险、报告第一句就是「回退＝改哪一处、多久生效」，然后再讲改了什么。

## 聊天门「陪伴向特调」方向定案（主人拍板；完整版 `/opt/data/chat-layer/CHAT-AGENT-DIRECTION.md`）

- **归因结案：两侧模型完全相同 → 「像人」全部来自提示词**（不是模型、不是平台）。别再安排模型/平台对照实验，功夫全砸在提示词形状上。
- **提示词是两种形状**：陪伴向 = 少规则 + **要有 user/AI 对话示例** + 写节奏 + 不给结论不说教 + **窄工具面** + 常驻记忆；干活型相反（规则严密、工具全开、结论先行）。**禁止把工作型 SOUL 直接搬去当聊天人格**（工具 schema 也计入系统提示词，会让语气变汇报腔）。
- **定位是「助理」不是「员工」**（主人原话标准）：助理会查资料、记事、安排、转达、提醒、把麻烦交给专业的人；员工才自己动手改配置跑脚本。工具闭集按「**一步能做完**」定，砍掉不等于不能做——多步/要中途改方向/登录态一律转手派出，窄工具面 ≠ 传话筒。
- **主动开口必须落在已存在的会话里**：不许「空开一个新对话」再说话——新会话没有上下文，主人得引用那一句才知道她说过什么（实机踩过）。验收：主动开口后她能说出自己上一句说了什么。
- **告警分级**：能自愈的不吵人，需要人干预的才推送；全都告警 = 没有告警（告警疲劳）。检查项必须含**参数漂移**（防抖 wait、记忆开关、工具白名单、补丁在位）。
- **主人两条红线**：① **少外挂**——优先用官方扩展点，别为一点事多写一个程序；② **坏了必须能马上知道**——环境可以不优雅，但不能不可知。

## 跨门派活：非阻塞收件箱（比同步 A2A 优先）

派活方嫌 `a2a_call` 同步等（那一轮被卡住）时，不要放弃跨门直达——改走「**文件投递 + 入站注入**」：派活门把任务书 `write_file` 进收件箱目录就返回，接收门的插件把任务书当**真入站消息**注入目标会话，接收门在**那条会话里**被激活一轮（主人当场看得见、能直接给审批）。

- 三方向各用一种工具，别混：**入站注入**（`PluginContext.inject_message` → 真触发一轮，会打断正在跑的那轮）/ **出站推送**（`hermes send`，只落 `assistant` 镜像行，不触发）/ **cron 投递**（`deliver: platform:chat_id`）。
- 两道开关缺一不可：`plugins.enabled` 有名字 + `plugins.entries.<id>.allow_gateway_injection: true`（fail-closed，只认已存在的会话）。
- **写 `config.yaml` 用 `hermes config set`**：`patch`/`write_file` 会被框架拒（`Refusing to write to Hermes config file`）。插件代码在网关进程启动时加载 → 改完必须重启那道门。
- `a2a_call` 降为**备份通道**：只用于「必须在自己这一轮里拿到结果」的短活。
- **回执要能「推」回去，不能只落在文件里等对方想起来看**：派活方向的反向通道同理要做（对称插件 + 反向收件箱目录），否则干完活的结论会一直躺着没人看，派活方根本不知道收工了。实测做法与脚本见 `references/cross-door-inbox-injection.md` 的「反向通道」节。
- **主人对这类缺口的期待是「自己建」，不是「报告限制」**：当发现「我发不出去 / 我收不到」时，先想有没有一个最小、无端口无令牌、可回滚的通道能补上（对称镜像现有插件通常就是最小解），把通道建好跑通再来回报；只在「确实没有可行机制」时才回限制。
- **自写插件必须带 `plugin.yaml`**：只有 `__init__.py` 时 `hermes plugins list` 里**根本不出现**、也不加载（白改一轮）——`plugin.yaml` + `__init__.py` 两个文件才是完整插件。
- **目标达成且主人确认后就停手**：验收标准逐条满足、主人回一句「确实成功了」之后，别再顺手加打磨项（更多标签、更全的编号表、更花的花活）——本轮没验完的自动生效余波（重启、cron）确认到位即收工。

完整机制、插件骨架、双向通道、回执三条路与坑见 `references/cross-door-inbox-injection.md`。

## 改 Hermes 自己的配置（`config.yaml`）：CLI 是唯一通道

`patch`/`write_file` 对 `config.yaml` 一律被框架拒（`Refusing to write to Hermes config file: …`）。**别绕（别用 python 直写），走 `hermes config set`** —— 它原子写、保留文件其余内容；改完 `diff` 对备份核一遍，确认只动了预期那几行（顺带拿到回滚依据）。

- **未登记进默认 schema 的键要 `--force`**：不加只会报 `unrecognized key` 而**不写盘**（看着像成功）。这类键运行时确实会被读到——`stt.provider`、`stt.openai.base_url`、`tts.openai.api_key` 都属于「schema 不认但代码认」。写盘后 `hermes config get <key>` 复核。
- **改非默认 profile 的配置**：`-p` 不是全局旗标；用 `HERMES_HOME=<映射>/profiles/<名> hermes config set …`。先 `HERMES_HOME=… hermes config path` 确认指向哪份文件（默认会指向默认门那份，别猜）。**两个门各有自己的 config.yaml**，改一道门不会影响另一道——「接入某能力」时期望两门都有就得各写一次。
- **改键之前先确认它的「生效层」，三层不能互相串**：① **适配器 extra**（`platforms.<平台>.extra.<键>`，网关**基类兜底路径**才读，如 telegram/slack 式适配器）；② **GatewayConfig 顶层**（与 `compression:`/`memory:` 同级的 config.yaml 顶层键，由 `SessionStore._generate_session_key()` 那类路径读，如 `group_sessions_per_user`）；③ **构建期读**（`compression` 这类 agent 构建时注入、必须重启）。写错层的典型症状是「改完重启了、行为一丝不变」。**同一个键可以两层都写**（不冲突，保持两路径一致），但只有真层的那份起作用。
- **不许拿「文件里写了」当「已生效」**（这条犯过）：验证分两级——① `HERMES_HOME=<profile> hermes config get <键>` 走官方读取器（不是自己读 yaml）；② **线上日志/真实业务的可观察痕迹**（例：「整群一条会话」的判据是 `response ready: … session=agent:main:onebot:group:<群号>` **尾号消失**；只有 ① 过、② 没过，就说明改错了层或没重启）。没拿到 ② 就说「配上了、待下一条消息验证」，**别说「验证过、已生效」**。
- **接线类改动必须真跑一次业务才算数**（写对配置 ≠ 链路通）：两端各跑一次真实调用。例：本地语音栈接进来后，① 合成一句话看产物时长 + 对方容器日志出现 `POST /v1/audio/speech 200`；② 把真实样本喂给转写入口（可直接调模块函数，如 `PYTHONPATH=/opt/hermes /opt/hermes/.venv/bin/python -c "from tools.transcription_tools import transcribe_audio; print(transcribe_audio('<wav>'))"`）看 `{"success": true, "transcript": …}`。provider 语义与踩坑细节见 `voice-stack`。
- **哪些改动免重启**：工具类配置多半是**每次调用现读**（`_load_*_config()`）→ 直接实测即可；**适配器/插件类配置在网关进程启动时读** → 那类必须重启对应那道门（配方见上文 s6 段）。

## cron 克制（主人定的常驻规矩）

一次修好就不再给它挂哨兵。

- **可以挂**：① 坏了能自愈的看门狗（`no_agent` + 脚本，无输出即静默）② 只在失败/需要人干预时才开口的告警 ③ 定期维护（备份清理、GC、月度合并）。
- **不要挂**：为单次修复配的「效果回看 / 体检单」日报；一次性自审跑完就删。口径就是主人的原话：**「一次搞好了就不要管了，不要搞一堆自检，减少一点」**。
- 减负用 `cronjob_manage`（`remove` / `pause`，`pause` 带上 `paused_reason`），动前 `cp -a cron/jobs.json` 备份；汇报时只列被删/被停的那几个，其余一句带过。

## 参考
- `references/cross-door-inbox-injection.md` — **跨门派活：非阻塞收件箱 + 入站注入（双向）**：三方向工具分界（注入=真一轮 / `hermes send`=只落镜像 / cron 投递）、`allow_gateway_injection` 同意闸与 `session_key` 写法、为何写配置要 `hermes config set`、收件箱实现要点（mtime 宽限 / 原子认领 / 只在 live injector 进程消费 / 失败退回）、**反向通道（干活门 → 聊天门 主动投递，`scripts/crossdoor_to_chat.sh` 一行投递、首行 `@session:` 换目标会话、投递会真激活对方一轮）与「缺 `plugin.yaml` = 插件等于没装」**、回执落点与「别拿 retain 返回值当证据」、注入会打断对门当前一轮、端到端自检与回滚
- `/opt/data/ops-changelog/README.md` — **运维总入口**（通道优先级 / 端点表 / cron / 已知坑），动系统之前先看它
- `/opt/data/ops-changelog/ownership-changes.md` — 属主/权限变更台账（from→to、理由、回滚、验证、基线普查）；**任何 chown 都要在这里登记**
- `references/host-docker-access.md` — 宿主 docker / 共享文件访问：官方 trim-cli 通道优先（容器内直调 + 通道自检）、SSH+sudo 兜底、GPU 直通、镜像站
- `references/mounts-and-visibility.md` — 挂载清单（哪个宿主路径已挂进容器、属主权限、可读/可写）、通道优先级、路径可见性三步排查法
- `references/migration-ownership-residue.md` — 迁移残留（990 属主目录）导致的 `Permission denied`：哪些目录中招、legacy 回退修法、彻底修好要的一条 chown
- `references/rebuild-and-mount-20260825.md` — 备份挂载实战全记录（含错误路径：SSH 隧道被禁、trim_acl 两轮排查）
- `references/s6-supervised-endstate.md` — gateway 的 s6 托管常态与安全重启语义，**以及同容器多门（多 profile）的识别与探活**：槽目录的 `run` 脚本才是「这个门用哪个 profile」的权威答案、容器内没 `ss` 时用 `/proc/net/tcp[6]` inode→`/proc/*/fd` 反查端口归属、**认门看 `/proc/<pid>/environ` 的 `HERMES_HOME` 而不是命令行**、`s6-supervise` 在 ≠ 被监督的 gateway 活着
- `references/platform-credentials-migration.md` — 平台凭证迁移清单（旧框架 → Hermes 键位映射）与各平台适配器配置键速查
- `references/home-channel-and-cron-delivery.md` — **home channel 提示的成因/处置 + 投递目标与出站推送**：`deliver` 的 `platform:chat_id[:thread]` 写法与从 `channel_directory.json` 取目标、`attach_to_session` 的语义（可续会话，也是显式目标能被附上的唯一开关）、chat_id 与 `/new` 无关、**投递必须由持有该平台适配器的网关执行**（跨门派活要把目标写进任务书）、一次性 job 的端到端验证配方，以及 **`hermes send` 轻量出站推送与会话镜像的角色语义**（mirror 行是 `assistant`：看得到、但不被当指令、**不触发一轮**）+ 「门→主人 / 主人→门 / 门→门」三方向分界与镜像落库的记号验证法
- `local-llm-ops/references/qq-image-vision.md` — **QQ 附件落地**：图片/文档/语音各落到哪个缓存目录、消息里以什么标记出现（主人发的文件能直接读，不必先传共享文件夹）、排障看哪行日志
- `references/im-channels-and-frontends.md` — **IM 通道能力与前端选型**：QQ 官方机器人 / 微信 iLink 各自能拿到什么、哪些限制绕不过、Hermes `api_server` 的约束、外部聊天前端（AstrBot 类）引入评估与安全下限、**AstrBot 当纯通道的源码级事实（`stop_event()` 会连分段一起吃掉，以及正确的接管姿势）**、**第二道门用独立 sidecar 容器（`/p/<profile>/` 被 s6 容器排除）与 prompt 成本量级**、**个人号协议端上线实操（NapCat 扫码运维：重启即掉登录态、二维码约 2 分钟轮换、拷 PNG 放大再发；AstrBot 纯通道的配置级落点与属主坑）**、**协议端长期运维（免扫码靠环境变量 `ACCOUNT` 而非命令行参数——`command: ["-q",…]` 从未生效；`ACCOUNT` 写对后重启/重建**都免扫码**、票据过期才需重扫；二维码取图与放大、`stack/` 数据分类约定、数据卷迁出配方、掉线走版本策略、反检测开关别开、面板 token 属涉密）**
- `templates/napcat-astrbot-compose.yml` — 个人号协议端（NapCat）+ AstrBot 通道层的可用 compose（host 网络、数据卷、`mem_limit`、`no-new-privileges`），复制后改路径即用；配 `im-channels-and-frontends.md` 一起看
- `references/chat-layer-segmentation-and-cost.md` — **聊天门调优**（分段/节奏/收窄/成本；另含「Hermes 无分段与防抖、通道层 vs 大脑的分工结论」）：分段三门槛与「≤150 字才切」的反直觉阈值、切分/节奏两个 stage 的分工与「自己切仍借它间隔」配方、人格提示词落点（`profiles/<name>/SOUL.md` + 注入拦截）、**profile 收窄配方（`platform_toolsets` / `tool_search auto=on` 陷阱 / `memory.provider` 默认空 / Hindsight profile 配置与 `recall_sync`·`memory_mode`）+ 实测降幅（33 万→8 千 token）**、记忆共享的正确验证法（查 `/documents`，别拿「它答对了」当证据）、日志特征排障、余额与花费自查（`/user/balance`、`hermes insights`）与 DeepSeek 峰谷时段、**收窄后的能力边界表（能写什么·不能写什么）与「要它记住某个人」的四件套**、字符 vs 字节与中文 token 换算（≈0.5 token/字符）、**文件权限能限什么（写沙箱 `HERMES_WRITE_SAFE_ROOT`、指令文件闸其实是「等审批再拒」、给聊天门开文件权的落地四条）与跨门派活的准确工具面（`a2a` 默认关／没配置就幻觉）**
- `references/multi-profile-a2a-dispatch.md` — **跨门派活（A2A）**：两条第一方通路的区别（A2A 协议级 / Bot Mode `message_agent` 会话级 / `hermes peer dm` CLI 级）、启用步骤（`config set … --force`、`tools enable a2a --platform api_server|cli`、`a2a_agents`、`A2A_AGENT_NAME`）、s6 槽名与「别在会话里重启承载自己的那道门」、**验证三件套（Agent Card / headless CLI / api_server 生产路径）**、同步等待与 push 通知、审计与对话落点、回滚；**§8 反向「回执」链路（干活门 → 聊天门）：落点 `agent:main:a2a:dm:<context>` vs 私聊 `agent:main:onebot:dm:<QQ>` 两条互不相通的上下文、入口 `scripts/receipt_to_chat_door.py`、反循环闸 5 条/小时、入站被官方框为不可信输入且对端名默认是 IP、以及两条硬事实（本 build 无 agent 可调 send 工具 → 只有 cron 投递能主动对平台说话；插件平台默认工具集 = 全套 core tools，收窄必须显式写列表且不能留 `hermes-<平台>` 这个名字）**
- `templates/chat-door-run.sh` — **profile sidecar（第二道门）启动器**：host 网络、独立 `HERMES_HOME`、`PUID/PGID` 取数据目录属主（漏了就会跑成 uid 10000 → `PermissionError`）、ROOT 变量兼容宿主/容器两种路径视图、起来后自动探活 `/v1/models`
- `references/container-source-patch-persistence.md` — **容器内源码补丁「重建即自愈」配方**：载荷放挂载目录（合并版幂等补丁 + AST 自检脚本 + entrypoint 先打再 `exec "$@"` + 宿主一键兜底）、compose `entrypoint` 覆盖与只读挂载、**用一次性容器做「模拟重建」三段验收**（自愈/幂等/真 import 签名判定）、以及坑（分批补丁脚本必须合并、`ast.AsyncFunctionDef`、模拟容器必须挂 data 目录、线上旧容器与新 compose 暂时不一致时不要擅自重建）
- `references/hindsight-memory-curation.md` — **记忆库纠错与整备**：主人定的常驻规则（写下错误结论后**必须把那条记忆标废弃**，不能只补一条新更正）、软废弃接口与字段、**验证要看召回不看 PATCH 的 200**、找错条目的两条检索（子串 + 语义）、三类条目只有一类该废弃、「retain 会把每轮进度絮语当事实存下来」这个会持续产生中间态的类级问题、以及**上游压制的标准流程与双向量尺（两份指令都追加、`PATCH /config` 用 `{"updates":{…}}`、用只读的 `dry-run-extract` 做改前/改后 A/B，并必补测「日常闲聊会不会被误筛」）**；可复用工具 `scripts/mem_invalidate.py`
- `references/hindsight-queue-triage.md` — **记忆写入失败 / 任务队列排查**：`hindsight_retain` 报 `Failed to store memory: ` 的真实含义（请求已入队、只是同步等待超时，不等于数据丢失）、`/operations` 端点与参数上限（数组键是 `operations` 不是 `items`）、僵尸判据（`processing` 且 `updated_at` 停住）、**「慢」与「死」的区分读数（pending 是否在减 / `llm-requests` 是否还在出新 `success` / `failed` 计数；只凭一条 op 停着不动不叫卡死）**、错误分类表、抽取后端 ctx 硬约束与「修法未验证」的边界，**以及 §6 重跑失败任务的规矩：挑子 op 不挑父聚合器、串行且逐条等终态、别碰运维主动取消的、重跑会重写该文档既有事实**
- `scripts/ops_triage.py` — Hindsight 只读体检脚本：队列聚合（task_type×status）+ failed 清单 + 僵尸检测 + documents/fact_count 计数；`--retry <op_id>` 显式重试（写操作）
- `references/alerting-design-rules.md` — **告警通道设计（血泪）**：告警不能挂在被退役/被切换的一方（实测 HTTP 500 静默丢）、主通道用 Hermes cron `--no-agent`、分级与同签名退避、「故意失败一次」验证、体检项要含参数漂移、日志纪律
- `references/onebot-protocol-capability-probe.md` — **OneBot v11 协议端能力探测与适配器改造**：NapCat HTTP 探针（无副作用、错误三态判读「action 不存在 / 参数不匹配 / 缺必填」——不校准就会把参数写错误判成能力不存在，**且探针函数自己用 `shift` 写错会造出同形的假阴性**）、本机实测存在与不存在的 action 清单（`set_msg_emoji_like`·`get_emoji_likes` 有、**账号收藏表情包一族 `fetch_custom_face` 等有且生态无人用过**、`set_group_reaction` 无、**QQ 空间一族全无**）、表情段的 id 字段名与渲染约定、**出站散段「类型 + 取值」双坑（`face.id`/`at.qq` 必须 String、`image.sub_type` 才能是 Number；编号必须在原生表情表里，否则整条报「消息体无法解析」）与探测格式问题要用自己账号当靶**、AstrBot 段处理参考位置、Hermes 原生 onebot 适配器**已落地项与仍未动项**、媒体落盘目录/命名约定与「不碰真实会话」的验证方式
- `references/group-chat-design.md` — **群聊机制设计（潜水 / 主动插话 / @ 必答 / 记忆不污染）**：状态机（潜水态・对话态・冷却闸）与硬规矩「冷却只能作用在『潜水→主动插话』路径上，对话态内一律不生效」（否则连贯对话会「聊着聊着人没了」）、@ 必答直通 + 合并与分钟上限、三级门控（规则→概率→小模型，且**必须做在适配器层、LLM 之前**，否则每条群消息烧一整轮 ~21k token）、三层记忆（每群滚动窗口 / 她自己挑着写并打来源标签 / 默认零入库；以及为何「先全量入库再挑着删」做不到）、表情包两阶段（先屯后发）、两个可抄参考实现（麦麦bot 规则评分前置 + 收到即入库去重；qq-bridge 全量收不唤醒，但收藏库同步那条别抄）、参数集中可调、群内边界与分步 B→C1→C2→C3；**B 阶段已落地**：每群滚动窗口落 `profiles/<p>/onebot-groups/<群号>.jsonl`（条数+字节双上限、重启不丢、只存昵称/时间/占位文本），唤醒模式三态 `collect-only | mention-only | full`（红线开关 `group_wake_enabled` 默认关），心跳暴露 `group_mode / group_rx_count / group_llm_calls / group_window` 用来取证「只看不说 = 0 次 LLM、0 次入库」——**先跑通「收得到、不乱花钱、不污染」，再开 @ 唤醒**；开唤醒前必须先做「群回合会不会触发 profile 级 auto_retain 入库」的取证，堵不住就不许开
- `references/astrbot-era-pitfalls.md` — **AstrBot 时代技术坑（已退役留档，多数通用）**：人格白名单挡不住每轮硬注入的工具/英文提示 → 必须每轮剪枝且逐句校正；`persona.tools` 的 `None`=全量 / `[]`=一个不给；**分段规则已简化为「有分隔符就切、没有就整段」——长度阈值已废（它会导致「不切=不清理=分隔符露在正文里」），且分隔符在任何路径下都不得出现在用户可见的消息里**；分隔符不能选正文里会出现的符号；容器内源码补丁重建即丢（登记+自愈入口）、插件 pip 依赖在可写层重建会串行重装拖停整条链路
- 技能 `chat-channel-switchover`（devops）— **给聊天通道换接入方/回退**：只改协议端一个 url、旧侧一字不动当回退保险、验证三连、告警随迁、换大脑必须先迁历史；附可抄清单 `templates/switchover-checklist.md`
- `references/astrbot-platform-ops.md` — **AstrBot 平台运维**：面板口令只存哈希（MD5+pbkdf2）→ 只能重置的两条路与登录验证法、**凭证交付规矩（不进口令到聊天正文；0600 文件 + 遮蔽输入）**、provider 配置键名（`provider_sources`+`provider`）、开 provider 不会让插件闭嘴（双回复）、`@filter.llm_tool` 的 docstring 陷阱、MCP `streamable_http` 接法、pipeline 顺序与分段不冲突、原生历史库、派活走 A2A 的报文契约、**`persona.tools` 白名单语义（NULL=全量工具 / `"[]"`=一个不给——填错的症状是模型自称「够不着/没权限」）**、**直聊后记忆不会自动回填（Hindsight 只是工具集；要自动入库得挂 `on_llm_response`/`on_agent_done` 钩子）**、function-calling 端点探针、**§八 记忆双向（`hermes_memory` v1.1.0：retain + recall 合成一个模块、`@filter.on_llm_request` 自动召回注入、共用守卫与降级、延迟量级、以及 **Hindsight recall 的 `score` 恒为 0** → `min_score` 是死键）**、**§九 连发合并（防抖）插件机制（`event_message_type` 优先级、`stop_event()` 断链、改参数需重启、它会重建事件所以 `set_extra` 标记会丢）**

- `references/tool-surface-inventory.md` — **列某道门/某平台真实生效的工具面 + 给它划边界**：官方 CLI 要在交互终端里跑 → 会话内只能走 `scripts/dump_platform_tools.py`（`_get_platform_tools()` 返回的是工具集名不是工具名、须用 `TOOLSETS` 展开、插件工具集另读注册表）、「工具面 ≠ 适配器行为」的判据、拿清单写边界的分类法、**按消息来源在适配器层分流（私聊有/群聊摘、fail-closed、钩子怎么单测）**、**`approvals.deny` 命令层硬闸（smart/--yolo 之下也生效）与用 `_match_user_deny_rule` 做 A/B 的验证法、以及「同 uid 凭据无法靠权限隔离，只能软堵」的口径**、人设/技能里的工具名要对着清单核（假信息高发区）
- `references/context-window-and-compression.md` — **上下文上限与自动压缩**：`threshold`（窗口比例）与 `threshold_tokens`（绝对上限）**取低**的语义、模型窗口表在 `agent/model_metadata.py`（最长键优先）、循环/会话上限键位；答「现在上限是多少」时把三层数分开；**「profile 里没写 `compression` 段」= 走默认 0.5（不是不压缩）**、窗口 <512K 时 threshold 被抬到 0.75、`config get` 实读法、**改完必须重启该门才生效**、用 `state.db` 量会话规模判断压缩会不会真触发、**会话连续性 / 花费取证（回答「是不是每次都会新开对话、开销多大」：`sessions` 表与 `sessions.json` 的读法、单轮 vs 累计、缓存命中的读法）**、以及**两个门的提示词/记忆/召回/压缩落点表（宿主映射路径）**

## 验证「新 SOUL/人设是否生效」（按会话，可复现）

提示词在**会话创建时**定下来：改了 `SOUL.md` 或 `profiles/<name>/SOUL.md`，老会话继续跑旧人格，重启网关也不会重建它。

⚠️ **一个门的 SOUL 往往是两份（别拿手里的副本直接覆盖）**：主人也在看/改的共享稿（`/vol2/@team/共享文件/提示词.txt`）与生效文件（`profiles/<p>/SOUL.md`）；而**对方 agent 自己也会改它那份**（她有 `file` 工具，实测把自己人格里与能力打架的一句改掉了，而且改得对）。所以动 SOUL 前：① `md5sum` 两份 + 和本机工作副本对比，**三方不一致就先 diff**；② 对方那侧改得合理就**采纳措辞并推回共享稿**（让生效文件 = 共享稿），不要顺手改回去；③ 推共享稿 ≥ 10KB 一律走 stdin（`… "cat > '<路径>'" < 本地文件`）+ **md5 回读**——`base64` 塞 argv 会静默截断并把原文件清零（细节在 `host-docker-access.md`）。

```python
import sqlite3
con = sqlite3.connect("file:/opt/data/state.db?mode=ro", uri=True); cur = con.cursor()
cur.execute("select system_prompt_hash, message_count from sessions where id=?", (sid,))
h = cur.fetchone()[0]
cur.execute("select prompt from system_prompts where hash=?", (h,))
print([m for m in ["新标记1", "新标记2"] if m in cur.fetchone()[0]])
```

- 干活门库 `/opt/data/state.db`，聊天门库 `/opt/data/profiles/chat/state.db`；表是 `sessions(hash) → system_prompts(hash, prompt)`
- `sessions.system_prompt` 列是空的，别读它；`sessions.system_prompt` 与 `system_prompt_hash` 名字相近，取 hash 才对
- 想让它换新人格：**开新会话**（删掉该会话行，或让主人发 `/new`）；`session_key`/`chat_id` 为 NULL 的 api_server 会话是每次请求新建的，天然带新人格
- 判据用**内容标记**（新旧两套各 2–3 个只在某版出现的短语），别用长度或时间戳
- **聊天门例外、不用重置**：AstrBot 的 `hermes_forward` 插件每次把“最近 8 轮历史 + 本条”整包 POST 给 api_server、**不带会话 id** → 每个请求都是一条新会话（所以会看到 33 条消息的会话=8 轮×2+1，不是持久会话）→ **SOUL 改完下一条就生效**，不用 /hreset、不用删会话、不用重启。判断依据读插件 `_ask()`：payload 只有 model/messages/stream
- 反过来：`session_key`/`chat_id` 有值的（qqbot / a2a / cron）才是持久会话，改 SOUL 得过新会话
- **AstrBot 平台能力事实**（provider 配置键名、开 provider 不会让插件闭嘴会双回复、`@filter.llm_tool` 的 docstring 陷阱、MCP `streamable_http` 接法、pipeline 与分段不冲突、A2A 派活报文契约）→ 常驻部分已写进 `references/astrbot-platform-ops.md`；某个改造项目的进度与待办在 `/opt/data/chat-layer/PLAN-v2-astrbot-direct.md`（配套：人格草案 `astrbot-persona-prompt.md`、工具插件 `plugin/hermes_delegate/`）
- **分段正则不要加 `\n`**（主人明确否掉）：那等于纵容「拿换行代替标点」，正好是他要避免的风格。所以只能往提示词治——聊天门 SOUL 「分段」小节已写成硬规矩（每条短消息带 `。？！~…` 或句号收尾，**优先级压过**「大部分句子不加句号」那条偏好；换行与语气词收尾都不切）。改 AstrBot 切分正则 / 重启它属于动聊天门链路，要主人直接点头。
  - **分隔符当前定案用 `⁂`（U+2042）**（冷门、非正则元字符、日常与中文文档都不打）：切分字符类加它 ＋ `content_cleanup_rule: "[⁂※⸮]"` → 只当分隔、不出现在消息里，不必逼主人写「。！？」（实测 3 句 → 3 气泡、无可见标点）。早先用的 `※` 已废弃（中文文档里常见 → 发文档会被误切）。**清理层与切分层必须配套**：符号不进切分字符类就永远不切，只写 cleanup 等于没改。候选与选符号判据见 `references/astrbot-platform-ops.md`

## 宿主操作的坑（血泪）

- **`pkill -f <模式>` / `pgrep -f <模式>` 会匹配到自己**：外层 `bash -c` 的命令行里就含那个模式（尤其 `rm -f /tmp/<名字>` 这种把文件名写进命令行的），`xargs kill -9` 会把自己干掉（实测 exit -15、-9 各一次）。必须杀时用括号技巧（`pgrep -f 'foo_[b]ar'`）或先列 PID 再按 PID 杀
- **别把脚本内容塞进 SSH 命令串**：heredoc 会被外层 eval 拆散，落地成 0 字节空文件（`wc -l` 一看就是 0）。正确三步：本地 `write_file` 写好 → `ssh 'cat > /path' < 本地文件` → 另一条命令启动，并用 md5 核对落地。**内联 `python3 -c "…"` 走 SSH 同样会被撕**（报 `unexpected EOF while looking for matching` 或命令被静默截断）→ 一律写成文件再执行：写到已挂载目录（如宿主 `<映射>/stack/astrbot/data/mianmian-tmp/`，容器内 `/AstrBot/data/mianmian-tmp/`）再用 `docker exec … python3 <该路径>` 跑
- **宿主 `/tmp` 易失**：`/tmp/mian_sudo.sh`（sudo askpass）会被清掉，用之前先探在不在，不在就从容器内 `/opt/data/scripts/mian_sudo.sh` 重新投放（`ssh 'cat > /tmp/mian_sudo.sh && chmod 700 …' < 本地文件`）
- **SSH 到宿主后 `docker` 必须走 `sudo -A`，裸 `docker` / `sudo -n` 都会失败**：`棉棉` 账号不在 docker 组，裸 `docker ps` 报 `permission denied … docker.sock`；`sudo -n` 报 `sudo: a password is required`（这是缺 askpass，不是缺权限）。可用写法只有一种：`export SUDO_ASKPASS=/vol1/1000/<USER> sudo -A docker …`（容器内路径视图 `/opt/data/scripts/mian_sudo.sh`）——`docker ps/logs/exec/inspect` 全部同一条。别因为看到 `permission denied` 就判定「docker 通道不通」而放弃复核。
  - **`-A` 是必须显式带的那一半**：`export SUDO_ASKPASS=…; sudo cat <file>`（漏 `-A`）照样报 `sudo: a terminal is required to read the password`——错误文案指向「没有终端」，实际病根是没让 sudo 去用 askpass。**同一条命令的赋值前缀写法最稳**：`SUDO_ASKPASS=/vol1/1000/<USER> sudo -A cat <file>`（`export` + `sudo -A` 亦可）。
  - **读 root 属主 600 的容器运行时文件走这条路**：`stack/<服务>/…` 下的 `data/`、`patches-boot.log` 这类文件容器内 hermes 读是 `Permission denied`（`find` 能列出路径不代表能读），宿主侧 `sudo -A cat` 一把就行；别为此 chown，也别往 `/tmp` 传口令副本。
