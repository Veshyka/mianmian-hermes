# hermes_onebot 运行手册（RUNBOOK）

> 「怎么部署、怎么开、怎么关、坏了怎么查」都在这一页。
> 「能不能做、为什么不会丢」见 `FEASIBILITY.md`。
> **现状（2026-09-23 07:38 起）：已启用并在跑 —— QQ 小号(<BOT_QQ>) 的 OneBot 事件已从 AstrBot 切到本适配器。**
> NapCat `websocketClients[0].url = ws://127.0.0.1:6700/ws`；聊天门 `plugins.enabled: [onebot]`、
> `platforms.onebot.enabled: true`；token 在 profile secret `/opt/data/profiles/chat/.env` 的 `ONEBOT_ACCESS_TOKEN`。
> **回退一句话**：NapCat 的 url 改回 `ws://127.0.0.1:6199/ws` + `docker restart napcat`（AstrBot 侧零改动）。
>
> ⚠️ **`extra` 里每个键的真值不再抄在这一页 —— 以 `doctor.py` 的输出为准**（它现读 `config.yaml` 现算：
> `read_only` / `group_config` 两行）。**这条规矩是被一次真事故换来的**：2026-09-23 早上这一页写着
> `group_enabled: false`，而 `config.yaml` 里早就是 `true` —— 文档与配置漂移，靠人眼比对才发现的。
> 2026-09-23 08:35 实测真值：`read_only: false`、`group_enabled: true`、`group_wake_enabled: false`
> （**B 阶段：群消息只采集、不进 agent**）、`dm_policy: open`、`allow_from: ['*']`。群聊见 §六。

路径约定（本机）：
- 源码真身：`/opt/data/chat-layer/plugin/hermes_onebot/`
- 部署副本：`/opt/data/profiles/chat/plugins/onebot/`（= 聊天门的 `$HERMES_HOME/plugins/onebot`）
- 聊天门脚本/证据：`/opt/data/chat-layer/`

---

## 一、日常命令（只读，随时可跑）

```bash
# 1. 同步源码 → 部署副本（幂等；不启用任何东西）
cd /opt/data/chat-layer/plugin/hermes_onebot && bash deploy.sh

# 2. 自检（一条命令给「正常/异常 + 哪一项」；只读、不动服务）
#    ⚠️ 必须用 Hermes 自带的 python：系统 python3 没有 yaml 模块 → 配置项会被误判成「未启用」
cd /opt/data/chat-layer/plugin/hermes_onebot && /opt/hermes/.venv/bin/python doctor.py   # 加 --json 机器可读

# 3. 单测（166 个；stdlib unittest，不用 pytest）
cd /opt/data/chat-layer/plugin/hermes_onebot && bash tests/run_tests.sh

# 3b. 群聊「只看不说」取证（不连服务、不发消息）：窗口里存了什么 + 机制证明（退出码 0）
/opt/hermes/.venv/bin/python tests/show_group_evidence.py

# 4. 全局体检（双侧门 + 插件 + 补丁 + 人设工具面）
python3 /opt/data/scripts/health_all.py        # 期望：✅ 健康总览：全部正常（16/16）
```

> `doctor.py` 的检查项：源码/副本 md5 一致、插件是否部署、两个开关、
> 心跳文件新鲜度、WS 端口有人在听、**群聊模式与两把量尺**（§六）、告警通道（hermes_report 8098）活着。

---

## 二、启用（**等主人拍板**，不自动做）

前置（缺一样都会「看起来开了但没反应」）：

1. **迁历史**：把 AstrBot `data_v4.db` 里的对话迁进 Hermes 会话，
   否则切换当日她失忆（记忆三层不用迁，共用 Hindsight bank `mianmian-history`）。
2. **协议端**：NapCat 的反向 WS 目标指向本适配器的监听地址/端口，token 与下面一致。
3. **token**：写进聊天门的 profile scoped secret（`ONEBOT_ACCESS_TOKEN`）。

然后改 `/opt/data/profiles/chat/config.yaml`，三处：

