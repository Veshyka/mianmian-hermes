#!/bin/bash
# 宿主侧一键补打 AstrBot 补丁（在【宿主】上执行，不依赖容器怎么被重建出来的）
#
# 用途：容器是被 compose 之外的途径重建的（手搓 docker run、导入镜像、别的脚本），
#       compose 的 entrypoint 自愈没生效时用这个兜底；也用于「补丁在、但进程没重启」的现场。
#
# 用法（宿主）：
#   bash /vol1/1000/<USER>          # 容器名默认 astrbot
#   bash .../apply-from-host.sh astrbot --no-restart     # 只打补丁+自检，不重启容器
# 退出码 0 = 补丁已在容器内生效且自检通过。
set -Eeuo pipefail

SRC=/vol1/1000/<USER>
C="${1:-astrbot}"
[[ "${1:-}" == --no-restart ]] && C=astrbot
RESTART=1
[[ "${2:-}" == "--no-restart" ]] && RESTART=0

echo "== 1. 把补丁目录送进容器 =="
docker exec "$C" mkdir -p /opt/astrbot-patches
docker cp "$SRC/." "$C:/opt/astrbot-patches"
docker exec "$C" chmod +x /opt/astrbot-patches/astrbot-entrypoint.sh

echo "== 2. 打补丁（幂等）+ 静态自检 =="
docker exec "$C" bash -c '
  PY=$(command -v python3 || command -v python)
  "$PY" /opt/astrbot-patches/patch_astrbot_context.py
  "$PY" /opt/astrbot-patches/verify_patch.py
'

echo "== 3. 复核容器内目标函数签名 =="
docker exec "$C" bash -c 'PY=$(command -v python3 || command -v python); "$PY" - <<PYEOF
import inspect, sys
sys.path.insert(0, "/AstrBot")
from astrbot.core.provider.func_tool_manager import _PermissionGuardedTool
from astrbot.core.agent.mcp_client import MCPTool
from astrbot.core.astr_agent_tool_exec import call_local_llm_tool
for f in (_PermissionGuardedTool.call, MCPTool.call, call_local_llm_tool):
    sig = inspect.signature(f)
    p = list(sig.parameters.values())[0]
    print(f"{f.__qualname__}: {p.name} kind={p.kind.name}")
    assert p.kind is inspect.Parameter.POSITIONAL_ONLY, "首参不是仅位置参数"
print("签名自检 ✓")
PYEOF'

if [ "$RESTART" = "1" ]; then
  echo "== 4. 重启容器让运行中的进程吃到补丁 =="
  docker restart "$C"
  sleep 12
  docker exec "$C" bash -c 'tail -5 /AstrBot/data/patches-boot.log'
fi
echo "== 完成：补丁在 $C 内已生效 =="
