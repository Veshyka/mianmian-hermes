# hermes_memo —— 聊天门的「常驻记忆块」

> 一句话：把一份文件**每轮注入她的 system prompt**，并且**她自己能改它** ——
> 补上 Hindsight 按查询召回给不了的那层「一直都在」的底。

## 为什么要它（判断依据）

| | 原来有什么 | 缺什么 |
|---|---|---|
| AstrBot 会话历史 | `data_v4.db` 按会话存对话 | 跨会话不连续，隔天就断片 |
| `hermes_memory` 召回 | 按**当前这句话**语义检索 Hindsight | 检索不到 = 等于没有；「一直都在」的底色给不了 |
| **本插件** | 一份文件固定挂在 system prompt 里，每轮都在 | ——（这就是 Hermes 值钱的那块） |

Hermes 侧的实现（读源码得出的对应关系，不是搬代码）：
`agent/system_prompt.py` 的 volatile 层把 `tools/memory_tool_store.py::MemoryStore` 渲染出的
`MEMORY.md` / `USER.md` 块拼进系统提示词，模型每轮都看得见，且随时能用 `memory` 工具改写它。

## 机制

```
每轮请求 → build_main_agent（人格 + 技能拼进 req.system_prompt）
        → call_event_hook(event, OnLLMRequestEvent, req)   ← 本插件挂这儿
        → req.system_prompt += 常驻记忆块（追加在**最尾部**）
        → 真发请求
```

- 注入点：官方钩子 `@filter.on_llm_request()`，调用点
  `core/pipeline/process_stage/method/agent_sub_stages/internal.py:352`。
- **追加在尾部**：人格与技能那一段保持稳定（护住前缀缓存），只有这一块每轮可能变；
  每轮现读文件 → 她改完**下一条消息就生效**。
- **幂等**：同一个 `req` 里已有块就不再塞（一轮对话会发多次请求）。
- 任何异常只在插件内吞掉，绝不影响她的回复；失败降级 = 这轮不注入。

## 数据文件

| | |
|---|---|
| 容器内 | `/AstrBot/data/mianmian-memo/MEMORY.md` |
| 宿主（主人可直接打开） | `/vol1/1000/<USER>` |
| 分隔符 | `\n§\n`（**与 Hermes 的 MEMORY.md 同款**，两边内容可以直接互抄） |
| 默认上限 | 整块 2500 字，单条 400 字 |

文件在 bind mount 里 → **重建容器不丢**。权限 `755/644`（root 属主，任何人可读；
容器内 root 负责写）。

## 她的工具（`@filter.llm_tool`，docstring 的 `Args:` 段就是参数说明）

| 工具 | 作用 |
|---|---|
| `memo_view` | 看全文 + 用量 |
| `memo_add(content)` | 追加一条（重复的自动跳过） |
| `memo_replace(old_text, content)` | 整条替换（匹配多条时不猜，要她说具体点） |
| `memo_remove(old_text)` | 删一条 |

超上限时的行为与 Hermes 一致：**拒绝写入 + 回吐当前条目 + 要她在本轮内先合并/删旧的再重试**。

## 配置

`/AstrBot/data/config/hermes_memo_config.json`（schema 见 `_conf_schema.json`）：

| 键 | 默认 | 说明 |
|---|---|---|
| `enable` | true | 总开关 |
| `memo_path` | `/AstrBot/data/mianmian-memo/MEMORY.md` | 记忆文件（容器视角） |
| `char_limit` / `entry_max_chars` | 2500 / 400 | 整块 / 单条上限 |
| `seed_if_missing` | true | 文件不存在时写入初始条目（只写一次，之后只由她自己改） |
| `inject_enable` | true | 是否每轮注入 |
| `inject_umos` | `["*"]` | 注入范围（默认全部会话：连贯性不分群私；要收紧就填具体 umo） |
| `tools_enable` | true | 是否允许她写 |
| `write_umos` | 主人私聊 | **写权限白名单**（防群聊里的人改她的长期记忆） |

## 验证（三层，缺一层都不算过）

```bash
# ① 纯逻辑 / 类行为（沙箱文件，不碰线上记忆）
cd /opt/data/scripts && bash astr_run.sh hermes_memo_selftest.py

# ② 真框架探针：真装饰器 + 真事件 + 真 call_event_hook + 真工具 handler
cd /opt/data/scripts && bash astr_run.sh hermes_memo_live_probe.py

# ③ 线上运行态（重启后）：就绪行 + 真请求的注入行
docker logs --since 10m astrbot | grep -a hermes_memo
#   [hermes_memo] 就绪 path=… 条目=6 用量=463/2500 字 seed=False | 注入=on(umo=*) | 工具=on(可写=…)
#   [hermes_memo] 注入 n=6 chars=761 用量=18% — 463/2,500 字 umo=aiocqhttp:FriendMessage:<OWNER_QQ>
```

主人可感知的验证：私聊问她「你现在常驻记忆里有什么」，她会背全文；
或者直接打开上面那个宿主路径看文件。

## 已知取舍（别当 bug 查）

- 记忆块约 **760 字 ≈ 380 token/轮**（DeepSeek 上是钱），换来的是跨会话连贯性。
  嫌贵就调小 `char_limit`，或把 `inject_umos` 收成只对主人私聊。
- 注入块在 system prompt 尾部，**每轮都占上下文**；这不叫「缓存失效」，
  前缀缓存仍在（人格+技能段没变）。
- 模型自己写记忆 = 她可能写进不合适的东西。闸门有两条：单条 400 字、总 2500 字上限
  （逼她合并），以及只有主人私聊能写。真出问题就手动改那个文件。
- **改插件源码后要同步**：`/opt/data/chat-layer/plugin/hermes_memo/`（源码副本）
  → `/opt/data/stack/astrbot/data/plugins/hermes_memo/`（容器加载的那份），
  两侧 md5 必须一致；容器没开热重载（`ASTRBOT_RELOAD`）时要重启 AstrBot 才生效。
