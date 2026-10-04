# AstrBot 本地工具 context 传参 bug —— 修复与验证证据（2026-09-23）

派单来源：聊天门 A2A 代主人派单。执行：棉棉（干活门）。

## 1. 根因

`TypeError: … got multiple values for argument 'context'` 是**参数名撞名**，分三层，前一层修完才暴露下一层：

| 层 | 位置 | 说明 |
|---|---|---|
| ① | `astr_agent_tool_exec.py:743` `call_local_llm_tool(context, …)` | 唯一调用点用 `context=run_context` 关键字传 → 工具自带 `context` 参数一起进 kwargs 时撞名。**症状 2**。若只给它加 PEP 570 `/` 而不同步改调用点 → 本地工具全线 `missing 1 required positional argument: 'context'`。**症状 1**。 |
| ② | `provider/func_tool_manager.py:246` `_PermissionGuardedTool.call(self, context, **kwargs)` | `get_full_tool_set()` 把**每个**工具都包一层它；`_execute_local` 的 `call` 分支是 `handler(context, **kwargs)` → 工具参数里的 `context` 再撞一次。Hindsight `retain` 参数表自带 `context` → 必踩。 |
| ③ | `agent/mcp_client.py:814` `MCPTool.call(self, context, **kwargs)` | ② 委托给它，同一坑的第二层。 |

**症状 3 的由来**：`call_local_llm_tool` 用 `except TypeError` 兜底，把真错换成
`Tool handler parameter mismatch … Handler parameters: {params[1:]}`，而 `call(self, context, **kwargs)`
跳过首参只剩 `kwargs: Any` → 报错文本与真因完全脱节，看着像「handler 定义错了」。

## 2. 改动（3 个文件，2 处加 `/` + 1 处补真因）

```diff
--- a/func_tool_manager.py
+++ b/func_tool_manager.py
@@ -244,5 +244,5 @@
-    async def call(self, context: Any, **kwargs: Any) -> Any:
+    async def call(self, context: Any, /, **kwargs: Any) -> Any:

--- a/mcp_client.py
+++ b/mcp_client.py
@@ -813,5 +813,5 @@
     async def call(
-        self, context: ContextWrapper[TContext], **kwargs
+        self, context: ContextWrapper[TContext], /, **kwargs
     ) -> mcp.types.CallToolResult:

--- a/astr_agent_tool_exec.py
+++ b/astr_agent_tool_exec.py
@@ -799,5 +799,5 @@
         raise Exception(
-            f"Tool handler parameter mismatch, please check the handler definition. Handler parameters: {handler_param_str}"
+            f"Tool handler parameter mismatch, please check the handler definition. Original error: {e}. Handler parameters: {handler_param_str}"
         ) from e
```

- 脚本：`/opt/data/tmp/patch_astrbot_context_v3.py`（幂等 + 断言 + `py_compile`）
- 备份：`/AstrBot/data/_setup_backup_20260923-043217/`（3 个原始文件）
- 回滚：把那 3 个文件覆盖回 `/AstrBot/` 对应路径（容器内路径：`/AstrBot/data/_setup_backup_20260923-043217/`）后 `docker restart astrbot`
- 安全性前置核查：全库 `call(` 调用点只有两处，且都是位置传参（`astr_agent_tool_exec.py:737`、`func_tool_manager.py:269`）→ 加 `/` 不影响任何调用方

## 3. 验证（容器内 scratch 进程跑**真实执行链路**）

链路：`FunctionToolExecutor.execute → _execute_local → call_local_llm_tool → _PermissionGuardedTool.call → 真 MCPTool.call / 真内置工具`
替换项仅两处（与 bug 无关）：star Context（MCP 路径用不到）、权限 manager（借真实 `_check_tool_permission` 本体，只给空壳 self）。
脚本：`/AstrBot/data/mianmian-tmp/verify_tools.py`；执行器：`/opt/data/scripts/astr_run.sh`。

### 3.1 修复前（复现，04:32 之前）

```
包装后类型: _PermissionGuardedTool | call 签名: (self, context: 'Any', **kwargs: 'Any') -> 'Any'
绑定自检: 绑定失败 ✗ multiple values for argument 'context'
  File "/AstrBot/astrbot/core/astr_agent_tool_exec.py", line 766, in call_local_llm_tool
    ready_to_call = handler(context, *args, **kwargs)
TypeError: _PermissionGuardedTool.call() got multiple values for argument 'context'
  …
Exception: Tool handler parameter mismatch, please check the handler definition. Handler parameters: kwargs: Any
```

与容器日志（`docker logs astrbot`）里 03:35 / 04:18 / 04:25 的三条报错逐字一致。

### 3.2 修复后（04:32）

```
包装后类型: _PermissionGuardedTool | call 签名: (self, context: 'Any', /, **kwargs: 'Any') -> 'Any'
[retain] 绑定自检：call(运行上下文, context=工具参数) 绑定成功 ✓
[retain] ← 返回: TextContent(text='{"status":"accepted","message":"Memory storage initiated","operation_id":"ea4519f5-b252-485e-9f05-dd82805c24fc"}') isError=False
[local] ← 返回: 本地工具收到参数: ['content', 'context', 'path']
[local] handler 实收参数: ['content', 'context', 'path'] | context='工具参数里的 context'
[file:文本] ← 返回: TextContent(text='棉棉的工具链路验证文本：astrbot_file_read_tool 走真实执行链路读到我。\n')
[file:图片] ← 返回(52980 字符): ImageContent(type='image', data='iVBORw0KGgoAAAANSUhEUgAAAfQAAAHnCAYAAABOlK+/…')
```

### 3.3 落库回读（不只信工具返回）

```
GET /v1/default/banks/mianmian-history/operations?limit=50
  69e0b237 batch_retain completed   817400c6 retain completed
  ea4519f5 batch_retain completed   1a46cb36 retain completed
GET /v1/default/banks/mianmian-history/documents?limit=6
  2026-09-22T20:35:30Z tags=['chat','hermes','tool-fix-verify'] len=76  id=6a18a2de-b246-473b-89d5-0025f9b06d51
  2026-09-22T20:35:42Z tags=['chat','hermes','tool-fix-verify'] len=76  id=8da1f204-2563-40e8-9398-42ec06b95df2
```
（两条测试记忆当时留在库里，tag `tool-fix-verify` → **2026-09-23 已清理，全库复核 0 残留**，见 `astrbot-patches.md`「测试残留清理」节；只剩 4 条 completed 的 operation 记录因 API 限制删不掉。）

### 3.4 上线

`docker restart astrbot` @ 04:34:25（补丁落盘 04:32:17 < 进程启动时间，故运行中的进程即补丁代码）：

```
[04:34:25.779] [Core] AstrBot started.
[04:34:25.484] Connected to MCP server hindsight, Tools: ['retain','sync_retain','recall',…] （30 个）
[04:34:27.723] aiocqhttp(OneBot v11) 适配器已连接。
hermes_delegate / hermes_memory / astrbot / builtin_commands 插件全部正常加载
```