```yaml
plugins:
  enabled: [onebot]            # ① 才轮到 register(ctx) 被调用（不写=插件根本不被 import）
platforms:
  onebot:
    enabled: true              # ② 才真起反向 WS 监听
    extra:
      ws_host: 127.0.0.1       # 默认 127.0.0.1；只服务本机 NapCat 就别动
      ws_port: 6700            # 默认 6700
      self_id: "<小号QQ>"       # 多账号路由用，单账号可留空
      read_only: false         # ③ **安全默认是 true（只收不发）**，要真发必须显式改成 false
```

改完重启聊天门网关，然后：

```bash
python3 doctor.py            # 期望退出码 0
python3 /opt/data/scripts/health_all.py
grep -i onebot /opt/data/profiles/chat/logs/gateway.log | tail
```

回滚（任一步都能单独撤回到「只收不发」或「完全关掉」）：

```bash
# 只收不发（最软的一档）
#   config.yaml: extra.read_only: true
# 平台不连
#   config.yaml: platforms.onebot.enabled: false
# 连插件都不加载（回到今天的默认态）
#   config.yaml: plugins.enabled: []
# 上面任意一步后：重启聊天门网关 → 跑 doctor.py 确认
```

**影响面**：以上全部只动 Hermes 聊天门（`/opt/data/profiles/chat/`），
不碰现役 NapCat↔AstrBot 链路、不碰 `/opt/hermes` 一个字节。

---

## 三、AstrBot 侧同批改动（分段换符 + 文件工具加回）

同一批还有 AstrBot（现役）侧的两件事，回退方式在这里，细节证据在 `astrbot-patches.md`：

```bash
# 在宿主上执行；/opt/data == 宿主 /vol1/1000/<USER>
SSH='bash /opt/data/scripts/fygo_ssh.sh'
export 前缀见 astr_run.sh：SUDO_ASKPASS=/vol1/1000/<USER>

# 分段符（※ → ⁂）：改 cmd_config.json 的 regex / content_cleanup_rule
$SSH "… sudo -A docker exec astrbot python3 /AstrBot/data/_apply_seg_symbol.py --dry"   # 先看要改什么
$SSH "… sudo -A docker exec astrbot python3 /AstrBot/data/_apply_seg_symbol.py"          # 真改（自动备份）

# 人设工具面 + 边界段 + ※→⁂：写 personas 表
$SSH "… sudo -A docker exec astrbot python3 /AstrBot/data/_apply_boundary.py --dry"     # 先看要改什么
$SSH "… sudo -A docker exec astrbot python3 /AstrBot/data/_apply_boundary.py"            # 真改（自动备份）
```

两个脚本都**先备份再写**、**幂等**、写回后自己再读一次校验：

| 改了什么 | 备份落在 |
|---|---|
| `cmd_config.json`（regex / cleanup） | `/AstrBot/data/_cmd_config_backup_<ts>.json` |
| `personas.system_prompt` + `tools` | `/AstrBot/data/_persona_backup_<ts>.json` |

回退：
```bash
# 1) 分段符回到 ※（改回两个键）
sudo -A docker exec astrbot python3 - <<'PY'
import json,shutil
p="/AstrBot/data/cmd_config.json"; c=json.load(open(p,encoding="utf-8-sig"))
sr=c["platform_settings"]["segmented_reply"]
sr["regex"]=".*?[。？！~…※]+|.+$"; sr["content_cleanup_rule"]="[※]"
shutil.copy2(p,p+".rollback.bak"); json.dump(c,open(p,"w",encoding="utf-8"),ensure_ascii=False,indent=2)
print("OK")
PY
# 2) 人设/白名单回退：把 _persona_backup_<ts>.json 里的 system_prompt 与 tools 写回 personas 表
# 3) 重启：sudo -A docker restart astrbot   →  每次都要确认三件事：
#      · [hermes_lookup] 白名单稽核 行  · aiocqhttp 适配器已连接  · 无 ERROR/Traceback
```

> ⚠️ **AstrBot 的补丁与容器内改动重建后会丢**（代码在 `/AstrBot`，不在卷里）。
> 重建后照 `astrbot-patches.md` 重来一遍。

---

## 三·补：出站分段（分隔符 / 间隔 —— 规则只剩一条）

**规矩（2026-09-23 主人拍板，简化成一条）**：

    出现 `⁂` 就切；没有 `⁂` 就整段发。**没有长度阈值。**

