# 属主/权限变更台账（Hermes）

**为什么要这个文件**：OpenClaw 时代没有官方 skill，属主问题是靠直接改属主解决的，那些改动**没有记录**；现在统一记在这里，保证任何一次改动都能查到（谁改的、为什么、怎么回滚）。

**怎么查**：
```bash
grep -n "2026-09" /opt/data/ops-changelog/ownership-changes.md      # 按时间查
grep -n "cache" /opt/data/ops-changelog/ownership-changes.md        # 按路径查
grep -c "^- " /opt/data/ops-changelog/ownership-changes.md          # 已登记条数
```

## 背景（2026-09-19 普查）

- 容器内身份：`uid=1003(hermes) gid=1001(hermes)`；`HERMES_HOME=/opt/data` = 宿主 `/vol1/1000/<USER>`
- **990 = 旧 OpenClaw 应用用户**（`trim.openclaw:AppUsers`）。迁移（2026-08-24）时容器 uid 从 990 变成 1003，一批顶层目录留在 990 名下
- 后果实例：`cache/` 归 990/755 ⇒ Hermes 建不了 `cache/vision` ⇒ **识图整条挂掉**（`Native vision failed: Permission denied`）
- 基线：`/opt/data` 下不属于 hermes 的条目 **33 个**，全部 mtime ≤ 2026-08-24（迁移当天），无新增内容

## 授权（主人 2026-09-19）

> 「原 openclaw 导致的属主问题你可以确定文件不属于我之后改，但是改完一定要落实到某处纪录」

即：**先确认不属于主人的数据** → 可以改 → **必须登记在本文件**。

## 变更记录

### 2026-10-04 · 容器时区 UTC → Asia/Shanghai（主人授权）

- **授权**：主人 2026-10-04 看到「凌晨 4-7 点她连发 4 条」的时区事故后回：「**有影响可以改**」。
- **为什么**：Hermes 容器跑在 UTC（`TZ` 空、`/etc/localtime -> Etc/UTC`），
  她那边「北京时间 00:30–07:00 不触发」被算成了 08:30–15:00；日志时间戳也比主人的表慢 8 小时。
- **处置**（容器内可写层，非宿主文件）：
  1. `ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime`
  2. 新建 `/etc/timezone` 内容 `Asia/Shanghai`
- **复核（实测）**：`docker exec hermes date` → `Sun Oct  4 10:35:08 CST 2026`（与主人的表一致）；
  重启 `gateway-chat` 后日志时间戳变成北京时间（`2026-10-04 10:35:22`）✓
- **回滚**：`ln -sf /usr/share/zoneinfo/Etc/UTC /etc/localtime; rm -f /etc/timezone` → 重启网关生效。
- **⚠️ 耐久性（重建容器会丢）**：这是容器**可写层**里的改动 —— `docker restart` 保留，
  **`docker rm` + 重新 run / compose 重建会丢**。而且这容器**没有 compose 标签**（手工 run 起的），
  宿主上也没找到创建脚本。**将来重建 Hermes 容器时，务必带 `-e TZ=Asia/Shanghai`**
  （或重建后按上面两步补软链，再重启网关），否则静默窗会重新错 8 小时。
  代码层已不依赖它（`plugins/onebot/onebot_time.py` 显式用 Asia/Shanghai），所以丢了也不影响她，
  只是日志时间戳又变 UTC。

### 2026-09-19 · 迁移残留属主修复（第一批 14 项）

全部经查证为「Hermes 自身运行态/缓存」或「我自己写的交接文档」，非主人数据：

| 路径 | 原属主 | 新属主 | 内容/为什么 | 回滚 |
|---|---|---|---|---|
| `cache/` | 990:901 | 1003:1001 | Hermes 缓存 6 个 json（banner/model_catalog/reasoning_caps…），阻塞识图 | `sudo chown -R 990:901 <path>` |
| `image_cache/` | 990:901 | 1003:1001 | 空目录，Hermes 图片缓存位置 | 同上 |
| `audio_cache/` | 990:901 | 1003:1001 | 空目录，音频缓存位置 | 同上 |
| `bin/` | 990:901 | 1003:1001 | 空目录 | 同上 |
| `plugins/` | 990:901 | 1003:1001 | 空目录，插件安装位置（写不进去 = 插件装不了） | 同上 |
| `sandboxes/` | 990:901 | 1003:1001 | 空目录（仅 singularity 子目录） | 同上 |
| `photon/sidecar` | 990:901 | 1003:1001 | photon 功能的 node sidecar（index.mjs/package.json 等 4 文件） | 同上 |
| `platforms/` | 990:901 | 1003:1001 | pairing 数据（其下文件本就是 hermes，只有父目录是 990） | 同上 |
| `pastes/` | 990:901 | 1003:1001 | Hermes 粘贴暂存（内含一条 8/24 的粘贴，内容是棉棉自己的输出） | 同上 |
| `install_id` | 990:901 | 1003:1001 | 600 权限运行态文件，原本连读都读不了 | 同上 |
| `projects.db` | 990:901 | 1003:1001 | 45KB，旧 projects 库 | 同上 |
| `state.db.fts_rebuild.lock` | 990:901 | 1003:1001 | 陈旧锁文件，属主不对可能导致抢锁失败 | 同上 |
| `00-Hermes部署报告.md` | 990:901 | 1003:1001 | 棉棉写的迁移文档 | 同上 |
| `00-交接总文档.md` | 990:901 | 1003:1001 | 棉棉写的迁移文档 | 同上 |

