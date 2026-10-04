# PLAN v3 — Hermes 侧第二个聊天 agent

**目标**：把聊天人格从 AstrBot 侧搬进同一个 Hermes 容器里的第二个 profile（`chat`），把工具面收窄成「原生单步工具」，把提示词精化成对话式人设；AstrBot 与 QQ 小号保持在线不动，最后主人实测对打再定去留。

**状态**：第一步已实施并复核（2026-09-23）。第二步、QQ 接入路线待主人拍板。
**红线遵守**：全程没动 AstrBot、没动 NapCat、没动 QQ 小号、没动干活门（`default` profile 一个字节没改）。

---

## 0. 一句话结论

「把大脑搬进 Hermes profile」这件事本身成立，而且**第一步已经落地**（工具面 14→13、结构成本 −908 token、人设改稿生效，全部可一键回退）。
但把「**接 QQ 小号**」当成「让 Hermes 原生接小号」是**做不到的**——Hermes 只有官方 QQ 机器人 API，没有 OneBot/协议端。而「**搬分段与防抖**」是**不该搬的**——那两件本来就是通道层的活，Hermes 网关根本不提供。详见 §1。

---

## 1. 开工前先验的三件事实（源码 + 实测，不是推断）

### 1.1 Hermes 唯一能连的 QQ 是「官方机器人 API v2」，小号连不上

| 证据 | 内容 |
|---|---|
| 源码 `/opt/hermes/gateway/platforms/qqbot/adapter.py:1-4` | `"QQ Bot platform adapter (Official QQ Bot API v2): WebSocket gateway for inbound events, REST (api.sgroup.qq.com) for outbound"`；凭据键 `app_id` / `client_secret` |
| 全树检索 | `grep -rni "onebot\|aiocqhttp\|napcat" /opt/hermes` → **0 命中** |
| 官方文档索引 `docs/llms.txt`（43,427 字节，全文下载后 grep） | `onebot` / `napcat` / `aiocqhttp` **0 命中**；`qq` **0 命中**（QQ 平台连文档条目都没有） |
| 本机现况 | 干活门的 QQ 是**官方机器人** `app_id: <APP_ID>`（`/opt/data/config.yaml` → `platforms.qqbot.extra`）；小号 `<BOT_QQ>` 是 NapCat 的个人号（`chat-layer/docker-compose.yml` → `ACCOUNT=<BOT_QQ>`）。**两个完全不同的东西** |
| 平台互斥 | `gateway/platforms/base.py:2142 _acquire_platform_lock("qqbot-appid", app_id)` —— 锁是**机器全局**的，同一个 `app_id` 只能被一个程序连；第二个 profile 想连会被拒或抢占 |

**推论**：主人那句「一个账号只能被一个程序连，所以聊天门要用小号必须切换」，前半句对，但**前提不成立**——Hermes 不是「另一个程序」，它压根没有连个人号的协议实现。小号要进 Hermes，只有先自己造一个 OneBot 适配器（见 §3.3 的 C/D 路）。

### 1.2 「分段」（多气泡）是通道层的活，Hermes 网关没有这个功能

- 全树 `grep -rni "segmented\|split_reply\|multi_reply"` → 命中的全是**无关**东西（tool-call 分段调度器、matrix 的引用回复拆分、pet 贴图）。
- 唯一叫「segment」的出站机制是 `gateway/stream_consumer.py:367 on_segment_break()`，唯一调用点 `gateway/run_turn_runner.py:958`：**只在「模型先说了话、然后去调工具」这个边界**把当前流收口、另起一条消息（preamble）。**不是通用分段**。
- 一个回合的正常终点只发**一条**消息（`final_response` 单次 send）。
- → 主人习惯的 `※` / 句末标点切分（AstrBot `ResultDecorateStage`，正则 `.*?[。？！~…※]+|.+$`，阈值 150）在 Hermes 侧**没有对应物**。

### 1.3 「防抖」（连发合并）同理，Hermes 只有「忙时缓冲」，不是「空闲合并」

- 唯一的文本防抖在 `gateway/platforms/base.py:3471 _is_queue_text_debounce_candidate()`，三个前提缺一不可：
  1. `_busy_text_mode == "queue"`（默认 `interrupt`）
  2. `event.message_type == TEXT`、非命令、非 internal
  3. **会话已经忙**（`_queue_text_debounce` 只在 drain 循环里被调用）