| 项 | 值 | 在哪调 | 语义 / 备注 |
|---|---|---|---|
| 分隔符 | `⁂`(U+2042)；容错 `※`(U+203B) / `⸮`(U+2E2E) | 人格 `SOUL.md`「分段」+ `segmentation.SEP`/`SEP_TOLERATED` | 只做分隔，**任何情况下都不出现在主人收到的消息里**（见下面「硬兜底」） |
| **长度阈值** | **已废弃、已删除** | ~~`extra.segment_threshold`~~ | 见下「为什么删」；配置里这个键**已不再被读取**，别再打开。测试里有「防复活」用例（`TestNoLengthThreshold`）盯着 |
| 段间隔 | 1.5~3.5s（首条不延迟） | `extra.segment_interval` / `extra.segment_leading_delay` | 逐字复刻 AstrBot respond stage，没动 |
| 是否分段 | `extra.segment_enabled`（默认 true） | 同上 | 设 false = 整段直发（**但照样会洗掉 `⁂`**，红线不因开关放松） |

**为什么删阈值**：旧实现抄了 AstrBot 的「`len > threshold` → 不切、整条发」。结果字数一多
（尤其英文占比高，字符数很容易超）整条不切 —— **不切也就不清理** —— `⁂` 就原样留在正文里被主人看到了（他报的就是这个）。
阈值这条路本身就在制造 bug，删掉。

**连带的一条必要收窄**：切分正则里**不再包含句末标点**（旧版是 `.*?[。？！~…⁂※⸮]+|.+$`，现在是 `.*?[⁂※⸮]+|.+`）。
否则「不含 `⁂` 的 1000+ 字长文要一条不切」做不到 —— 长文里必然有句号和换行。
（尾段用 `.+` 而不是 `.+$`：`$` 配 MULTILINE 会在每个换行处收尾，等于按行切。）

**硬兜底（红线）**：`segmentation.scrub()` 保证分隔符**任何情况下**都到不了主人眼前 ——
覆盖正常切分、整段发、`segment_enabled: false`、正则写坏、模型输出怪符号；适配器在
`send()` 里洗一遍、在 `_send_one()`（最后一个出站闸口）再洗一遍。清理正则坏掉时 `scrub` 退回**逐字符过滤**。
整条只有分隔符时返回空 → 不发，**绝不退回原文**（旧版会退回，等于把 `⁂⁂⁂` 发出去）。

**人格按内容类型分（SOUL.md「分段」）**：短对话（聊天 / 回话 / 吐槽 / 问答）才在每条末尾写 `⁂`；
**长内容（清单 / 文档 / 报告 / 代码 / 步骤 / 列举）一个 `⁂` 都不写、整段发** —— 长内容整段发
现在就靠「她不写符号」，不再靠长度判断。

**改完怎么验**（都不发消息、不动服务）：

```bash
cd /opt/data/chat-layer/plugin/hermes_onebot
bash tests/run_tests.sh                                       # 全过；含「短对话切」「任意长度含 ⁂ 照切」「无 ⁂ 长文不切」「降级路径不出符号」
/opt/hermes/.venv/bin/python tests/show_segmentation_evidence.py   # 四组真实形状取证，退出码 0
/opt/hermes/.venv/bin/python doctor.py                        # 期望退出码 0
```

**生效方式**：改 `SOUL.md` → 按会话组装，**新会话生效**；改适配器代码/配置 → **重启聊天门网关**
（宿主 `docker restart hermes-chat`，约 20s 收不到消息 —— 主人正在聊时先别动，挑空档）。
心跳里那行 `segmentation = sep=⁂ interval=…` 是判断跑着的进程是不是新版的凭据；
要是还写着 `threshold=…`，说明**没重启、旧代码还在跑**。

---

## 四、排障速查

