# 改造计划 v2：聊天归 AstrBot 直连，Hermes 只干活

- 日期：2026-09-23 ｜ 提出：主人 ｜ 状态：**勘察中，未动任何配置**
- 前身：`PLAN.md`（分层/Bot Mode 那一版）｜ 本文件是聊天侧的成本与职责重构方案

## 1. 为什么改（有实测数字）

小号现在的链路：QQ → NapCat → AstrBot(`hermes_forward` 插件) → **Hermes 聊天门 api_server（整套 agent）** → 回复。

实测每轮输入（`profiles/chat/state.db`，每次 api_server 会话 = 插件的一次 POST 整包）：

| 会话 | 消息正文 | 系统提示词 | 该轮输入合计 |
|---|---|---|---|
| api-a1ad92f3…（常态闲聊） | 1,789 字符 ≈ 894 tok | 14,458 字符 ≈ **7,229 tok** | **≈ 8,123 tok** |
| api-6e2f193c…（贴了长文那轮） | 74,035 字符 ≈ 37,017 tok | 同上 | **≈ 44,246 tok** |

结论：**固定开销里 89% 是那 14,458 字符的 Hermes 系统提示词**——聊天一句和干一件活的起步价一样，这就是主人说的「token 开销大」。附带问题：分段/换行受「Hermes 提示词 + AstrBot 正则」两层同时影响；两套人格（SOUL + 插件）要同步。

## 2. 目标架构

```
QQ ⇄ NapCat ⇄ AstrBot ──(provider 直聊)──> 自己的 LLM API        ← 闲聊走这条，便宜
                    └──(LLM 决定要干活的 tool 调用)──> 干活门 Hermes（A2A 9901 / api 8642）  ← 派活走这条
                    └──(MCP 客户端)──> Hindsight 记忆库（bank mianmian-history）    ← 按需 recall/retain
```

1. **AstrBot 自己配 provider**：`provider_settings.enable=true` + OpenAI 兼容 provider（DeepSeek）。人格提示词放 AstrBot 侧（从聊天门 SOUL 精简出「说话规则 / 标点 / 分段 / 示例」，砍掉 Hermes 工具规则那几节）。
2. **派活**：LLM 用 function calling 决定「这活要给 Hermes」→ 插件暴露一个 tool → POST 到干活门 → 结果回给 AstrBot 组织成回复。（具体注册方式待子代理勘察确认。）
3. **记忆库**：AstrBot 的 MCP 客户端接 Hindsight（`POST /mcp`，`hindsight-mcp-server 0.8.6`，已实测 200）→ **按需调用**，闲聊不吃召回 token。
4. 通道不变（NapCat / 小号登录态 / A2A 通路 / Hindsight bank 配置都不动）。

## 3. 收益（预期，改造后实测复核）

| 项 | 现状 | 目标 | 依据 |
|---|---|---|---|
| 每轮输入 token | ≈ 8.1k（长文轮 44k） | ≈ 1.7k + 历史（persona 草案实测 **3,447 字符 ≈ 1,723 tok**，已落盘 `astrbot-persona-prompt.md`） | 提示词 14,458→3,447 字符，**降 ~76%**；召回按需 |
| 人格维护点 | 2 套（Hermes SOUL + 插件） | 1 套（AstrBot system_prompt） | |
| 分段/换行 | 两层同时影响，说不清 | 只由 AstrBot 一层控制 | `segmented_reply` 机制已实测 |
| 聊天门 Hermes profile | 常驻网关（进程 + 内存） | 可退役（保留待用，随时切回） | |

## 4. 待确认（子代理勘察中，出结论再动手）

- AstrBot 版本 + provider 配置字段（`api_base` / `model` / key 字段名），以及 enable 后普通消息是否真的不再转发
- 是否支持 llm tool / function calling —— 决定派活是「LLM 自主触发」还是「规则触发」
- MCP 客户端 schema、是否支持远程 HTTP MCP、工具如何进 LLM 工具表
- provider 直聊时 `result_decorate` / `segmented_reply` 是否仍生效
- AstrBot 原生会话历史存储位置与保留策略（会不会与 Hindsight 重复）

## 5. 风险与回滚