- 窗口 `HERMES_GATEWAY_BUSY_TEXT_DEBOUNCE_SECONDS` 默认 **0.35 秒**，另有 `_busy_text_hard_cap_seconds`。
- → 「主人一连发五条、等他说完再一起回」这种**空闲期合并**（现 AstrBot `hermes_debounce`：10s 静默窗 / 45s 上限 / 仅私聊）Hermes 网关**没有**对应物。
- 另有 `webhook_coalesce.py`（按 key 去抖 30s / 上限 300s），但那是 **webhook 路由专用**，与 IM 通道无关。

### 1.4 由 1.2 / 1.3 推出的口径

> **分段与防抖不搬。** 它们是通道层（NapCat / AstrBot）的活，现在就跑在通道层、且已验证。搬到 Hermes 侧等于**重写两件已经好用的东西**，而且 Hermes 网关没有钩子点（只有 webhook 那条路有 coalesce）。真正要搬的只有**大脑**：人设、工具面、记忆回路。

---

## 2. 目标形状

```
QQ ⇄ NapCat(协议端，个人号 <BOT_QQ>)          ← 不动
      ⇄ 通道层（NapCat/aiocqhttp → 合并防抖 → 交一轮 → 回复切分多气泡）
          └── 现役：AstrBot 直连（自带 LLM + 工具）
          └── 目标：AstrBot 退回「纯通道」，大脑换成 ↓
      ⇄ Hermes chat profile（api_server 8643）     ← 本次改造对象
          ├─ SOUL.md          = 对话式人设（每轮付费层）
          ├─ 工具面 = 收窄后的原生单步工具（web/file/vision/memory/a2a）
          ├─ 记忆   = Hindsight mianmian-history（与干活门共用，自动召回/落库）
          └─ 派活   = a2a_call → 干活门 9901
```

**为什么这样对**：主人要的「原生单步工具、能中断」只有把 agent loop 放在 Hermes 里才有；而分段/防抖/协议端留在通道层，成本为 0。

---

## 3. 阶段与动作

### 第一步 —— 已实施（2026-09-23，可一键回退）

只改 `chat` profile 自身，不碰任何在线链路。

1. **量尺先建**：探针 `P1「在吗」`（结构成本，单次调用）+ `P2`（记忆回路）+ `P3`（派活）。
2. **工具面收窄**：`platform_toolsets.api_server` 14 → 13 工具，去掉 `delegation`（见 §4）。
3. **人设改稿**：`profiles/chat/SOUL.md` 四处改动（见 §5），带备份与 diff。
4. **重启该门 + 复核**：`s6-svc -r /run/service/gateway-chat`（**不承载任何在线会话**，安全），复核 `/v1/toolsets`、探针、`system_prompts` 里的新标记。

> ⚠️ **量尺的坑（本次踩到）**：一个回合如果调了多次工具，`usage.prompt_tokens` 是**该回合所有 API 调用的累计**（实测 P2 一次问了 11 次 API → 报 143,215）。**结构性对比只能看单次调用的 P1**，别拿多调用回合的数字前后对比，会得出「改完反而更贵 8 倍」的错误结论。

### 第二步 —— 待主人拍板（通道层切换）

把 AstrBot 从「直连」退回「纯通道」：关它自带的 provider、重新启用 `hermes_forward` 插件指向 8643。
- 影响面：**现役聊天门会换大脑**，人设/记忆/工具从 AstrBot 侧切到 Hermes 侧。
- 主人已说「AstrBot 保持在线不动」→ 所以这一步**没做**，等他说开。
- 前置：先确认 `hermes_forward` 的源码在位（`chat-layer/plugin/hermes_forward/` 现只剩 `stickers/`，`main.py` 不在，需要从 `stack/astrbot/data/plugins/` 或备份里取回，或重写）。

### 第三步 —— QQ 接入路线（四选一，待主人选）