| 症状 | 先看哪 | 常见原因 / 处置 |
|---|---|---|
| 聊天门日志里搜不到 `onebot` | `plugins.enabled` 里有没有 `onebot` | 没写 → 插件不被 import，`register` 不会被调用 |
| 平台「开了」但没连 | `platforms.onebot.enabled`、token、NapCat 的 WS 目标 | 三个都要对；光有 token 不会自动开平台 |
| 连上了但收不到消息 | `doctor.py` 的心跳（`no_client_seconds` / `rx_idle_seconds`） | 协议端没连上来 / 群里没 @ 她 / `dm_policy` 挡了 |
| 发了但对方没收到 | 心跳里的 `consecutive_failures` + 日志 `read_only=True — outbound suppressed` | `read_only` 还是 true（默认值），要发得显式关 |
| 她每条消息都新开一轮 | `_dispatch()` 用的 `(gid, uid)` 是否稳定；群号表 `self.group_ids` | 会话键靠这对 id 现算，任何消息级随机量都会毁掉连续性 |
| 回复没切成多条 | 平台提示词 `⁂` 有没有进 system prompt | **只有 `⁂`/`※`/`⸮` 会切**（句末标点、换行都不切了）；没有分隔符就整段发 —— 长内容靠她「不写符号」整段发 |
| 长内容里冒 `⁂` | 人格「分段」是不是按内容类型分的；跑着的进程是不是新版（心跳 `segmentation=`） | 旧版是「超阈值就不切 → 也就不清理 → 符号露在正文里」；新版 `send()`/`_send_one()` 两道 `scrub()` 兜底 |
| 全局体检红 | `python3 /opt/data/scripts/health_all.py` | 它会点名哪一项（插件在位/防抖 10.0s/工具面/补丁） |
| 群里说话了她不回 | `doctor.py` 的 `group_mode` 行 / 心跳 `group_wake_enabled` | **B 阶段就是这样（只看不说）**，不是故障；要她回话看 §六「C 阶段」 |
| 心跳里没有 `group_*` 字段 | 聊天门网关的启动时间（`started_ts`） | 进程还是**旧代码**（B 阶段上线时没重启网关）→ 见 §六「待办」 |

日志纪律：状态心跳里**只有计数、时间戳与错误摘要，绝不含消息正文与凭据**（`health.py` 头注）。

---

## 五、这批改了什么（对照）

| 侧 | 文件 | 改了什么 |
|---|---|---|
| Hermes 聊天门 | `profiles/chat/SOUL.md` | 工具面加「文件四件套」+ 新增《碰文件的规矩》（**不加路径保险，靠提示词写死**）+ 派活理由写明「干活门那边能被打断/改方向，她不能」 |
| Hermes 聊天门 | `profiles/chat/config.yaml` | `platform_toolsets.api_server` 含 `file`（读/写/改/搜四件）——**无任何路径 allowlist** |
| Hermes 聊天门 | `profiles/chat/plugins/onebot/` | OneBot 适配器部署副本（同步自源码） |
| AstrBot（现役） | `data/cmd_config.json` | 分段正则 `.*?[。？！~…⁂※⸮]+|.+$`、清理 `[⁂※⸮]` |
| AstrBot（现役） | `personas.mianmian` | 边界段换成新版、`※`→`⁂` 6 处、白名单 19 → **22** 项（加回文件四件套） |
| AstrBot（现役） | `plugins/hermes_lookup/main.py` | 每轮按真实工具面**逐句校正**那段英文提示（删 shell/python 句、留 workspace/文件句） |

**2026-09-23 第二批：分段规矩重写（阈值删掉，只剩「有 `⁂` 就切」）**

| 侧 | 文件 | 改了什么 |
|---|---|---|
| Hermes 聊天门 | `profiles/chat/SOUL.md` | 「分段」重写：短对话用 `⁂`、**长内容（清单/文档/报告/代码/步骤/列举）一个 `⁂` 都不写、整段发**；写明「只有 `⁂` 会切，句末标点/换行都不再切」+「分隔符主人看不到，别为格式担心」。删掉旧的「整条别超 150 字」。备份 `SOUL.md.bak-before-segtype-20260923-075756`（md5 前 `0f8339a6…`） |
| Hermes 聊天门 | `profiles/chat/config.yaml` | `segment_threshold` **已删除**（键不再被读取，别再打开）；`segment_enabled` / `segment_interval` 保留 |
| 适配器源码+副本 | `segmentation.py` / `adapter.py` | **删掉长度阈值**（`DEFAULT_THRESHOLD`/`THRESHOLD_BAND`/`parse_threshold` 全部移除）；切分正则收窄成 `.*?[⁂※⸮]+\|.+`（去掉句末标点与 `$`，否则「无 `⁂` 长文一条不切」做不到）；新增硬兜底 `scrub()`，`send()` 与 `_send_one()` 两道都洗；整条只有分隔符 → 返回空、不发（旧版会退回原文把 `⁂⁂⁂` 发出去） |
| 适配器测试 | `tests/check_segmentation.py`、`tests/check_adapter_e2e.py`、`tests/show_segmentation_evidence.py`（新） | 按主人给的四条验收重写：① 短对话含 `⁂` → 切、无符号；② 任意长度含 `⁂`（英文占比高的 1000+ 字）→ 照切、无符号；③ 无 `⁂` 的 1000+ 字 → 一条不切；④ 异常/降级（`segment_enabled: false`、正则写坏、只有分隔符）→ 也不出符号。另加「防复活」用例 `TestNoLengthThreshold`（阈值符号或 `threshold` 参数一回来就红） |

