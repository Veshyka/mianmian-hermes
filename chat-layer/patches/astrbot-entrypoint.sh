#!/bin/bash
# AstrBot 容器启动入口（compose entrypoint 覆盖）
#
# 为什么存在：AstrBot 的源码在镜像层 /AstrBot/，只有 /AstrBot/data 是挂载卷 ——
# 容器一旦被「重建」（docker compose up -d --force-recreate / rm+run / 升镜像），
# 源码补丁（见 chat-layer/astrbot-patches.md）就没了，本地工具会静默变回
#   missing argument: 'context' / multiple values for argument 'context'
# 这个入口在 python 起之前把补丁重新打上（幂等：已是目标状态就跳过），
# 于是「重建后自动恢复」不再依赖任何人的记忆。
#
# 启动链：entrypoint(本脚本) → 打补丁 + 自检 → exec 镜像原 CMD（python main.py）
# 只用 stdlib，不需要装任何东西；打补丁失败也照样启动（避免把聊天门弄 down），
# 失败会写进 /AstrBot/data/patches-boot.log，宿主侧可用 patches/apply-from-host.sh 兜底。
set -u

# shellcheck disable=SC2015
PATCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR=/AstrBot/data
[ -d "$LOG_DIR" ] || LOG_DIR=/tmp          # 极端情况（data 卷没挂上）也不至于把日志写炸
LOG="$LOG_DIR/patches-boot.log"
PY="$(command -v python3 || command -v python || echo /usr/local/bin/python)"

{
  echo "=== $(date -Is) astrbot boot: 补丁自检 ==="
  if [ -f "$PATCH_DIR/patch_astrbot_context.py" ]; then
    "$PY" "$PATCH_DIR/patch_astrbot_context.py" 2>&1 || echo "[BOOT] 补丁脚本执行失败（exit $?）"
  else
    echo "[BOOT] 找不到 $PATCH_DIR/patch_astrbot_context.py —— 未打补丁"
  fi
  if [ -f "$PATCH_DIR/verify_patch.py" ]; then
    "$PY" "$PATCH_DIR/verify_patch.py" 2>&1 || echo "[BOOT] !!! 补丁自检未通过，本地工具可能仍不可用 !!!"
  fi
} >>"$LOG" 2>&1

# PATCH_ONLY=1：只打补丁不启动服务（用于「模拟重建」验收 / CI 式检查）
if [ "${PATCH_ONLY:-0}" = "1" ]; then
  cat "$LOG"
  exit 0
fi

if [ "$#" -gt 0 ]; then
  exec "$@"
fi
exec python main.py
