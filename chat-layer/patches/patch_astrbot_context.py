"""AstrBot 本地工具 context 传参 —— 全部补丁（合并版，幂等，可在全新容器里重打）。

合并说明：原来分两次打（v2 修 call_local_llm_tool + 调用点；v3 修两处 call 首参 + 报错文案）。
容器重建后只重打其中一个 = 仍然坏（v3 单独打 → 调用点还是 context= 关键字传参 → 全线
missing 'context'；v2 单独打 → 工具自带 context 参数仍撞 `_PermissionGuardedTool.call`）。
所以本脚本一次覆盖全部 5 处锚点，是重建后唯一的重打入口。

5 处改动：
  1. astr_agent_tool_exec.py:743  call_local_llm_tool 首参加 PEP 570 `/`（仅位置参数）
  2. astr_agent_tool_exec.py:683  唯一调用点 context=run_context → 位置传参
  3. provider/func_tool_manager.py:246  _PermissionGuardedTool.call(self, context, /, **kwargs)
  4. agent/mcp_client.py:814            MCPTool.call(self, context, /, **kwargs)
  5. astr_agent_tool_exec.py:800        报错文案带上真因（Original error:）

特性：幂等（已是目标状态则跳过）、锚点命中数断言、py_compile 自检、自动备份到
      /AstrBot/data/_setup_backup_<ts>/、退出码非 0 表示有文件没改成功。
"""
import io
import os
import py_compile
import shutil
import sys
import time

BK = f"/AstrBot/data/_setup_backup_{time.strftime('%Y%m%d-%H%M%S')}"
os.makedirs(BK, exist_ok=True)

# (路径, 旧文本, 新文本, 「已是目标状态」判据)
TARGETS = [
    (
        "/AstrBot/astrbot/core/astr_agent_tool_exec.py",
        "async def call_local_llm_tool(\n"
        "    context: ContextWrapper[AstrAgentContext],\n"
        "    handler: T.Callable[",
        "async def call_local_llm_tool(\n"
        "    context: ContextWrapper[AstrAgentContext],\n"
        "    /,\n"
        "    handler: T.Callable[",
        "context: ContextWrapper[AstrAgentContext],\n    /,",
    ),
    (
        "/AstrBot/astrbot/core/astr_agent_tool_exec.py",
        "        wrapper = call_local_llm_tool(\n            context=run_context,",
        "        wrapper = call_local_llm_tool(\n            run_context,",
        "call_local_llm_tool(\n            run_context,",
    ),
    (
        "/AstrBot/astrbot/core/provider/func_tool_manager.py",
        "    async def call(self, context: Any, **kwargs: Any) -> Any:",
        "    async def call(self, context: Any, /, **kwargs: Any) -> Any:",
        "    async def call(self, context: Any, /, **kwargs: Any) -> Any:",
    ),
    (
        "/AstrBot/astrbot/core/agent/mcp_client.py",
        "    async def call(\n        self, context: ContextWrapper[TContext], **kwargs\n"
        "    ) -> mcp.types.CallToolResult:",
        "    async def call(\n        self, context: ContextWrapper[TContext], /, **kwargs\n"
        "    ) -> mcp.types.CallToolResult:",
        "        self, context: ContextWrapper[TContext], /, **kwargs",
    ),
    (
        "/AstrBot/astrbot/core/astr_agent_tool_exec.py",
        '            f"Tool handler parameter mismatch, please check the handler definition. '
        'Handler parameters: {handler_param_str}"',
        '            f"Tool handler parameter mismatch, please check the handler definition. '
        'Original error: {e}. Handler parameters: {handler_param_str}"',
        "Original error: {e}. Handler parameters:",
    ),
]

changed, skipped, failed = [], [], []

# 每个文件只备份/写回一次，逐条改动累积在内存里
cache = {}
for path, old, new, marker in TARGETS:
    if path not in cache:
        try:
            cache[path] = io.open(path, encoding="utf-8").read()
        except OSError as exc:
            failed.append(f"{path}: 读不到（{exc}）")
            continue
    src = cache[path]
    name = os.path.basename(path)
    if marker in src:
        print(f"跳过（已是目标状态）：{name} :: {old.strip().splitlines()[0][:60]}")
        skipped.append(name)
        continue
    n = src.count(old)
    if n != 1:
        failed.append(f"{name}: 锚点命中 {n} 次（要求恰好 1 次）→ 「{old.strip().splitlines()[0][:70]}」")
        continue
    cache[path] = src.replace(old, new, 1)
    changed.append(f"{name} :: {old.strip().splitlines()[0][:60]}")

for path, src in cache.items():
    orig = io.open(path, encoding="utf-8").read()
    if orig == src:
        continue
    if not os.path.exists(f"{BK}/{os.path.basename(path)}"):
        shutil.copy2(path, f"{BK}/{os.path.basename(path)}")
    io.open(path, "w", encoding="utf-8").write(src)
    try:
        py_compile.compile(path, doraise=True)
        print(f"已写入并通过 py_compile：{os.path.basename(path)}")
    except py_compile.PyCompileError as exc:
        failed.append(f"{os.path.basename(path)}: py_compile 失败 {exc}")

print(f"\n改动 {len(changed)} 处 / 跳过 {len(skipped)} 处｜备份目录（有改动才有内容）：{BK}")
if failed:
    print("失败项：")
    for f in failed:
        print("   -", f)
    sys.exit(1)
print("PATCH_OK")
