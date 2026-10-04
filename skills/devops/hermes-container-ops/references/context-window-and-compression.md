# 上下文上限与自动压缩（回答「你现在上限是多少」）

## 键语义：`compression` 段（干活门 `/opt/data/config.yaml`，聊天门 `profiles/<name>/config.yaml`）

| 键 | 含义 |
|---|---|
| `threshold` | **占模型窗口的比例**（0.25 = 到窗口 25% 就压缩） |
| `threshold_tokens` | **绝对上限**；两者同时存在时**取低**，apply 时再 clamp 到窗口内（源码 `agent/agent_init.py` 的 `threshold_tokens` 注释） |
| `target_ratio` | 压缩后目标占比 |
| `protect_last_n` / `protect_first_n` | 尾部保留条数 / 头部保护条数（system 恒保护） |
| `idle_compact_after_seconds` | 空闲多久先压一次，0 = 关 |

## 答的时候把三层数分开（「上限」从来不是一个数）

1. **模型窗口**：源码表 `/opt/hermes/agent/model_metadata.py`，**最长键优先**匹配（如 `deepseek-v4-flash` = 1,000,000，同族兜底 `deepseek` = 128,000）→ `grep -n "<模型名>" /opt/hermes/agent/model_metadata.py` 查，别凭感觉。第三方端点可能更低（表里有按 host 覆写的条目）。
2. **触发压缩的阈值** = min(`threshold` × 窗口, `threshold_tokens`)，把推导写出来（例：0.25 × 1M 与 250k 取低 → **250k 触发**），他是拿这个数决定要不要调的。
3. **循环 / 会话上限**：`agent.max_turns`（单次会话回合）、`delegation.max_iterations`（子代理）、`code_execution.timeout|max_tool_calls`、`tool_loop_guardrails`（`hard_stop_enabled` 可为 false = 只告警不硬停）、`session_reset.mode`（`none` = 不自动重置）。

## 口径

- 只读**盘上正在生效的配置**：不读 `.bak-*`，也不照抄技能/记忆里写的旧值——先 `ls -la` 看时间戳，再报数。
- 不同 profile 各有一份 `config.yaml`，别拿干活门的值当聊天门的（报之前先确认问的是哪道门）。
- 压缩阈值是「每轮付费」的量级感知项：报完上限顺手说一句「到 X 就自动压缩、压到 Y」比只丢一个数字有用得多。

## 默认值陷阱：「profile 里没有 `compression` 段」≠ 不压缩

框架默认写在 `/opt/hermes/hermes_cli/config_defaults.py`：`enabled true`、`threshold 0.50`、`threshold_tokens null`、`target_ratio 0.2`、`tail_mode lean`、`protect_last_n 20`、`protect_first_n 3`；`proactive_prune_tokens` 0、`micro_compact false`、`idle_compact_after_seconds 0`（主动瘦身全关）。

- 所以**一段都没写的 profile，触发点 = 0.5 × 窗口**（1M 窗口 → 50 万 token），表现得像「从来不压缩」。先 `grep -n "^compression:" profiles/<p>/config.yaml` 确认这段到底有没有，再报数。
- **窗口 <512K 的模型，`threshold` 会被 floor 到 0.75（raise-only）**（源码同处注释）→ 对这类模型算触发点要用 0.75，不能直接套写死的比例；≥512K 的窗口吃原值。
- 要让某道门与另一道门对齐：显式写要覆盖的键（`enabled` / `threshold` / `threshold_tokens`），其余项继承默认即与干活门相同，不必把默认值抄一遍。

## 实读生效值（比看 yaml 准）

```bash
/opt/hermes/.venv/bin/hermes -p chat config get compression   # 会把默认值展开成实际生效值
/opt/hermes/.venv/bin/hermes config get compression           # 干活门（default profile）
/opt/hermes/.venv/bin/hermes -p chat config get memory        # memory_enabled / user_profile_enabled / nudge_interval / provider
```

- 容器里 `hermes` **不在 PATH**，`/opt/hermes/bin/hermes` 是宿主用的 docker exec shim，别在容器内当 CLI 使 → 用 venv 里的绝对路径（`python3 -c` 之类的探针读 yaml 还会撞 `No module named 'yaml'`）。

## 改完必须重启该门（compression 是「构建期」读的）