复核基线（做完自跑过）：
`hermes_onebot` 单测 **135 OK**；
`show_segmentation_evidence.py` → **7/7 PASS**（3 句短对话 → 3 条；1355 字英文重 → 6 条；1124 字无 `⁂` → 1 条；`scrub` 兜底干净）；
`doctor.py` → 退出码 **0**（源码/副本 md5 一致、端口在听、协议端已连）；
`health_all.py` → **29/29 全绿**（在上一批的基线上）；
`_selftest_segmentation.py`（走 ResultDecorate 真身）**10/10**；
`_selftest_boundary.py`（走 enforce_boundary 真身）**24/24**。

> ⚠️ 上面最后两项（`_selftest_*`）是 **AstrBot 侧**的基线，本轮没动 AstrBot，未重跑。
> ⚠️ 适配器的 800 阈值要**重启聊天门网关**才在运行进程里生效（见「三·补」）。
> 📌 上面写的 `health_all.py 29/29` 是**当天的条数**；AstrBot 退役后这个脚本的检查项变成 **16 项**，
> 现在的基线是 **16/16**（别拿 29 去对）。

---

## 六、群聊（B 阶段：只看不说｜2026-09-23 上线）

**目标形态（主人定的三层，原话见 `../RESEARCH-group-chat-two-benchmarks-mapping.md` §0/§8）**：
群聊要**有连贯感**（同群上下文）但**不能污染主记忆库**。
① 连贯层＝每群一份滚动窗口（只活在群上下文里）；② 长期记忆层＝**默认零入库**，只有她自己主动写才写；
③ **B 阶段群消息不唤醒 LLM**（成本 0），只采集。

### 6.1 做了什么

| 文件 | 改动 |
|---|---|
| `group_window.py`（新） | 每群一份滚动窗口：JSONL 落盘、**条数 + 总字节双上限**、超了丢最旧、坏行容忍、写失败只记账不抛。**不 import 任何记忆模块、无任何写库调用** |
| `adapter.py` | 群消息在 `_ingest` 里**采集后直接 return**（不进防抖、不进 agent）；新增 `group_wake_enabled` 闸与 `_dispatch` 里的**纵深防御**（绕过 `_ingest` 也拦得住）；新增两把量尺 `group_rx_count` / `group_llm_calls` + 窗口摘要进心跳 |
| `onebot_proto.py` | 新增 `extract_window_text()`（比投递口径多保留 `[表情]`，所以**纯表情消息也能进窗口**）；`should_ignore(..., media_counts_as_text=)` 只在群事件上传 True，**私聊口径一个字没改** |
| `health.py` | 心跳判读新增：`group_wake_enabled=false` 却出现群回合 → **FAIL**；窗口落盘失败 → WARN；否则报「已收 N 条 / 0 次 LLM 调用」 |
| `doctor.py` | `group_config` 行（**打印 `group_enabled` / `group_wake_enabled` / `dm_policy` 的真值** —— 治文档漂移）＋ `group_window_dir` / `group_mode` 两行 |
| `tests/` | `check_group_window.py`（新，31 例）、`check_adapter_e2e.py` 新增 `TestGroupCollectOnly`（8 例）、`show_group_evidence.py`（新，取证） |

### 6.2 旋钮（都在 `profiles/chat/config.yaml` 的 `platforms.onebot.extra`）

