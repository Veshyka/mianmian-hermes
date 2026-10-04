#!/usr/bin/env python3
"""边界执行自测：**直接跑 hermes_lookup 真身的 enforce_boundary**（线上那份 main.py）。

为什么不是看日志：`边界执行` 只在有真 LLM 请求时打。这里用一个假 req 走同一条代码路径，
就能随时证明「哪些工具被摘、哪些留下、那段英文提示被怎么改」。

做法：`object.__new__(Main)` 不跑 `__init__`（要 Context），只塞 `config` 和 `_allow_cache`；
`_persona_allowlist()` 是原方法（只读 data_v4.db），读到的就是线上人格白名单。

用法（容器内）：python3 /AstrBot/data/_selftest_boundary.py
"""

import asyncio
import importlib.util
import sys

sys.path.insert(0, "/AstrBot")

PLUGIN = "/AstrBot/data/plugins/hermes_lookup/main.py"

# 这是 AstrBot local runtime 每轮无条件塞进来的那批（astr_main_agent.py:1643 _apply_local_env_tools）
SYSTEM_TOOLS = [
    "astrbot_execute_shell", "astrbot_shell_session", "astrbot_execute_python",
    "astrbot_file_read_tool", "astrbot_file_write_tool", "astrbot_file_edit_tool",
    "astrbot_grep_tool",
]

# astr_main_agent.py:1706-1722 原样注入的那段英文（含 workspace 路径，用真的那种形状）
INJECTED = (
    "\nCurrent workspace: `/AstrBot/data/workspace/aiocqhttp_FriendMessage_<OWNER_QQ>`. "
    "`astrbot_execute_shell` and `astrbot_execute_python` use it as their working directory. "
    "`astrbot_file_read_tool`, `astrbot_file_write_tool`, `astrbot_file_edit_tool`, and "
    "`astrbot_grep_tool` resolve relative paths from it. Prefer relative paths within the "
    "workspace; do not assume this behavior for other tools.\n"
    "You have access to the host local environment and can execute shell commands and Python "
    "code. To keep a persistent shell session, use astrbot_shell_session; it will automatically "
    "return a managed session.\n"
)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  ✅ %s %s" % (name, detail))
    else:
        FAIL.append(name)
        print("  ❌ %s %s" % (name, detail))


class FakeTool:
    def __init__(self, name):
        self.name = name


class FakeToolSet:
    def __init__(self, names):
        self.tools = [FakeTool(n) for n in names]

    def remove_tool(self, name):
        self.tools = [t for t in self.tools if t.name != name]


class FakeReq:
    def __init__(self, names, system_prompt):
        self.func_tool = FakeToolSet(names)
        self.system_prompt = system_prompt


def load_plugin():
    spec = importlib.util.spec_from_file_location("hermes_lookup_live", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hermes_lookup_live"] = mod
    spec.loader.exec_module(mod)
    return mod


async def main():
    mod = load_plugin()
    plugin = object.__new__(mod.Main)          # 不走 __init__（要 Context）
    plugin.config = dict(mod.DEFAULTS)
    plugin._allow_cache = None
    plugin._allow_cache_at = 0.0

    allow = plugin._persona_allowlist()
    print("== 线上人格白名单（%d 项）==" % len(allow))
    print("  ", ", ".join(allow))

    file_tools = ["astrbot_file_read_tool", "astrbot_file_write_tool",
                  "astrbot_file_edit_tool", "astrbot_grep_tool"]
    print("\n== 1. 白名单里有文件四件套 ==")
    for t in file_tools:
        check("白名单含 %s" % t, t in allow)

    print("\n== 2. 白名单里没有 shell / 解释器 ==")
    for t in ("astrbot_execute_shell", "astrbot_shell_session", "astrbot_execute_python"):
        check("白名单不含 %s" % t, t not in allow)

    # 模拟一轮真实请求：AstrBot 把 7 个系统工具 + 英文提示全塞进来
    names = SYSTEM_TOOLS + ["web_search", "delegate_to_hermes", "recall", "memo_view"]
    req = FakeReq(names, INJECTED)
    await plugin.enforce_boundary(None, req)
    left = sorted(t.name for t in req.func_tool.tools)

    print("\n== 3. 每轮剪枝后的真实工具面 ==")
    print("  剩余:", ", ".join(left))
    print("\n  提示词里还留下的那段:")
    for ln in req.system_prompt.split("\n"):
        if ln.strip():
            print("   |", ln)

    print("\n== 4. 断言 ==")
    for t in file_tools:
        check("剪枝后仍留着 %s" % t, t in left)
    for t in ("astrbot_execute_shell", "astrbot_shell_session", "astrbot_execute_python"):
        check("剪枝后摘掉 %s" % t, t not in left)
    for t in ("web_search", "delegate_to_hermes", "recall", "memo_view"):
        check("剪枝后仍留着 %s" % t, t in left)

    sp = req.system_prompt
    check("英文提示里不再说能跑 shell", "host local environment" not in sp)
    check("英文提示里不再有 astrbot_execute_shell", "astrbot_execute_shell" not in sp)
    check("英文提示里不再有 astrbot_execute_python", "astrbot_execute_python" not in sp)
    check("英文提示里不再提 astrbot_shell_session", "astrbot_shell_session" not in sp)
    check("workspace 相对路径提示还在（文件工具用得上）", "Current workspace:" in sp)
    check("workspace 句仍然指向文件四件套", "astrbot_file_read_tool" in sp)

    print("\n==== 结果：%d 通过 / %d 失败 ====" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项:", FAIL)
        raise SystemExit(1)
    print("BOUNDARY_SELFTEST_OK")


asyncio.run(main())
