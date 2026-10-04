#!/usr/bin/env python3
"""AstrBot 本地工具 context 补丁 —— 机器判定自检（只读静态分析，不 import astrbot）。

判据（AST + 文本，全部机器判定，不靠眼看）：
  1. astr_agent_tool_exec.py 的 call_local_llm_tool 首参 context 是「仅位置参数」
  2. astr_agent_tool_exec.py 的唯一调用点已改位置传参（不再出现 call_local_llm_tool(...context=run_context）
  3. func_tool_manager.py 的 _PermissionGuardedTool.call 的 context 是仅位置参数
  4. mcp_client.py 的 MCPTool.call 的 context 是仅位置参数
  5. 报错文案带真因（Original error:）

退出码 0 = 全部通过（可用来做重建后的自动验收）；1 = 有缺失（逐条打印缺哪条）。
"""
import ast
import io
import sys

CORE = "/AstrBot/astrbot/core"
FAIL = []
FuncDef = (ast.FunctionDef, ast.AsyncFunctionDef)  # async def 是 AsyncFunctionDef，别只判 FunctionDef


def read(path):
    try:
        return io.open(path, encoding="utf-8").read()
    except OSError as exc:
        FAIL.append(f"{path}: 读不到（{exc}）")
        return ""


def posonly_check(src, path, func_name, cls=None, first_arg="context"):
    """定位函数（可选限定 cls 类体），要求 first_arg 存在于仅位置参数里。

    类方法里 self 天然是第一个参数，所以判据是「first_arg ∈ posonlyargs」而不是「args[0] 是它」。
    """
    fn = path.split("/")[-1]
    tree = ast.parse(src)
    hits = []
    for node in ast.walk(tree):
        if not (isinstance(node, FuncDef) and node.name == func_name):
            continue
        if cls is not None:
            in_cls = any(isinstance(n, ast.ClassDef) and n.name == cls and node in n.body
                         for n in ast.walk(tree))
            if not in_cls:
                continue
        hits.append(node)
    if not hits:
        FAIL.append(f"{fn}: 找不到 {'%s.' % cls if cls else ''}{func_name}")
        return
    for node in hits:
        allargs = [a.arg for a in list(node.args.posonlyargs) + list(node.args.args)]
        posonly = [a.arg for a in node.args.posonlyargs]
        if first_arg not in allargs:
            FAIL.append(f"{fn}: {func_name} 参数里没有 {first_arg}（{allargs}）")
        elif first_arg not in posonly:
            FAIL.append(f"{fn}: {func_name} 的 {first_arg} 不是仅位置参数（posonlyargs={posonly}）")


ex_path = f"{CORE}/astr_agent_tool_exec.py"
ex = read(ex_path)
if ex:
    posonly_check(ex, ex_path, "call_local_llm_tool")
    if "call_local_llm_tool(\n            context=run_context," in ex:
        FAIL.append("astr_agent_tool_exec.py: 调用点仍是 context=run_context（关键字传参）"
                    "→ 会全线 missing 1 required positional argument: 'context'")
    if "call_local_llm_tool(\n            run_context," not in ex:
        FAIL.append("astr_agent_tool_exec.py: 调用点没找到位置传参写法（run_context,）")
    if "Original error: {e}" not in ex:
        FAIL.append("astr_agent_tool_exec.py: 报错文案未带真因（Original error: {e}）")

tm = read(f"{CORE}/provider/func_tool_manager.py")
if tm:
    posonly_check(tm, f"{CORE}/provider/func_tool_manager.py", "call", cls="_PermissionGuardedTool")

mc = read(f"{CORE}/agent/mcp_client.py")
if mc:
    posonly_check(mc, f"{CORE}/agent/mcp_client.py", "call", cls="MCPTool")

if FAIL:
    print("[VERIFY] 补丁未生效 ✗")
    for f in FAIL:
        print("   -", f)
    sys.exit(1)

print("[VERIFY] 补丁已生效 ✓（3 处 context 仅位置参数 + 调用点位置传参 + 报错文案带真因）")
sys.exit(0)