| 键 | 默认 | 含义 |
|---|---|---|
| `group_enabled` | false（**本机现为 true**） | 群消息是否入站。关 = 在准入处丢掉（连窗口都不进） |
| `group_collect_enabled` | **true** | 是否收进滚动窗口（「要看得到前面聊了啥」） |
| `group_wake_enabled` | **false** | ★ **B 阶段红线**：false = 群消息永不进 agent（0 次 LLM 调用）。**改成 true 就是 C 阶段了**（每条命中都烧一轮，见 6.6） |
| `group_window_dir` | `<profile>/onebot-groups` | 窗口落盘目录（在 `/opt/data` 持久卷里，重建容器不丢） |
| `group_window_max_msgs` | 200 | 每群保留条数上限 |
| `group_window_max_bytes` | 262144（256 KiB） | 每群窗口总字节上限 |

**路径与内容**：`/opt/data/profiles/chat/onebot-groups/<群号>.jsonl`，一行一条：
`{"ts": 秒, "t": "MM-DD HH:MM:SS", "uid": "QQ号", "name": "群名片/昵称", "text": "正文（含 [图片]/[表情] 占位）"}`。
**只有这些**：不落原始事件、不落图片 URL、不落凭据。上限按「条数 OR 字节」任一超了就从**最旧**那头丢。

### 6.3 心跳里怎么看（`profiles/chat/onebot-state.json`）

| 字段 | 例子 | 含义 |
|---|---|---|
| `group_mode` | `collect-only(0 LLM)` | 群聊当前形态（off / collect-only / collect+wake(付费回合)） |
| `group_rx_count` | `37` | 已收进窗口的群消息条数（**证明采集在动**） |
| `group_llm_calls` | `0` | 因群消息起过 agent 回合的次数（**B 阶段必须恒为 0**） |
| `group_window` | `3 群/128 条/41.2KB（上限 200 条/256.0KB，未入主库；淘汰 0，错 0）` | 窗口规模与淘汰/失败计数 |
| `group_window_errors` | `0` | 窗口落盘失败次数（>0 → health 报 WARN） |

### 6.4 为什么「0 次 LLM 调用、0 次入库」是**机制**而不是自觉

1. **0 次 LLM**：群消息在 `_ingest()` 里 `_collect_group()` 之后**直接 return** ——
   防抖、`_dispatch()`、`handle_message()` 整段（= 付费回合的入口）根本走不到；
   `_dispatch()` 里还有第二道闸（`is_group and not group_wake_enabled → 拦`）。
   **可验证**：心跳 `group_llm_calls` 恒 0；单测 `TestGroupCollectOnly` 用真 WS 路径断言
   `handle_message` **一次都没被调用**。
2. **0 次入库**：Hindsight 的 `auto_retain` 挂在 **agent 回合**上；群消息不进回合 → 结构上不可能 retain。
   另外窗口模块/采集函数里**没有任何记忆原语**（`hindsight` / `retain` / `recall` / `memory`），
   这条由 AST 级守卫盯着：`tests/check_group_window.py::TestNoMemorySink`、
   `tests/show_group_evidence.py` 第 ⑤ 段。
3. **要她主动写才写**（第 2 层）：只有她自己调 memory 工具才会落库 —— 那是 C 阶段的事，见 6.6。

### 6.5 验证命令（都不发消息、不动服务）

```bash
cd /opt/data/chat-layer/plugin/hermes_onebot
bash tests/run_tests.sh                                    # 166 OK（含群窗口/上限/不进 agent 的用例）
/opt/hermes/.venv/bin/python tests/show_group_evidence.py  # 退出码 0：窗口存了什么 + 机制证明
/opt/hermes/.venv/bin/python doctor.py                     # 退出码 0；看 group_config / group_mode 两行
python3 /opt/data/scripts/health_all.py                    # 16/16
```

**B 阶段复核基线（2026-09-23 08:35 实测）**：单测 **166 OK**（原 135 + 31）；
`show_group_evidence.py` → **PASS**；`doctor.py` → 退出码 **0**（`group_config: group_enabled=True /
group_wake_enabled=False`）；`health_all.py` → **16/16 全绿**。

### 6.6 C 阶段（@ 才进 LLM）要改哪里 —— 一步一句

> **顺序不能反**：先把「记忆隔离」定下来，再打开唤醒。否则群回合会按现有 `auto_retain: true`
> 落进 `mianmian-history`（与主人私聊**同一个 bank**），那就是主人明说不要的「污染」。