- **风险**：AstrBot 侧人格/工具链不稳 → 聊天质量下降；省了 token 但活派不出去（工具调用失败）
- **回滚**：`provider_settings.enable=false` 即刻回到现状；**插件转发路径保持原样不动**，两条路都能走
- **灰度**：先只对**私聊**开 provider 直聊，群聊维持现状；跑 1–2 天对比质量与花费再全量
- **不动**：NapCat / 小号登录态 / A2A 通路 / Hindsight bank / 干活门任何配置

## 6. 验证清单（改造后逐条跑，每条要留证据）

1. 私聊一句闲聊 → **聊天门 `state.db` 不该新增 api_server 会话**（证明没走 Hermes）
2. 私聊一句派活 → 干活门 `state.db` 新增会话 + 结果带回收发
3. 让 AstrBot retain 一条事实 → Hindsight 能 recall 到
4. 分段：多句回复 → 多条气泡，无换行残留
5. 花费对比：同一天同一时段改造前后 input token 数（`hermes insights` + AstrBot 侧日志）

---

## 7. 勘察结论（实测，AstrBot **4.28.1**，2026-09-23）

| 问题 | 结论 | 证据 |
|---|---|---|
| provider 直聊 | 支持，OpenAI 兼容 / DeepSeek 内置模板 | `core/config/default.py:1440`；配置键是 **`provider_sources` + `provider`**（不是 `providers`） |
| ⚠️ 关键坑 | **开 provider 不会让插件闭嘴** → 现有 `hermes_forward` 与 LLM 会各回一次（双回复） | `process_stage/stage.py:33-66`：插件只 yield 不 send → 不设 `_has_send_oper`；`star_request.py:53` 每轮 clear_result |
| LLM 工具（派活） | 支持 `@filter.llm_tool`；**`Args:` docstring 必须写**（它读 docstring，不读类型注解） | `star/register/star_handler.py:586`；`docs/zh/dev/star/guides/ai.md:97-120` |
| MCP 客户端 | 支持 stdio / sse / **streamable_http** | `core/agent/mcp_client.py:474-477,570-598` |
| Hindsight 接得上 | ✅ **实测通**：AstrBot 容器内 POST `http://172.17.0.1:8888/mcp` → HTTP 200，`serverInfo=hindsight-mcp-server 0.8.6`，SSE 帧正是 streamable_http 要的格式 | 本次实测 |
| 分段还生效吗 | **不冲突**：同一条 pipeline，`ResultDecorate` 照跑；`aiocqhttp` 不在排除名单 | `pipeline/stage_order.py`；`result_decorate/stage.py:200-267` |
| 自带历史 | 有：`data/data_v4.db` 的 `conversations`（现 0 行）；`max_turns=-1` 不按轮数截断，超 ~128k token 走 LLM 压缩 | `conversation_mgr.py:279`；`agent/context/manager.py:60` |
| 网络 | AstrBot 与 Hermes 都是 **host** 网络 | `docker inspect` 实测 |

## 8. 切换步骤（备好待点头；可一键回滚）

1. **备份**：`cmd_config.json`、`mcp_server.json`、插件目录、`data_v4.db`（只读复制）
2. **provider**：`provider_sources:[{id:"deepseek", type:"openai_chat_completion", provider_type:"chat_completion", enable:true, key:["$DEEPSEEK_API_KEY"], api_base:"https://api.deepseek.com/v1", timeout:120}]` + `provider:[{id:"deepseek-chat", provider_source_id:"deepseek", model:"deepseek-chat", enable:true, provider_type:"chat_completion"}]`；**key 走环境变量，不落明文**（给 AstrBot 容器补该 env）
3. **人格**：`astrbot-persona-prompt.md` 正文做 AstrBot persona，设默认
4. **插件**：摘掉 `hermes_forward` 的消息处理 handler，只留 `@filter.llm_tool(name="delegate_to_hermes")` 一个工具。**派活走 A2A**：AstrBot 容器（host 网络）内 `http://127.0.0.1:9901` **实测可达（HTTP 200）**，同步等到对端超时 300 秒。⚠️ **这一步必须和 ② 同批做，否则双回复**；旧插件源码原样留档可回滚
5. **记忆**：`mcp_server.json` → `{"mcpServers":{"hindsight":{"url":"http://172.17.0.1:8888/mcp","transport":"streamable_http","active":true}}}`
6. **重启 AstrBot**（小号断约 20–30 秒）
7. **回滚**：`provider_settings.enable=false` + 还原插件备份 + 重启 = 回到现状
8. **验证**：跑第 6 节那 5 条，逐条留证据