执行命令（宿主 sudo，经 `scripts/fygo_ssh.sh` 通道）见当次会话；等效命令：
```sh
export SUDO_ASKPASS=/vol1/1000/<USER>
sudo -A chown -R 1003:1001 /vol1/1000/<USER>
sudo -A chown 1003:1001 /vol1/1000/<USER>
```

### 2026-09-19（晚）· `backups/` 目录本身（主人单独授权）

| 路径 | 原属主 | 新属主 | 内容/为什么 | 回滚 |
|---|---|---|---|---|
| `backups/`（**仅目录本身，非递归**） | 990:901 | 1003:1001 | 里面是**主人的真备份**，只改目录 inode 让 Hermes 能往里写新备份（原来 `touch` 报 Permission denied）。**未动其中任何文件** | `sudo chown 990:901 <path>` |

- 授权原话（主人 2026-09-19）：「备份属主按你最方便办法改，改完纪录可查」
- 执行：`sudo -A chown 1003:1001 /vol1/1000/<USER>`（宿主 sudo，经 `scripts/fygo_ssh.sh`）
- 副作用与处理：chown 后 fnOS 把权限位从 755 重算成 705，已 `chmod 755` 复原
- 复验：容器内 `stat` 得 `755 1003:1001`，`touch /opt/data/backups/.wt_*` **成功**（改前同命令 Permission denied）
- 子项抽查：`backups/config` 仍为 hermes 所有、内容未动

### 2026-09-19 · 凭据文件权限收紧（主人授权「错误口令自己解决优化」）

| 路径 | 原权限 | 新权限 | 内容/为什么 | 回滚 |
|---|---|---|---|---|
| `scripts/mian_sudo.sh` | 755（**other 可读**） | 700 | 内含 SSH/sudo 口令，与 `askpass.sh` 同一内容（同 sha256）；755 意味着本机任何账号都能读到口令明文 | `sudo chmod 755 <path>` |

- 复核：改后 `sudo -A id` 仍返回 `uid=0(root)`（提权链路未被改坏）
- 现状：`askpass.sh` 700、`mian_sudo.sh` 700，均 `棉棉:Users`
- 建议（主人决定）：该口令曾以明文存在于记忆库 + 755 文件约 3 周，**彻底解法是换一次口令**，棉棉不能代改

### 2026-09-19 · 轮换 `棉棉` 账号口令（主人授权「自己改合适」）

**为什么**：该口令以明文存在于 Hindsight 记忆库（已清）+ `mian_sudo.sh` 755（other 可读）+ 11 个文档/历史文件里，且最后一次改密在 7 月 22 日。

| 项目 | 内容 |
|---|---|
| 操作 | 宿主 `棉棉` 账号（uid 1003，SSH 登录 + sudo 共用）改密为 24 位随机值；同步更新 `scripts/askpass.sh`、`scripts/mian_sudo.sh` |
| 执行 | `bash /opt/data/scripts/rotate_pw.sh`（脚本已落盘，修正版；宿主侧经 `fygo_ssh.sh` 通道） |
| 口令值 | **全程不打印、不进对话**，只存在于两个 700 文件里（主人可自行 `cat` 查看） |
| 备份/回滚 | 旧值临时存 `$D/.oldpw.600`、新值 `$D/.newpw.600`（用完即删）；回滚 = 用旧值 `sudo chpasswd` 改回 |
| 复验 | ① 正测：新口令 SSH 登录成功、`sudo -k && sudo -A id` → `uid=0` ② 负测：旧口令 SSH **已失效**（`Permission denied`）③ 全树复扫：旧口令只剩 5 个 curator 快照 blob（值已失效，无害），新口令只出现在那两个文件 |
| 顺带清理 | 抹掉 11 个文件里的旧口令明文：3 个 skill references、8 个 handover-assets / `.hermes_history` / `.archive`（用哈希比对定位，不打印值） |

## 验证结果（2026-09-19，执行后复验）

| 检查项 | 结果 |
|---|---|
| `chown` 两组命令 | 均 exit 0 |
| 非 hermes 条目数 | **33 → 6**（剩下的正是下面"没改的"两项及其子项） |
| `cache/` 可写 | ✓（`touch` 通过） |
| `get_hermes_dir('cache/vision','temp_vision_images')` | 解析回 **`/opt/data/cache/vision`**，uid 1003，可创建 ✓ |
| 中间用的 legacy 回退目录 `temp_vision_images/` | 已用 `trash-put` 入回收站（不是硬删） |
| **识图端到端** | ✓ 复验通过（图正常进视觉通道，不再是 `Permission denied`） |