- `CompressionSettings` 在 **agent 构建时**读盘并注入 `ContextCompressor`（`agent/agent_init.py`）→ **不像 `platform_toolsets` 那样每轮读盘**：改完不重启，运行进程仍在用旧值。
- 重启**另一道**门是安全的：`/command/s6-svc -r /run/service/gateway-<x>`（本机槽名 `gateway-default` / `gateway-chat`），实测 pid 变化、约 20 秒回 `up`。
- 回线验收看该门日志：`Connecting to <platform>` → `✓ <platform> connected` 全绿 + 适配器监听行（如 `[onebot] reverse-WS listener on 127.0.0.1:6700`）+ 协议端 `client connected from 127.0.0.1`；再看 `errors.log` 无新增。

## 量「这条会话到底多大了」（判断压缩会不会真触发）

`messages.token_count` 列恒为 NULL → 拿不到每轮精确上下文，用两路近似：

```sql
-- 平均每轮 prompt 规模：input_tokens 不含缓存读，必须加上 cache_read_tokens
SELECT id, api_call_count, input_tokens + cache_read_tokens AS prompt_total
FROM sessions ORDER BY last_activity_at DESC LIMIT 10;
```

```python
# 当前上下文规模粗估（中文 ≈1.8 字/token，含英文/JSON 会偏保守）
cur.execute("SELECT COUNT(*), SUM(LENGTH(content)), SUM(LENGTH(reasoning_content)) "
            "FROM messages WHERE session_id=? AND active='1'", (sid,))
```

- 汇报时**标明是估算**，并说明 `token_count` 为空这点，别让数字看起来像实测值；`prompt_total / api_call_count` 是「平均每轮」，上下文单调增长时最后一轮必然高于均值。

## 会话连续性 / 花费取证（回答「是不是每次都会新开对话」「开销是不是很大」）

**别推理，读库**。`<profile>/state.db` 是 sqlite，运行中的 gateway 也持有它 → **只读打开**：

```python
import sqlite3
con = sqlite3.connect("file:/opt/data/profiles/chat/state.db?mode=ro", uri=True)
for r in con.execute("""select session_key, id, message_count, api_call_count, input_tokens,
        cache_read_tokens, estimated_cost_usd, datetime(started_at,'unixepoch','localtime'),
        datetime(last_activity_at,'unixepoch','localtime'), end_reason
        from sessions where session_key like '%onebot%' order by started_at"""):
    print(r)
```

读法（每条对应用户会问的一句话）：

- **「是不是每次唤醒都新开一个对话」** → 看 `session_key` + `started_at`/`last_activity_at`/`end_reason`：同一个键跨天、`end_reason` 为空 = **一条持续会话**。会话键由 `gateway/session.py::build_session_key` 从 SessionSource 现算：私聊 `…:onebot:dm:<QQ>`、群聊 `…:onebot:group:<群号>:<发送者>`（`group_sessions_per_user` 默认 True → **群里每个人一条独立会话**，群友之间不会互串历史；这个开关可以切，见下节「群会话键」）。想看「最近一轮 prompt 多大」再读 `<profile>/sessions/sessions.json` 的 `last_prompt_tokens` + `session_id`（**那是单轮值，不是累计**）。
- **「开销是不是很大」** → 两组数必须分开报：`last_prompt_tokens`（单轮 prompt 规模，四万量级 ≠ 贵）与 `estimated_cost_usd`（这条会话**累计**）。判贵不贵看缓存：`cache_read_tokens / api_call_count ≈ last_prompt_tokens` = 每轮 prompt 基本全命中前缀缓存、**边际成本远低于单价**（实测群会话 34 次调用累计 ≈ $0.024）。
- 表分工：`sessions`（每会话一行）、`messages`（每消息一行，`compacted`/`active`/`_compressed_summary` 可看有没有被压过）、`session_model_usage`（按模型×任务用量）。列名先 `select name from sqlite_master where type='table'` 现查，别照抄（`platform_name` 这类列并不存在）。

⚠️ 同一份 `sessions.json` 里 `last_prompt_tokens` 是单轮、`input_tokens` 才是累计——混用会得出「她一轮烧了四万 token」这种吓人但不成立的结论。
⚠️ 真正把轮次拉长的是**工具调用链**（跑命令/抓网页的那类会话单条就贵一个量级），别把账算在群聊那几十条记录上。

## 群会话键：每人一条 vs 整群一条（可切）

- ⚠️ **生效层是 `config.yaml` 顶层**（这一句曾写错成「只在平台 extra」，实测纠正）：
  会话键由 `SessionStore._generate_session_key()` 用 **`self.config.group_sessions_per_user`** 算，
  而 `self.config` 是 **GatewayConfig**（config.yaml 顶层键，`gateway/config.py` 的 DEFAULTS 里默认 True）。
  平台 `extra` 那份只在 `gateway/platforms/base.py::_source_session_key` 这条**基类兜底路径**
  （telegram/slack 式适配器）才被读。**两份都写上保持一致无妨，但只有顶层那份对 onebot 生效。**
  改完重启该门，并用官方读取器复核：`HERMES_HOME=<profile 目录> /opt/hermes/.venv/bin/hermes config get group_sessions_per_user`。