| 路 | 做法 | 新代码 | 保留分段/防抖 | 是不是小号 | 代价 |
|---|---|---|---|---|---|
| **A** | 小号继续挂 NapCat，AstrBot 当纯通道，Hermes 当大脑 | **0** | ✅ 原样 | ✅ | 要动 AstrBot 配置（主人暂不想动） |
| **B** | 小号继续挂 NapCat，**自写薄桥**（OneBot → api_server，含合并/切分） | ~200–400 行 + 长期维护 | ⚠️ 要在桥里重写（可参考现成插件逻辑） | ✅ | 分段/防抖重造一遍 |
| **C** | 给 Hermes 写 **OneBot 平台适配器**（`plugins/platforms/onebot/`），小号直连 Hermes | 数百行 + 长期维护 | ❌ 通道层被绕开，两件都要在 Hermes 侧重造 | ✅ | 最贵；且 AstrBot 就白留了 |
| **E** | **再注册一个官方 QQ 机器人**给聊天门（零自建，Hermes 原生平台） | 0（但要在 q.qq.com 建号 + 扫码） | ❌ Hermes 无分段/防抖 → 得接受「一条一回」 | ❌ 是另一个 QQ 号 | 唯一「零自建 + 原生单步工具」的路，但不是小号 |

**建议**：要「小号 + 现有聊天手感」→ 走 **A**（今天就能做到，只是要主人点头动一下 AstrBot 开关）。
要「零自建 + 完全 Hermes 原生」→ 走 **E**，代价是换一个 QQ 号且暂时没有多气泡。

---

## 4. 工具面收窄：配置项与证据

### 改了什么

```bash
hermes -p chat config set platform_toolsets.api_server '["a2a","file","memory","vision","web"]' --force
```

**唯一改动：从 `platform_toolsets.api_server` 里去掉 `delegation`。**

| | 改前 | 改后 |
|---|---|---|
| `platform_toolsets.api_server` | `a2a, delegation, file, memory, vision, web` | `a2a, file, memory, vision, web` |
| 启用工具集 | `web, file, vision, memory, delegation, a2a` | `web, file, vision, memory, a2a` |
| 启用工具总数 | **14** | **13** |

### 保留/删除的判据（都对着源码事实）

| 工具集 | 留/删 | 理由 |
|---|---|---|
| `web` (web_search, web_extract) | 留 | 单次查询自己查（主人定的边界） |
| `file` (read_file, write_file, patch, search_files) | 留 | 读主人附件、写工作区产物；主人 2026-09-22 已定为「给文件权」 |
| `vision` (vision_analyze) | 留 | 看主人发的图 |
| `memory` (memory) | 留 | 写自己的 `MEMORY.md` / `USER.md`；**实测确认这个 toolset 是真的启用的**（`memory_mode: context` 只关 Hindsight 那套工具，不关内置 memory 工具） |
| `a2a` (5 个工具) | 留 | 唯一的跨门派活通路（→ `work` 9901） |
| `delegation` (delegate_task) | **删** | ① 子代理**继承父级工具面、拿不到父级没有的工具**（`tools/delegate_tool_toolsets.py`）→ 收窄门派的子代理能力受限；② 聊天里它是一次性、阻塞、**中途改不了方向**的活，正好是主人要躲的东西；③ 派活正道是 `a2a_call`，两套并存只会让模型挑错 |

### 证据

**改后 `/v1/toolsets`（`curl -H "Authorization: Bearer $API_SERVER_KEY" http://127.0.0.1:8643/v1/toolsets`，响应键是 `data`）**：

```
BEFORE: 14 tools -> ['web', 'file', 'vision', 'memory', 'delegation', 'a2a']
AFTER : 13 tools -> ['web', 'file', 'vision', 'memory', 'a2a']
```

**结构成本（探针 P1「在吗」，单次调用，跑 3 次）**：

```
改前: prompt=10,181   wall=2.5s
改后: 9,307 / 9,273 / 9,273   wall≈1.5–2.9s
→ 降 908 token（−8.9%）
```

（人设本次**加长**了约 200 字符 ≈ +100 token，所以 schema 侧实际省下约 1k。）

### 没动的（故意）