## 明确**没有**改的（连同原因）

| 路径 | 原因 |
|---|---|
| `backups/`（目录本身 990） | ✅ **已于 2026-09-19 晚改**（见上「变更记录」最后一节）：只改目录本身、未动其中文件。`backups/config` 本就归 hermes，配置备份一直正常写 |
| `hindsight-eval/export_20260919_0209.zip`（root.root，14MB） | Hindsight 记忆库时点快照，主人尚未拍板是否纳入清理；属主 root 意味着我删不动 |
| 宿主 `/vol1/1000/<USER>`、`/vol2` 其它路径 | 不在 `/opt/data` 内，与本容器无关（走官方 trim-cli 通道访问） |

## 2026-09-20 映射规范化（`stack/` 归位 + Hindsight 卷迁出）

主人要求「docker 映射要做好、都映射到你那个文件夹、要有合格分类（包括 bot 和 qq 那俩）」。

| 路径 | from → to | 理由 | 回滚 | 验证 |
|---|---|---|---|---|
| `chat-layer/{napcat/config,ntqq}` → `stack/napcat/{config,ntqq}` | 属主**不变** 1003:1001（`mv` 同盘） | 容器运行时数据与代码分层：`stack/`=数据、`chat-layer/`=代码文档 | `mv` 回原位 + 改 compose 两行 | napcat 起来后免扫码快速登录成功、AstrBot 适配器自动重连 |
| `chat-layer/astrbot/data` → `stack/astrbot/data` | 属主**不变** 1003:1001 | 同上 | 同上 | AstrBot 面板与插件配置读到了（适配器连接、插件自测全通过） |
| docker 卷 `hindsight-data`（6.8G，`/vol2/docker/volumes/`）→ `stack/hindsight/data` | 内容属主**保持原值 1000:1000**（`cp -a` + `chown -R 1000:1000`） | 唯一没映射进主人文件夹的数据块 | 旧卷已按主人指令删除；回滚靠 `stack/hindsight/data` 自身（另有 08-22/09-19 备份 zip 在内） | `du` 6.8G 两侧一致、文件数 4673=4673、`/health` healthy、`bank mianmian-history` **fact_count 28469**、Hermes 侧 `hindsight_recall` 端到端通过 |
| `stack/hindsight/`（目录本身） | root:root，`chmod 755` | 挂载点只需容器内可读；父目录无需给 1000 写权限 | `chmod 700` | 容器 healthy（bind mount 的路径解析由 daemon 做，父目录不通不影响） |
| `stack/hindsight/docker-compose.yml`（新建） | root:root 700 → **1003:1001 644** | 纳入 Hermes 文件管理，日后编辑不必 sudo | `chown root:root && chmod 700` | 已能直接读改；`docker compose config -q` 通过 |

**顺带清理**：`chat-layer/{napcat,astrbot}` 空壳目录、旧 `qrcode.png`/`qrcode_big.png`、空的 `chat-layer/backup/` → 一律 `mv` 进 `.trash/chat-layer-leftover-<ts>/`（未硬删）。
**残留卷清理**：`hindsight-data-2560`（2026-09-19 02:10 建的 2560 维实验残留，115M，仅 skeleton 无数据）已 `docker volume rm`。
**未动**：`qb`、`woc-wx` 等其它应用的卷与本容器无关，不在本次范围。

### 2026-09-21 · llama-vision 退役清理（主人定「基本上不会用了」）

背景：`llama-vision`（minicpm-v4.6:1b @8083）已于 2026-09-21 停用（Bonsai 27B 自带 mmproj 顶掉看图）。
本次核查确认：**容器早已不存在**（`docker container ls` 中无 llama-vision，12 个容器只剩 llama-extract / llama-embed），
Hermes `config.yaml` 的 `auxiliary.vision.model` 已指向 `bonsai2-27b`，**无任何活配置引用 minicpm**。

| 对象 | 处理 | 理由 | 回滚 | 验证 |
|---|---|---|---|---|
| `llamacpp/models/minicpm-v4.6-1b-Q4_K_M.gguf`（505M） | `trash-put` → `~/.local/share/Trash` | 被 Bonsai mmproj 顶掉，主人定不会再用 | `trash-restore`（7 天内）或重新下载 | `trash-list` 可见，原路径已消失 |
| `llamacpp/models/minicpm-v4.6-1b-mmproj.gguf`（1.1G） | 同上 | 同上 | 同上 | 同上 |
| `llamacpp/vision-on.sh`（2.6K） | 同上 | 该实例的按需启动脚本，实例已无 | 同上 | 同上 |
| `llamacpp/docker-compose.yml` 中 llama-vision 的注释段与 `--no-webui` 尾行 | 删除注释，替换为 2 行退役说明 + 回退指针 | 配置项已无用；保留回退指引 | 备份 `docker-compose.yml.bak-before-vision-cleanup-20260921-102655` | **剥掉注释后逐行 diff 与备份完全一致**（`scripts/verify_compose_diff.py`），生效配置零改动 |