1. **@ 判定**（适配器，`onebot_proto.py`）：`at` 段已渲染成 `[@<QQ>]`（`onebot_proto.py:52-54`），
   且适配器有 `self.self_id` → 判 `f"[@{self.self_id}]" in text` 或群名片点名即可，**不需要新 API**。
2. **注入窗口**（适配器 → 提示词）：`GroupWindow.render_context(gid)` 已经产好带
   `source=qq-group` 标签的材料，把它拼在被 @ 的那条消息前面一起投递即可。
3. **开闸**：`group_wake_enabled: true`。**外加限流**（每群每分钟/每小时上限、冷却），
   照 qq-bridge 的 `maxWakePerMinute 1` / `maxWakePerHour 12` 抄（否则群里刷屏 = 连续付费回合，
   实测一轮 3~11 次 API 调用）。
4. **记忆隔离（必须先定）**，三条路（代价从小到大，都已核过源码）：
   - **① `retain_source`**：Hindsight 支持 `HINDSIGHT_RETAIN_SOURCE` / 配置 `retain_source`
     （`plugins/memory/hindsight/settings.py:20-21,62`）→ 给落库的条目打 `metadata.source`。
     **但它也是 profile 级**（聊天门私聊/群共用）→ 只能标「来自本门」，分不出群/私聊。
   - **② `bank_id_template`**：Hindsight 支持模板化 bank（占位符 `{profile}/{workspace}/{platform}/{user}/{session}`，
     `plugins/memory/hindsight/__init__.py:429-430`）→ 可另开群库。但模板在**初始化时**渲染，
     同一进程内分不开「这条是群的」「那条是私聊的」→ 只能整个聊天门换库。
   - **③ 工具写 + 提示词**（最稳、最贴主人原话）：群回合**不自动入库**（关掉该会话的 auto_retain
     或不让群回合进 retain 路径），要留的由**她自己调 memory 工具**写，并在材料里已带
     `source=qq-group` 字样。**注意**：`memory_tool` 的签名里**没有** `source`/`tags` 参数
     （只有 `action/target/content/old_text/new_text/operations`，已核 `tools/memory_tool.py:172-174`）
     → 「带上 source=qq-group 标签」目前只能靠**材料自带标签 + 提示词要求她转述来源**，
     真要机器可查的标签得走 ① 或改核心。
5. **出站**（要她在群里说话才有）：`send()` 已能发 `send_group_msg`（群号在 `self.group_ids` 里就会走群动作）；
   引用回复 / 发表情包是 `build_action` 的事（现在只发 text）。

### 6.7 两条备注

- **群号白名单**：现在**没有**（主人明确说群是他自己开的，不要加限制；适配器对任何群都放行）。
  **若日后他人能把 bot 拉进别的群，加一条群号白名单即可**：`_authorized_sender()` 的群分支加
  `gid in self.group_ids` 判定（`group_ids` 字段已在，`adapter.py` 里只用于出站判群/私）。
- **待办（B 阶段唯一没做完的一步）**：本批改动**要重启聊天门网关才在运行进程里生效**
  （宿主 `docker restart hermes-chat`，约 20s 收不到消息）。重启前先看有没有 onebot 入站（主人在聊天时别动），
  重启后 `doctor.py` 应出现 `group_*` 字段与 `group_mode=collect-only(0 LLM)`。
  **重启后请主人发一条群消息**做终验：`group_rx_count` +1、`group_llm_calls` 仍 0、窗口文件里出现那一条。

---

## 七、群回合记忆隔离闸（C1「@ 必答」的硬前置｜2026-09-23）

### 7.1 为什么必须先装它（有取证，不是推测）

B 阶段的「群消息零入库」是**免费**的 —— 群消息压根不到 agent，没有回合就没有 retain。
C1 一开唤醒，被 @ 的那一轮就是**一次正常回合**，而 auto_retain 是 **profile 级**的。

取证实验（`tests/evidence_group_retain.py`，不改配置、不发消息、不写库：把 provider 的
`_retain_batch` 换成捕获器，模拟群/私聊各一个完成的回合）：

