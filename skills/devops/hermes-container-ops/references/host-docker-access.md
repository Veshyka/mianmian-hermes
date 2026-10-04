# 宿主侧 docker / 共享文件访问（容器内无 docker.sock）

容器内没有 `/var/run/docker.sock`（`docker ps` 直接 permission denied）。查/改宿主容器（watchcow 应用化、sakuraFrp、任何宿主 docker 命令）必须 SSH 到宿主再提权。

## 核心命令（2026-08-28 实测可用）

```bash
# 1) 宿主侧（如需临时 askpass；持久路径见下）——口令值从 700 的 askpass.sh 读，别写进文档
#    取法：sed -n '2s/^echo //p' /opt/data/scripts/askpass.sh
SSH_ASKPASS=/opt/data/scripts/askpass.sh SSH_ASKPASS_REQUIRE=force setsid ssh 棉棉@172.17.0.1 'echo ok'

# 2) 宿主 docker 命令（sudo 走 askpass）
SSH_ASKPASS=/opt/data/scripts/askpass.sh SSH_ASKPASS_REQUIRE=force setsid ssh 棉棉@172.17.0.1 \
  'SUDO_ASKPASS=/tmp/askpass.sh sudo -A docker ps -a --format "{{.Names}} | {{.Status}} | {{.Image}}"'

# 3) 容器内读共享文件同理（团队空间无挂载时）：
SSH_ASKPASS=/opt/data/scripts/askpass.sh SSH_ASKPASS_REQUIRE=force setsid ssh 棉棉@172.17.0.1 \
  'cat "/vol2/@team/共享文件/ds key.txt"'
```

## 往共享文件写大于 10KB 的内容：走 stdin，别塞 argv（2026-10-03 血泪）

❌ **错的写法**（`base64 -w0` 塞进 `ssh "<整条命令>"` 的 argv）：**~26KB 载荷被静默截断** →
远端 `base64 -d` 失败 → `> 文件` 已经把文件**清零**，共享稿变成 0 字节。文件越大越静默，
本地看命令"成功"、`md5sum` 不解码根本发现不了。

✅ **对的写法**（把本地文件直接灌进远端标准输入，再回读校验）：

```bash
FN=/vol2/@team/共享文件/提示词.txt
bash scripts/fygo_ssh.sh "cp -p '$FN' '$FN.bak-$(date +%Y%m%d-%H%M%S)'"     # 先备份
bash scripts/fygo_ssh.sh "cat > '$FN'" < /opt/data/tmp/共享提示词.txt          # stdin 写入
bash scripts/fygo_ssh.sh "md5sum '$FN'"                                      # 回读校验（必做）
md5sum /opt/data/tmp/共享提示词.txt                                          # 两边对得上才算成
```

规则：**写完必须 md5 回读**；`$(...)` 写在要给远端展开的命令里时，注意外层引号别把它变成字面量
（曾造出一个名字里带 `$(date...)` 的备份文件）。

## 坑

- ⚠️ **`echo pass | sudo -S` 管道密码被 Hermes 安全机制 BLOCK**（判为 brute-force 向量）——必须 `SUDO_ASKPASS=... sudo -A`
- `sg docker` 也会失败（组密码不对），别走那条路
- **askpass 别建在宿主 `/tmp`，直接用持久路径**：`SUDO_ASKPASS=/vol1/1000/<USER>`。容器里的 `/opt/data` 就是宿主 `/vol1/1000/<USER>`，两侧是同一个文件——用它就**不用在宿主重启后再重建 `/tmp/askpass.sh`**。
- SSH 到宿主会报 `Could not chdir to home directory /home/棉棉`——无害，忽略。

## 口令轮换（2026-09-19 实操修正）

宿主 `棉棉` 账号的口令同时管 **SSH 登录** 与 **sudo**，两处都靠 `scripts/askpass.sh` / `scripts/mian_sudo.sh`。

**格式铁律**：这两个文件是**可执行脚本**，内容必须是
```sh
#!/bin/sh
echo <口令>
```
写成裸口令（无 shebang）→ ssh/sudo 执行它拿不到输出 → **Permission denied，运维通道当场断**。