**未动**：`models/qwen3-embedding-4b-Q4_K_M.gguf`（2.4G，现役 llama-embed 用）；
`llamacpp/logs/迁移验证报告.md`（历史记录，含 mmproj 挪 CPU 的实测数据，按"历史不改"原则保留）。
**服务未重启**：仅注释变更，`8081/8082/8888` 三次 health 均 200。

#### 同日追加 · 删除旧抽取模型 qwen3-8b（主人语音：「旧模型可以删了，新模型体积差不多但性能明显更好」）

| 对象 | 处理 | 理由 | 回滚 | 验证 |
|---|---|---|---|---|
| `llamacpp/models/qwen3-8b-Q4_K_M.gguf`（**4.9G**） | `trash-put` → `~/.local/share/Trash` | Bonsai 27B 全面顶替（抽取 6/6 合规 + 因果边 11 vs 7 + 多模态）；主人确认不再用 | `trash-restore`（7 天内）或重新下载 | `trash-list` 可见；`models/` 7.2G → 2.4G |

**删除前查证（无活引用）**：`stack/hindsight/docker-compose.yml` 的 `HINDSIGHT_API_LLM_MODEL=bonsai2-27b`；
Hermes `config.yaml` 的 `auxiliary.vision.model=bonsai2-27b`。
残留的 `qwen3:8b` 字样只在**注释**与两个走 **ollama**（非此 gguf）的旧脚本里：`scripts/hindsight_switch_model.sh`、`scripts/hindsight_test_2560.sh`、`llamacpp/hindsight_rollback_ollama.sh`。
**回退路径的如实说明**：llama.cpp 那条回退（改回 qwen3-8b gguf）**从此需要重新下载 4.9G**；ollama 那条回退依赖 ollama 服务，而 **ollama 已由主人主动关闭**（`172.17.0.1:11434` 连不上是预期行为，非故障）。本机 `/vol1`、`/vol2` 下未找到 ollama 模型库目录。

## 2026-09-21 · 启用 Hindsight strict schema（属主改动：无）

- **文件**：`stack/hindsight/docker-compose.yml`（属主 `hermes:hermes`，**无需 sudo**，直接就地写入）
- **改动**：新增一行 env `HINDSIGHT_API_LLM_STRICT_SCHEMA_RETAIN=true`，并把原「备选（未启用）」注释块替换为启用说明（4 行 → 18 行）。
- **坑（记下来省下次）**：`stack/hindsight/` **目录是 root 属主**，所以 `patch` 工具会在同目录建 `.hermes-tmp.*` → `Permission denied`；`cp` 想在同目录建备份文件也会被拒。**但 compose 文件本身可写**。解法：备份写到 `tmp/`，改动用「就地 open(w) 写」（脚本 `scripts/enable_strict_schema.py`，带 md5 校验 + 匹配数断言）。
- **属主变更**：无。全程未改任何属主/权限。
- **宿主侧操作**：`sudo -A docker compose up -d` 重建 hindsight 容器（走 SSH + `SUDO_ASKPASS=/vol1/1000/<USER>`；**注意不是** `askpass.sh`，用错会静默失败）。

## 2026-09-22 · 聊天门文件权限落地（跨服务权限改动）

**动因**：主人「还是给文件权限吧，我有时候发点小东西啥的，她拍出去调研的结果也要看文件」。

| 对象 | 改动 | 理由 | 回滚 | 验证 |
|---|---|---|---|---|
| 宿主 `stack/astrbot/data/temp`、`.../attachments`（原 `700 root:root`） | **`chmod 755`** | AstrBot 以 root 落盘附件，Hermes 侧以 uid 1003 运行 → 否则整个目录读不到 | `sudo chmod 700 <路径>` | hermes 容器内 uid 1003 读到 JPEG 魔数 `ff d8 ff e0`、目录可列 |
| 上述目录内的现有文件 | `chmod 644` | 同上（文件原为 `-rwx------`） | `sudo chmod 600 <文件>` | 同上 |
| `chat-layer/plugin/hermes_forward/main.py`（root:root 700） | 打补丁：新增 `_relax_perms()`（落盘后 600→644），图片/文件分支各加一次调用；`get_file(allow_return_url=True→False)` 强制本地路径 | 以后的新附件仍会以 root 600 落盘；原参数可能返回 URL，Hermes 侧读不到 | `sudo cp -a main.py.bak-before-perms-20260922-225308 main.py` + `docker restart astrbot` | `py_compile` 通过；`_relax_perms` 实测 600→644 且 hermes 容器随即读到；离线 `self_test.py` 全通过 |