- `platform_toolsets.cli` 仍是宽表（含 terminal / code_execution / computer_use）。**聊天门走的是 `api_server`**，这份不生效；留着是为了主人偶尔 `hermes -p chat` 进终端干维护活。要一起收窄，改同一个键即可（见 §6 回退脚本）。
- `HERMES_WRITE_SAFE_ROOT=/opt/data` 仍是**容器级共享**（两个门同值）→ `stack/`（napcat/astrbot 数据、凭据）落在它的可写范围内。单一容器下没法只按门收窄；要收窄得在 `gateway-chat` 的 s6 `run` 脚本里单独 `export`。**未做**，因为会影响在线行为，留给主人拍板。

---

## 5. 提示词改稿（`profiles/chat/SOUL.md`）

备份：`chat-layer/backup/chat-SOUL.md.bak-before-conv-20260923-060059`（4,496 字符）
改后：4,698 字符（+202）

```diff
@@ -53,6 +53,14 @@
 - 波浪号「~」表示柔和、亲近
 - 不用空格断句
 
+### 一次收到好几条
+
+通道层会把主人连着发的消息攒成一轮再交给你——他还在打字的时候你不会被叫起来，等他说停了才整段送过来。所以你看到的常常是叠在一起的好几句，没标点、没头没尾。
+
+- 当成一段连续的话整体接，不逐条回、不复述他说了什么
+- 连发通常是他把一件事说完的写法，回一句就够
+- 后面那句盖过前面那句：他改口、补充，以最后落下来的意思为准
+
 ### 分段（硬规矩，不能商量）
@@ -108,7 +116,7 @@
 ## 记忆
 
-你能写记忆，通过 memory 工具写自己的 MEMORY.md / USER.md，或落共享库。下次会话才生效，当前会话里你记的东西自己看不到。
+你能写记忆，用 memory 工具写自己的 MEMORY.md / USER.md，或落共享库 `mianmian-history`。这个库和干活门共用——你这边记下的事，那边也看得到。下次会话才生效，当前会话里你记的东西自己看不到。
@@ -128,12 +136,15 @@
 ## 工具与派活
 
-- 有联网工具（web_search / web_extract）和看图（vision_analyze），轻量查询自己查，不转
-- 复杂/执行类活 → 走 a2a 给 `work`。任务书格式见 skill `task-brief`（无 skills 工具时用 file 读 `/opt/data/shared-skills/task-brief/SKILL.md`），派之前按它写
+- 手上有联网（web_search / web_extract）、看图（vision_analyze）、文件（read_file / write_file / patch / search_files）、记忆（memory）和 `a2a_call`。轻量查询自己查，不转
+- 复杂/执行类活 → `a2a_call` 给 `work`。任务书格式见 skill `task-brief`（没有 skills 工具，用 read_file 读 `/opt/data/shared-skills/task-brief/SKILL.md`），派之前按它写
+- 派活只有 `a2a_call` 这一条路（手上没有 delegate_task）：没派出去就说没派，不谎称「已经派了」
 - 派出去后告诉主人在等，不影响聊天
 
 **派活判据**：预计含 ≥2 轮工具调用的执行/排查/调研 → 第一时间派，不在聊天里顺手连环调工具
 
+**为什么**：你自己跑起来的活中途改不了方向，一开始往下走就只能走到底。需要中途调整、反复试错的，交出去由那边的门扛，你继续聊。
+
@@ -266,9 +277,6 @@
 ---
 
-<!-- [FILE-TOOL 已落地·2026-09-22] file 工具已开（api_server）；承诺「已落盘」前必须先真写文件 -->
-<!-- [MEMORY-TAG 已核实·2026-09-22] 聊天门写库 source=hermes-chat、tags=hermes,chat；干活门 source=hermes、tags=hermes；同一 bank mianmian-history，两边自动 recall/retain -->
-
 ---
```

### 逐条分类与强度