- **本机 onebot 落点**：顶层键写 `<映射>/profiles/chat/config.yaml` 的顶层（与 `compression:`/`memory:` 同级）。
- **效果**：True → `…:onebot:group:<群号>:<发言人QQ>`（每人一条）；False → 所有发言人都落 `…:onebot:group:<群号>`（整群一条）。
- **只读客户端说的不算**：最终凭据是线上日志 `response ready: … session=…` 那一行的键形——改完没重启、或只改了 extra，键里就还带着尾号。
- **验证手法（不改线上、不起网关）**：cwd=`/opt/hermes`，用 `/opt/hermes/.venv/bin/python`

```python
from types import SimpleNamespace
import gateway.session as S
def src(uid):
    return SimpleNamespace(platform=SimpleNamespace(value="onebot"),
        chat_id="<GROUP_ID>", chat_type="group", user_id=uid, user_name="\u7532")
S.build_session_key(src("<OWNER_QQ>"), group_sessions_per_user=False)
S.build_session_key(src("<OWNER_QQ2>"), group_sessions_per_user=False)   # 应与上一条相同
```

  - source 只需要 `platform` 是个**带 `.value` 的对象**；`Platform("onebot")` 要在插件 `register(ctx)` 之后才存在，**别为了造 source 去注册插件** —— 直接调 `build_session_key`。SimpleNamespace 缺 `thread_id`/`prospective_thread_id` 会 AttributeError，一次补齐。
- **切换的实情**：旧会话行留在库里不再被使用，该群上下文等于从零起步（人设+工具 ≈ 2 万 token 起步）。实测某群旧会话 `last_prompt_tokens` 已涨到 8.7 万，切换顺带把每轮 prompt 砍掉一大半；**要让新键继承旧历史只能手工改 `sessions.session_key`（连带镜像），一般不值得**。
- **取舍**：整群一条 = 上下文连贯、前缀缓存更热；代价是多人发言共处一条历史。**群回合不入主库靠 `hindsight_guard` 拦（每轮带 `source=…-group` 标记），与会话键无关，切换不影响它。**
- `sessions.json` 与 `state.db` 的 `gateway_routing` 只是镜像 / 路由缓存，**权威是 `state.db`**。

## 两个门的自动化参数落点（宿主映射路径，别报容器路径）

| 行为 | 干活门 | 聊天门 |
|---|---|---|
| 人格 + 规则 | `<映射>/SOUL.md` | `<映射>/profiles/chat/SOUL.md` |
| 长期记忆 / 用户画像 | `<映射>/memories/{MEMORY,USER}.md` | `<映射>/profiles/chat/memories/{MEMORY,USER}.md` |
| 自动召回 / 回填 | `<映射>/hindsight/config.json` | `<映射>/profiles/chat/hindsight/config.json` |
| 对话压缩 | `<映射>/config.yaml` 的 `compression` | `<映射>/profiles/chat/config.yaml`（可能没有这段） |
| 群回合记忆隔离 | — | `<映射>/profiles/chat/plugins/hindsight_guard/{config,state}.json` |

- `auto_recall` / `auto_retain` 是 **profile 级、无条件**的（群回合同理会往同一个 bank 写）→ 群不污染主库靠 `hindsight_guard` 拦三处（`sync_turn` / `prefetch`+`queue_prefetch` / 记忆工具调用），计数落它的 `state.json`（`retain_skipped`/`recall_skipped`/`tool_blocked` + `last_chat_type`，**不落正文**）。判定信号两个：主 `chat_type`，兜底正文标记 `source=…-group`；适配器侧 `group_memory_guard_required: true` 时**闸不在位就不唤醒**（fail-closed）。
- ⚠️ **闸只拦 Hindsight，不管 profile 的 `memories/MEMORY.md` / `USER.md`**：那是每轮都注入的系统提示，私聊与群聊**读的是同一份**。她若在群回合调 `memory` 工具，写下的内容私聊也看得见（反向亦然）。要堵这条缝，得把 `memory` 工具也加进闸的 `blocked` 面或按 `chat_type` 拒写；改之前先问主人（这是行为口径，不是 bug）。
- 召回注入量看同文件的 `recall_max_tokens`（两道门可以不同，是「每轮固定成本」的直接参数），另有 `recall_sync` / `recall_budget` / `memory_mode`。
- 两份 `SOUL.md` **从来不自动同步**：改哪一份只影响那道门；汇报改动时把「只影响哪道门」写出来。