**⚠️ 关键坑（本次踩到）**：本机 btrfs 挂载用的是 fnOS 自定义 **`trimacl`**（`/proc/mounts` 可见，**没有标准 `acl`**），
**`setfacl` 写的 POSIX ACL 在这个挂载视图里不生效**——实测：宿主上 `sudo -u '#1003'` 能读，容器内同 uid 被拒；
容器侧 `ls -ld` 能看到 `+`、`os.listxattr` 也能看到 `system.posix_acl_access`，但内核不放行。
→ **跨容器授访问权只能用 mode 位（chmod）**。ACL 实验已回滚（`sudo setfacl -b -R <目录>`），最终方案 = chmod + 插件侧 chmod。

**未动**：napcat 数据目录、astrbot 的 `cmd_config.json`/`data_v4.db` 等（只动了 temp/attachments 两个目录的 mode）。

## 2026-09-23 · AstrBot 重建后把 `astrbot_plugin_qzone` 移出加载路径（临时处置）

**动因**：为让源码补丁自愈在线上真跑，`git`-外操作 `docker compose up -d --force-recreate astrbot`
重建了 AstrBot 容器（09-20 建的旧容器 `Entrypoint=null`、没有 patches 挂载 → 自愈从未生效过）。
重建后 AstrBot 启动时要**重新 pip 装插件依赖**（依赖在可写层，见下方「已知属性」），
`astrbot_plugin_qzone` 的 `pillowmd`(20.2MB) 下载 3m50s 后 **`fonttools` 卡死 >10 分钟**
（`docker stats` net=0、CPU 0.5%），整个启动流程卡在插件加载 → **聊天门（平台适配器）一直不启动**。

| 对象 | 改动 | 理由 | 回滚 | 验证 |
|---|---|---|---|---|
| 宿主 `stack/astrbot/data/plugins/astrbot_plugin_qzone`（root:root 755） | `mv` 到同卷 `stack/astrbot/data/plugins_disabled/` | 让启动流程跳过这个装不上依赖的插件，先恢复聊天门可用 | `sudo mv .../plugins_disabled/astrbot_plugin_qzone .../plugins/` + `docker restart astrbot` | 移出后 `docker restart astrbot` → 08:14:16 `aiocqhttp(OneBot v11) 适配器已连接`、`hermes_report/hermes_memory/hermes_delegate` 均加载 ✓ |

- **属主/权限**：未改（mv 保留 root:root 755）。
- **凭据**：无。
- **已知属性（不是本次引入，但重建必踩）**：AstrBot 插件 pip 依赖写在容器可写层
  （`pip_installer.install()` 仅当 `is_packaged_desktop_runtime()` 为真才写持久卷
  `/AstrBot/data/site-packages`；Docker 下 `utils/runtime_env.py:9` 恒假）→ **每次重建都要重装**。
- **未做（留给主人拍板）**：给 astrbot 加 `ASTRBOT_DESKTOP_CLIENT=1` + `ASTRBOT_ROOT=/AstrBot`
  让依赖落到持久卷 `data/site-packages`（必须成对设置：desktop 模式下 `get_astrbot_root()` 会返回
  `~/.astrbot`，只设前者会把 data 路径带飞）。涉及 AstrBot 运行模式语义，未擅自启用。
- **另（已完成）**：`qzone` 依赖用 `docker exec astrbot pip install --no-cache-dir --timeout 30 -i https://pypi.tuna.tsinghua.edu.cn/simple …` 单独补装（**76 秒**，避开主进程里卡死的那个线程与阿里云源）→ `import fontTools, apilmoji, json5, pillowmd` 全部通过 → 插件 `mv` 回 `plugins/` + `docker restart astrbot` → 08:16:48 日志确认 `Plugin astrbot_plugin_qzone (v3.1.0) by Zhalslar` 加载、适配器已连接。
- **顺带记两个坑**：① 这个发行版的模块名是 **`fontTools`（大写 T）**，`import fonttools` 必然 `ModuleNotFoundError`（我据此误判过一次「包没装上」）；② 同一个包，**清华镜像 22.6 MB/s vs 阿里云 107 kB/s** —— 卡死那次就是阿里云连接挂住（`docker stats` net=0 是判据）。

## 历史说明

OpenClaw 时期（2026-08-24 之前）为让框架能跑，有过未经记录的属主改动（主人原话：「以前没有官方 skill 的时候我也没办法只能允许你改属主，有些就是那个时候改的」）。**那段历史无法追溯**，本台账以 2026-09-19 的普查结果为基线，之后逐条登记。

## 2026-09-23 · 告警降噪：health_all 签名去重 + QQ 小号自检启动宽限期