| provider | 群会话回合 | 私聊回合 | 结论 |
|---|---|---|---|
| 原版 `hindsight` | **retain×1 → bank `mianmian-history`** | retain×1 | ⚠️ 洞是真的：群回合会写**主库** |
| `hindsight_guard` | retain×0 | retain×1 → 主库 | 群被堵住、私聊不受影响 |

结论：**不装闸就不许开群唤醒**。适配器把这条写成了代码级硬前置（`group_memory_guard_required`）。

### 7.2 闸门是什么（透明包装，不是新记忆库）

`/opt/data/profiles/chat/plugins/hindsight_guard/`（源码真身就这一个目录，`plugin.yaml` 同名）：

- 继承官方 `HindsightMemoryProvider`，**只有 4 个方法被改**：`initialize` / `on_turn_start` /
  `prefetch`(含 `queue_prefetch`) / `sync_turn` / `handle_tool_call`；
- 判据（任一命中即隔离）：**① `chat_type ∈ {group,channel,supergroup,guild}`（主信号，来自网关）**
  **② 正文含 `source=qq-group`（兜底，适配器每个群回合都写这行）**；
- 被隔离的那一轮：**不写主库、不召回、不放行 `hindsight_retain/recall/reflect` 工具**；
- 私聊：**逐字透传**给原版实现（DM 的记忆行为一个字没变）；
- 配置：`plugins/hindsight_guard/config.json`（`blocked_chat_types` / `marker` / `enabled`）；
- 证据：`<HERMES_HOME>/hindsight_guard/state.json` —— **只有计数**
  （`retain_skipped` / `recall_skipped` / `tools_blocked` / `last_skip`），**不含任何消息正文**。

### 7.3 装 / 验 / 回退

```bash
# 装（一行切 provider；闸门插件已随本批落地）
#   config.yaml:  memory.provider: hindsight_guard
# 验 1：provider 真被解析（官方 doctor 打的是配置名）
hermes -p chat doctor | grep -A2 "Memory Provider"     # 期望 ✓ hindsight_guard provider active
# 验 2：取证实验（群 skip / 私聊 write）
cd /opt/data/chat-layer/plugin/hermes_onebot
HERMES_HOME=/opt/data/profiles/chat /opt/hermes/.venv/bin/python \
  tests/evidence_group_retain.py --provider hindsight_guard --expect "group:skip dm:write"
# 验 3：单测里那条功能级守门用例
bash tests/run_tests.sh check_group_memory_guard      # 群 0 写 / 私聊 1 写
# 回退（**注意连带效应**：provider 回原版 → 群唤醒会被适配器拒绝，这是 fail-closed，不是 bug）
#   config.yaml:  memory.provider: hindsight   →  重启聊天门网关
```

### 7.4 fail-closed 的三道闸（顺序即成本控制，任一不过 0 token）

1. `group_wake_mode`（三态）—— `collect-only` 直接结束；
2. **记忆隔离硬前置** —— 闸门未就位就**拒绝唤醒**并计数（`group_wake_blocked`）+ ERROR 日志；
3. 命中判定 + 每分钟/每小时上限 —— 被限流**必须留痕**（`group_wake_limited` + WARN），不静默丢。

> 排障档 `group_memory_guard_required: false` **只许在还没装闸时临时用**，装好就该是 `true`。

### 7.5 生效时刻（C1 上线的那一步，尚未做）

闸门的 `config.yaml` 改动**已经落盘**（`memory.provider: hindsight_guard`），
但**运行中的聊天门进程**只有创建新 agent 时才读它 —— 群里真的会说话，还需要**一次网关重启**
（运行进程里跑的还是 B 阶段的适配器模块，日志里能自证：`group=collect-only(0 LLM)`）。

**重启前的纪律（硬约束）**：先看最近 onebot 入站，主人在私聊时**别动**（2026-09-23 09:17~09:22
主人连续在私聊测试，本条因此卡住未做）。重启后确认三件事：

1. 监听行变成 `group=mention-wake(付费回合，仅@/回复/点名)`；
2. `onebot-state.json` 里 `group_memory_isolated=true`；
3. `doctor.py` 的 `group_memory_guard` 项 = provider `hindsight_guard` + 闸门插件在位。

然后请主人在群里 @ 一句做终验（`group_mention_count`/`group_llm_calls` 各 +1；随后不带 @ 聊两句，两个数不动）。