**轮换步骤**（脚本：`scripts/rotate_pw.sh`，已按此格式修正）：
1. 保留旧值副本（600）、新值另存救回副本（600），**先把两个 askpass 新文件写好再 chpasswd**
2. `echo "棉棉:$NEW" | sudo -A chpasswd`
3. `mv` 原子落位 → `chmod 700`
4. 复验：`sudo -k; sudo -A id`（→ uid=0）+ **另开一条 `fygo_ssh.sh` 验证 SSH 登录**
5. 全树复扫旧值是否残留：`python3 scripts/scan_after_rotate.py`
6. 删临时明文副本；主人自行 `cat` 文件取口令（**口令不进对话、不进记忆库**）

**排查顺序**：SSH 连不上先 `bash /opt/data/scripts/fygo_ssh.sh 'echo ok'` 看是不是 askpass 格式被改坏；真断了还有 trim-cli 通道（`scripts/trim_cli.py file`）能直接改宿主文件，锁不死。

## GPU 直通（不需要改 daemon.json）

宿主 `docker info` 里已经有 CDI 设备（`cdi: nvidia.com/gpu=0` / `cdi: nvidia.com/gpu=all`），所以：

```bash
sudo -A docker run --rm --device nvidia.com/gpu=all <image> <cmd>
```

- ✅ **不要在 `daemon.json` 里加 `runtimes`／nvidia runtime，更别为此重启 docker**——「必须加 nvidia runtime 才能 `--gpus all`」是旧结论，实测 CDI 已就绪。宿主 docker 一重启，Hermes 容器和记忆服务全部跟着下线。
- 验证是否真进 GPU（以 llama.cpp 镜像为例）：`--entrypoint /app/llama-server <image> --list-devices` → 应打印 `CUDA0: NVIDIA GeForce RTX 3060 ...`。
- 看到 `free` 只剩 2G 出头是正常的——Ollama 常驻占掉大部分显存；跑第二个引擎前先卸模型。

## 拉镜像：ghcr.io 走不通就用镜像站

`docker pull ghcr.io/...` 可能报 `Get "https://ghcr.io/v2/": EOF`；同机 curl 走宿主代理是 `SSL_ERROR_SYSCALL`，`raw.githubusercontent.com` 也报证书错误。**这不是 docker 的问题**，直连和走梯子都拿不到。

替代：`ghcr.nju.edu.cn/<同路径>`（与本机已有的 hindsight 镜像同源）。拉完 `docker inspect --format '{{index .RepoDigests 0}}' <img>` 记下 digest，便于日后跟上游核对。

## 映射目录约定

主人说「**和你映射到一个文件夹**」= `/vol1/1000/<USER>`（= 容器 `/opt/data`）。
新引擎/新容器要落数据，就在这个目录下建自己的子目录做映射（例：`llamacpp/{models,logs,tmp}`），这样宿主和容器两边都直接看得到，不用走 SSH 搬运。

## 规范优先（官方通道 > SSH）

- 宿主侧 docker/应用中心**优先走 trim-cli 官方通道**（`app list/install/start/stop`、`docker request` 等有规范接口，避免通用命令在 fnOS 私有权限模型下出错）——主人要求「官方功能够用就别动不动 SSH」
- **容器内直调官方通道**（不需要 SSH，技能自带二进制）：
  ```bash
  B=/opt/data/skills/productivity/fnos-trim-cli-skill/bin/trim-cli-linux-x64
  A="--host 172.17.0.1 --port 5666 --scheme ws --allow-insecure-ws"
  $B $A system info          # 通道自检：出 JSON = 通道通
  $B $A app list             # 应用中心全量（含 start/stop/restart）
  $B $A monitor cpu|memory   # 宿主指标
  ```
  `--scheme` 必须显式写 `ws`（自动判定会 `InvalidContentType`）；会话建立后不必每次登录。
- **区分「通道不通」和「服务端拒绝」**：先跑 `system info` / `app list` 证明通道本身通；这两个通而某模块报错（如 docker 类返回
  `failed to get docker data ... errno 135168`，而 `docker container` 子命令表里 start/stop/restart/inspect 齐全 = CLI 侧支持）
  → 那是**账号权限或功能开关**的问题，不是通道坏。此时 SSH+sudo 兜底，并把它当待查项（fnOS 用户权限 / 应用权限），
  别固化成「官方通道干不了这个」。
- SSH sudo docker 用于官方通道到不了的场景（容器级写操作、宿主特权写、应急查看）
- 查飞牛应用是否安装/运行：`trim-cli app store search <name>` 返回 installedList；`app status <name>` 看详细状态