**动因**：处理两条真实告警（聊天门 A2A 派单）。①`ab-errors` 误报 + 回执每两分钟刷一次（自我回灌回路）；
②QQ 小号(OneBot)自检在「网关/NapCat 重启空窗」里把「协议端还没连上」判成故障。
详细报告：`deliverables/2026-09-23-alarm-triage.md`

| 对象 | 改动 | 理由 | 回滚 | 验证 |
|---|---|---|---|---|
| `profiles/chat/plugins/onebot/health.py` | 新增阈值 `client_grace_seconds=180` + 宽限逻辑（宽限内「从未连上」降 warn、「启动至今没收到事件」静默；超时才 fail） | 旧判据只看「有没有连过」不看进程活了多久 → 重启后 ~35s 空窗即误报 | 覆盖回 `memory/backups/onebot-health.py.bak-20260923T075329Z` | 用 07:38:34 真实读数复现：旧 fail → 新 warn；进程跑满 10min 仍 fail |
| `profiles/chat/plugins/onebot/adapter.py` | `health_state()` 输出 `started_ts`（进程启动时刻） | 宽限期需要基准 | 同上 `onebot-adapter.py.bak-…` | 116/116 单测通过 |
| `profiles/chat/plugins/onebot/tests/*.py` | +5 项宽限期用例；去重用例越过宽限期 | 判据变了必须有用例钉住 | 见 `memory/backups/` | `bash tests/run_tests.sh` OK（116 项） |
| `chat-layer/plugin/hermes_onebot/`（health/adapter/tests 同名文件） | 与加载副本同步 | 源码副本必须等于加载副本（否则下次同步把改动冲掉） | 同上备份 | md5 一致 |
| `scripts/health_all.py` | +告警**签名去重**（同一异常未恢复前只发一次，默认 4h；恢复即清零）+ main() 接闸门 | 「每条都发」导致刷屏；源头的 :8098 回执通道已被并行会话删除 | 覆盖回 `memory/backups/health_all.py.bak-pre-dedup-20260923T0755Z` | `scripts/check_health_dedup.py` 8/8；总览 16/16 绿；灵敏度自检 17/17 |
| `scripts/check_health_dedup.py`、`scripts/check_health_cron_dedup.py` | 新增两个离线回归脚本 | 去重机制要可复核 | 删文件即可 | 8/8、6/6 |

- **属主/权限**：未改任何文件属主（只改内容，保留 hermes:hermes）。
- **凭据**：无（未碰 token/密码）。
- **未动**：NapCat 配置（`onebot11_*.json` 本来就正确，只有 `enable=true` → `ws://127.0.0.1:6700/ws`）；
  AstrBot 的 `cmd_config.json` / `data_v4.db`。
- **未做（留聊天门/主人决定）**：重启 `gateway-chat` 让宽限期修复生效
  （`/command/s6-svc -r /run/service/gateway-chat`，约 20s；那是聊天门自己的进程，且主人正在用那条线）。

---

## 2026-10-02 旁路由（ImmortalWrt / Newifi D2）地址迁移 + 回退路径修复 + SSH 加固

**动因**：收尾 NAS 侧观测到的二层异常（`.2` 上有别人家设备抢答 ARP → 指向它的客户端整段丢包，已受控实证），并把"保险绳"（extroot 回退路径）修成真能用的、顺手关掉路由器的空密码洞。
**详细报告**：`/opt/data/diag/l2-side.md`

