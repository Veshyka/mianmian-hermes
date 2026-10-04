"""AstrBot 补丁 2（2026-09-23）：运行上下文形参与工具自身参数重名，彻底修掉。

补丁 1 让 call_local_llm_tool 的 context 变成仅位置参数，但工具那一侧还有两层同样
首参名叫 `context`：
  - _PermissionGuardedTool.call(self, context, **kwargs)   # 所有工具都被它包一层
  - MCPTool.call(self, context, **kwargs)                  # Hindsight 的 retain 自带 context 参数
于是 `handler(context, **{"context": "..."})` 仍会 TypeError:
  _PermissionGuardedTool.call() got multiple values for argument 'context'
（MCP 工具恰恰带 context 参数 → 必然踩中。）

改法：两处首参加 PEP 570 `/`（仅位置），工具自带的 context 就能正常落进 **kwargs。
改前已确认全库只有两处调用这两个 call，且都是位置传参（astr_agent_tool_exec.py:737、
func_tool_manager.py:269），加 `/` 不影响。
"""
import io
import os
import py_compile
import shutil
import time

BK = f"/AstrBot/data/_setup_backup_{time.strftime('%Y%m%d-%H%M%S')}"
os.makedirs(BK, exist_ok=True)

TARGETS = [
    # (路径, 旧文本, 新文本, 已打补丁的判据)
    (
        "/AstrBot/astrbot/core/provider/func_tool_manager.py",
        "    async def call(self, context: Any, **kwargs: Any) -> Any:",
        "    async def call(self, context: Any, /, **kwargs: Any) -> Any:",
        "    async def call(self, context: Any, /, **kwargs: Any) -> Any:",
    ),
    (
        "/AstrBot/astrbot/core/agent/mcp_client.py",
        "    async def call(\n        self, context: ContextWrapper[TContext], **kwargs\n    ) -> mcp.types.CallToolResult:",
        "    async def call(\n        self, context: ContextWrapper[TContext], /, **kwargs\n    ) -> mcp.types.CallToolResult:",
        "        self, context: ContextWrapper[TContext], /, **kwargs",
    ),
    # 附带：把被吞掉的原始 TypeError 写进报错文本，便于日后一眼定位
    (
        "/AstrBot/astrbot/core/astr_agent_tool_exec.py",
        '            f"Tool handler parameter mismatch, please check the handler definition. Handler parameters: {handler_param_str}"',
        '            f"Tool handler parameter mismatch, please check the handler definition. '
        'Original error: {e}. Handler parameters: {handler_param_str}"',
        "Original error: {e}. Handler parameters:",
    ),
]

changed = []
for path, old, new, marker in TARGETS:
    src = io.open(path, encoding="utf-8").read()
    name = os.path.basename(path)
    if marker in src:
        print(f"跳过（已是目标状态）：{name}")
        continue
    assert src.count(old) == 1, f"{name} 锚点命中 {src.count(old)} 次，未改动"
    shutil.copy2(path, f"{BK}/{name}")
    io.open(path, "w", encoding="utf-8").write(src.replace(old, new, 1))
    py_compile.compile(path, doraise=True)
    changed.append(name)
    print(f"已改：{name}（py_compile 通过）")

print(f"\n改动文件：{changed or '无'}｜备份目录：{BK}")