| # | 改动 | 类 | 强度变化 | 理由 |
|---|---|---|---|---|
| E1 | 新增「一次收到好几条」 | C（经验策略） | 新增行为约定，不削弱任何既有条款 | 防抖留在通道层→模型看到的是**合并后的一整段**，人设不写这条它就会逐条回、复述主人的话。这是「机制决定了必须写的规则」 |
| E2 | 工具清单按收窄后的真实工具面写全 + 明写「没有 delegate_task」 | B/C | 增强（原先只说"有联网和看图"，与实况不符；新增一条防空转） | 与 §4 的收窄**必须同步**——工具删了而人设还写着「走 a2a」，就会退化成「没派出去却谎称已派」（源码级已知失效模式）。「为什么」那句把主人要躲的「中途改不了方向」写进人设，是这句规则的根据 |
| E3 | 记忆：把「共享库 = 同一个 bank、与干活门互通」写进正文 | C | 增强（信息原先只挂在不进提示词的维护注释里） | 原文只说「或落共享库」，没说和谁共享——它就不会用这层关系 |
| E4 | 删两条 `<!-- -->` 维护注释 | 清理 | 不变 | 注释是给维护者看的，却按字节进了**每轮**提示词，还读起来像指令。信息没丢：落点改到本文件与 `astrbot-personas` 相关登记 |

**没动的**：「分段」整节（机制仍在通道层，措辞与阈值仍然正确）、禁止项、接话规则、尺度、示例。

### 生效证据（读的是落库的 system prompt，不是「应该生效」）

```
api-979fb398b0fdf2b0  len 14712 | new: ['一次收到好几条', '派活只有 `a2a_call` 这一条路'] | old: []
api-671a7d25fa5eba0e  len 14712 | new: ['一次收到好几条', '派活只有 `a2a_call` 这一条路'] | old: []
api-a1ad92f331dce2e0  len 14458 | new: []                                              | old: ['有联网工具（web_search / web_extract）和看图']
```

（`api_server` 每个请求各自成会话、天然带最新人设，所以**不用 /new、不用删会话**；改完即生效。）

---

## 6. 回退（一条命令，三步都在这）

脚本：`chat-layer/rollback-chat-agent.sh`（默认 dry-run，`--yes` 才真执行）
手动等价：

```bash
# 1) 工具面（回到 14 工具）
hermes -p chat config set platform_toolsets.api_server \
  '["a2a","delegation","file","memory","vision","web"]' --force
# 2) 人设
cp /opt/data/chat-layer/backup/chat-SOUL.md.bak-before-conv-20260923-060059 \
   /opt/data/profiles/chat/SOUL.md
# 3) 重启该门（不承载在线会话）
/package/admin/s6/command/s6-svc -r /run/service/gateway-chat
# 4) 复核
hermes -p chat config get platform_toolsets          # 应含 delegation
curl -s -H "Authorization: Bearer $API_SERVER_KEY" http://127.0.0.1:8643/v1/toolsets   # 应 14 工具
```

**回退粒度**：§4 与 §5 互不依赖，可单独回退任一项。
**B 计划**：若 `s6-svc -r` 后该门没起来（本机已知 `exitcode 78` 且 `finish` 不会自动重启）：`s6-svc -u /run/service/gateway-chat`（`-r` 对已 down 的服务无效），宿主侧兜底 `docker exec hermes /command/s6-svc -u /run/service/gateway-chat`。

---

## 7. 未验证 / 风险（如实）

1. **多调用回合的成本**：`usage.prompt_tokens` 是多调用累计，本次没能给出「记忆回路」与「派活回合」的可比结构数（改前样本丢了）。要严谨对比得改探针：统计**每次 API 调用**而非回合。
2. **`delegation` 删掉的真实收益未独立量化**：结构成本降 908 token 是「删 delegation + SOUL 加长 + 其他波动」的净值，没做单项隔离实测。保守说法：schema 侧约 −1k。
3. **`platform_toolsets.cli` 仍宽**：主人若从 CLI 进 chat profile，仍能拿到 terminal / computer_use。**单容器方案下没有按门收窄 env 的办法**，只能改 s6 `run` 脚本 —— 未做。
4. **第二步的行前件**：`hermes_forward` 源码不在 `chat-layer/plugin/hermes_forward/`（只剩 `stickers/`）。切回纯通道前要先确认插件本体在 `stack/astrbot/data/plugins/`（root 700，容器内列不了，需宿主 sudo）或从备份恢复。
5. **一件事没做也建议别做**：不要给聊天的 profile 加 `HERMES_WRITE_SAFE_ROOT` 的按门收窄而**不改** s6 脚本——env 是容器级的，改了会影响干活门。