| 对象 | 改动 | 理由 | 回滚 | 验证 |
|---|---|---|---|---|
| 路由器 `network.lan.ipaddr` | `192.168.1.2` → `192.168.1.250` | `.2` 被天邑康和 CPE（`90:52:bf:*`）抢答 ARP 且只答不转发 = 黑洞；指向 `.2` 的客户端 100% 丢包（10 轮受控实证） | `uci set network.lan.ipaddr='192.168.1.2'; uci commit network; /etc/init.d/network reload`（**不建议**，黑洞设备还在） | `.250` ping 0% 丢包；迁后 60s 内对 `.2` 的 ARP 询问 0 帧；外网/DNS/SSH/LuCI 均正常；180s 自动回滚看守已过截止点 |
| 路由器**内置 overlay**（`/dev/mtdblock6`）的 `etc/config/{network,wireless,system,dhcp,firewall,dropbear}` + `rc.local` + `zram-swap.sh` + hotplug 脚本 | 用当前运行中的配置镜像覆盖（原文件备份 `/root/backups/int-overlay-20261002-152402`） | 原内置配置是重置前旧货：`.2`、`br-lan` 含 `eth0`+`wan`、WiFi 开放、NTP 用域名 —— U 盘一坏回退就成残废 | 覆盖回备份目录；或改 `fstab.overlay.enabled` 切回 extroot | **真演练**：`enabled='0'` 重启 40s 起来（jffs2 22.4M、`.250`、网桥 lan1-4、WPA2、外网 0% 丢包、nft 217 行）；`enabled='1'` 重启 60s 回 extroot（ext4 29.6G） |
| 路由器 `/etc/config/dropbear` + `/etc/dropbear/authorized_keys` | `PasswordAuth`/`RootPasswordAuth` → `off`；新增 authorized_keys（`dsh-agent@newifi`，与主人 PC 上 E 盘那把同一把） | 此前**没有** authorized_keys 且 `root` 是**空密码** → 同网段任何设备都能以 root 登入（日志：`Auth succeeded with blank password`） | `/root/backups/dropbear.<时间戳>` 覆盖回 + `restart`；或把两项改回 `on` | 容器侧 `Pubkey auth succeeded ... ssh-ed25519`；NAS 空密码通道 → `Permission denied (publickey)`；加固配置已同步进回退 overlay |
| `scripts/health_all.py` | +7 项旁路由检查（SSH 密钥可达 / 地址 / 网桥 / DHCP / nft / 外网 / DNS）+ 灵敏度自检 6 例 | 这台路由器此前**不在任何监控里** | 改动是**纯增量**：删掉 `router_probe()`、`check_home_network()`、`main()` 里那行调用、`selftest()` 里 `router_cases` 段即可（未改任何既有判据逻辑） | `--selftest` 全绿；线上读数见 `health_all.py --no-alert` |
| 技能库 `immortalwrt-newifi` / `home-network-ops` / `mihomo-airport-ops` | 地址 `.2` → `.250`；新增"地址冲突""回退 overlay 演练""密钥登录"章节 | 技能里留着旧地址，下次运维会直接失联 | 技能库有月度合并护栏 + 原始文本在会话记录 | 逐处核对；三处引用的命令块都已改到可执行状态 |

- **属主/权限**：未改任何文件属主；路由器侧新增文件 `root:root 600`。
- **凭据**：无新增明文；只写入**公钥**（非私钥）；token/密码未出现在任何日志或对话里。
- **未动**：光猫（主人无后台权限）、那台天邑设备、OpenClash（保持 disabled —— 启用会改 LAN 的 DNS 行为，可能波及 NAS 的上网路径）。
- **留给主人**：① **root 密码：主人 2026-10-02 决定保持原样**（空密码 → LuCI 网页后台任何密码都能进；SSH 侧已改为只认密钥）。据此已把 `health_all.py` 里这条从 warn 降成 ok 并写明是"主人明示不管"，**不再周期性提醒**；但"空密码 + PasswordAuth=on"（远程 shell 级回退）仍保留 fail。② WiFi（`shy` / WPA2，现挂 3 台设备）要不要动。③ 广告过滤的覆盖面要不要扩（现在只有 DNS 交给这台路由器的 WiFi 设备被过滤；走网线的设备直接问光猫）。

### 2026-10-02 追加：广告过滤 + 宿主 NAS 侧无改动

| 对象 | 改动 | 回退 |
|---|---|---|
| 旁路由 | 装 `adblock-fast` + `luci-app-adblock-fast`；只启用一条列表（StevenBlack，URL 换成实测可达的 raw 源）；`enabled=1`、`dns=dnsmasq.servers`、`compressed_cache=1` | `uci set adblock-fast.config.enabled='0' && uci commit && /etc/init.d/adblock-fast stop`（或纯卸载 `opkg remove adblock-fast luci-app-adblock-fast`） |
| 旁路由 | 新增 `/etc/hotplug.d/iface/90-adblock-fast`：ifup 后补启动过滤（单飞、最多 5 次）。**原因**：`S20` 启动太早 → 下载失败即放弃、且其 wan 触发在本机永不触发 → 无缓存重启后过滤静默失效（已复现） | `rm /etc/hotplug.d/iface/90-adblock-fast`（回到"开机那次成不成看运气"） |
| 旁路由 | 防火墙新增 10 行（`:53` redirect、`:853` reject，`ubus:adblock-fast` 注释），nft 217→227 行 | 关掉 adblock 后由它的 ubus 规则自动撤 |

- 实测代价：内存 +2.9MB（48.7M→51.5M）；规则 41,588 条；拦截验证 `doubleclick.net`→NXDOMAIN、`baidu/qq` 正常。
- 主人指示「路由器自己的日志/备份存它自己那儿，重要的才进 NAS 备份」→ 本轮只在 `/opt/data/backups/router/` 放了 **10.7KB 精简配置快照**（原 29.8MB 的 `sysupgrade -b` 因为 90% 是 OpenClash 内核二进制，已移入回收站）。
- 主人问过"能不能挂载到 NAS"：结论**不改**（NAS `/vol1` 还剩 577G；SMB 方案要往路由器加服务，收益不值）；文件管理继续走容器→路由器的密钥 ssh。要改随时说。

### 同日第三阶段：优化设置（主人授权"看着办"）

