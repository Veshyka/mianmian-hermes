#!/usr/bin/env python3
"""列出某个 Hermes profile / 平台**真实生效的工具面**（工具集 → 工具名 + 一句话描述）。

用法：
    HERMES_HOME=/opt/data/profiles/chat /opt/hermes/.venv/bin/python \
        dump_platform_tools.py [平台名 ...]
    不带平台名 → 打印该 profile config 里 platform_toolsets 的全部平台。

为什么不用官方 CLI：`hermes tools list` 与 `hermes tools --summary` 都要求交互终端，
管道/非交互 subprocess 直接报 "requires an interactive terminal" → 会话内取不到数。

三个必须知道的点：
  1. `hermes_cli.tools_config._get_platform_tools(cfg, platform)` 返回的是**工具集名**
     （如 a2a / cronjob / file / memory / session_search / skills / vision / web），**不是工具名**；
     平台没显式配 platform_toolsets 时它回落到平台默认（插件平台 = 全套 core tools）。
  2. 工具集 → 工具名 要用 `toolsets.py::TOOLSETS` 展开（`tools` + `includes` 递归）。
     **插件工具集不在 TOOLSETS 里**（如 a2a），要读插件自己的注册
     （`plugins/platforms/a2a/tools.py` 的 `register_tool(toolset="a2a", …)`）。
  3. 描述从 `model_tools.get_tool_definitions(quiet_mode=True)` 的全量 schema 里查
     （本机实测 43 条，含插件工具）——但它**与平台无关**，只是名字→描述的字典。
"""
import os
import sys

HERMES_ROOT = os.environ.get("HERMES_ROOT", "/opt/hermes")
PROFILE_HOME = os.environ.get("HERMES_HOME", "/opt/data")

sys.path.insert(0, HERMES_ROOT)
os.chdir(HERMES_ROOT)  # 插件发现依赖相对路径

from hermes_cli.config import load_config_readonly  # noqa: E402
from hermes_cli.tools_config import _get_platform_tools  # noqa: E402
from toolsets import TOOLSETS  # noqa: E402

cfg = load_config_readonly()
platforms = sys.argv[1:] or list((cfg.get("platform_toolsets") or {}).keys())

descs = {}
try:
    import model_tools  # noqa: E402

    for d in model_tools.get_tool_definitions(quiet_mode=True):
        fn = d.get("function") or {}
        descs[fn.get("name")] = fn.get("description") or ""
except Exception as exc:  # pragma: no cover - 只为诊断信息
    print(f"# 警告: 取不到全量 schema（插件工具会缺描述）: {exc}", file=sys.stderr)


def resolve(key, seen=None):
    """工具集名 → 工具名列表（递归展开 includes）。"""
    seen = seen if seen is not None else set()
    if key in seen:
        return []
    seen.add(key)
    entry = TOOLSETS.get(key)
    if not entry:
        return [f"<插件工具集 {key}: 不在 TOOLSETS，查 plugins/ 里的 register_tool>" ]
    out = list(entry.get("tools") or [])
    for inc in entry.get("includes") or []:
        out += resolve(inc, seen)
    return out


print(f"# profile home = {PROFILE_HOME}")
for plat in platforms:
    toolsets = sorted(_get_platform_tools(cfg, plat))
    names = []
    for t in toolsets:
        names += resolve(t)
    print(f"\n## platform={plat}  工具集 {len(toolsets)} 个 → 工具 {len(names)} 个")
    print("   工具集: " + ", ".join(toolsets))
    for n in names:
        line = (descs.get(n) or "(无 schema: 插件工具或未注册)").splitlines()[0][:120]
        print(f"   - {n}: {line}")