| 对象 | 改动 | 理由 | 回滚 | 验证 |
|---|---|---|---|---|
| 路由器 `wireless.radio0.channel` | `1` → `13` | 扫描实测 ch1 有 **18 个 AP / 10 个强信号**，ch13 只 **3/2** —— 老小区 2.4G 挤爆 | `uci set wireless.radio0.channel='1'; uci commit wireless; wifi reload` | `iwinfo` 确认 `Channel: 13 (2.472 GHz) HT20`；原 2.4G 客户端重连，信号 **-59 → -56 dBm**；5G 未动（866Mbps 正常） |
| 路由器 `dhcp.@dnsmasq[0]` | `noresolv='1'` + `server='223.5.5.5' '119.29.29.29'` | `/etc/init.d/openclash` 会写死 `resolv.conf.auto` 为 `119.29.29.29 + 8.8.8.8`（WAN 空 → 恒触发）；8.8.8.8 给国内站点返回海外 CDN（`baidu → wshifen.com`） | `uci set dhcp.@dnsmasq[0].noresolv='0'; uci -q delete dhcp.@dnsmasq[0].server; uci commit dhcp; /etc/init.d/dnsmasq restart`（配置备份 `/root/backups/dhcp.<ts>`） | 生成的 dnsmasq conf 里 `no-resolv` + 两个 server 生效；路由器与 NAS 解析正常 |
| 路由器 两个 overlay 里的 `/upper` 垃圾树 | U 盘 `/overlay/upper/upper`（3.0M）+ `upper/work`（8K）搬出 | 上次误操作留下的死树（还害得前任以为公钥装好了） | 内容在容器回收站 `/opt/data/.local/share/Trash/files/router-junk-<日期>/`（7 天自动清） | 合并视图 `/upper` 仍列出但 0 项（空 dentry，重启消失）；overlay 读写验证通过 |
| 路由器配置备份 | `sysupgrade -b` 的 29.8MB 版本 → 废；改存精简快照 **10.7KB** 到 `/opt/data/backups/router/router-config-lite.<日期>.tar.gz` | 29.8MB 里 90% 是 `/etc/openclash/core` 的 clash 内核二进制，违反"不要啥都备份" | 肥的那份已进回收站；精简版按需重打 | `tar -tzf` 列目录，network/wireless/dhcp/dropbear/firewall 等 5 个关键配置齐全 |
| 插件（adblock-fast） | **未装，等主人一句话** | 实测列表源 2/3 可达（AdGuard hostlist 200/1.0MB、StevenBlack 200/793KB；jsDelivr 403）→ 可行；但会改全屋 DNS 行为（含 NAS 的 IPv6 DNS 路径） | — | 只做了可达性实测 |

- **纠正**：会话中我先前说过两处未验证就下的结论（8.8.8.8"被劫持"、adblock 列表"国内拉不动"），实测都推翻了，已在报告 `/opt/data/diag/l2-side.md` 第三阶段里写明更正。

## 2026-10-02 · llama.cpp 栈：主人调参固化 + 自启策略修复（干活门；主人授权「改吧」）

- **背景（隐患）**：主人当天手工 `docker run` 调参（extract `-c 16384` + 新增 `--cache-ram 1024`；
  embed `-ngl 8`），但这些容器 `restart_policy=no`、参数与内存上限都不在 compose 里
  → 宿主/docker 一重启不会自启；谁执行 `compose up -d` 就把调参打回 9/23 旧值（且 embed 会因旧 `1g` 上限被 OOM）。
- **处置**
  1. `llamacpp/docker-compose.yml` 固化终态：extract `-c 8192→16384`、新增 `--cache-ram 1024`、
     `mem_limit 7g→2583m`；embed `-ngl 99→8`、`mem_limit 1g→3527m`；加 2026-10-02 注释段（依据/实测/回滚）
     。改前原文 byte-exact 备份：`llamacpp/tmp/docker-compose.yml.bak-before-persist-20261002`
  2. `docker update --restart unless-stopped llama-extract llama-embed`（零停机，先堵自启）
  3. `docker compose up -d` 重建两容器（extract 重载约 110s）→ 现由 compose 托管（label proj=llamacpp）
- **复核（实测，非推断）**：Cmd 与主人所给一字不差；`mem=2708471808/3698327552`（=2583m/3527m）；
  `restart=unless-stopped`；`/props` n_ctx=16384 / 2048；冒烟：抽取模型回「正常」、embedding 维数 1024 ✓
- **内存/显存效果（实测）**：extract anon 0.86G、embed 0.11G、dmesg 0 次 OOM；显存 8560 / 12288，空 3349 MiB
- **附注**：手工 `docker run` 起的容器只继承镜像自带 HEALTHCHECK（探 8080 假端口）→ 会一直显示 unhealthy；
  走 compose 后探针指向 8081/8082，health 恢复正常。**别把那个红灯当成模型坏了。**
- **回滚**：换回备份文件后 `docker compose up -d --force-recreate`（或按注释段改回 8192/7g/ngl 99/1g 并删 `--cache-ram`）
