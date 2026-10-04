#!/usr/bin/env bash
# Hermes 容器「换镜像/重建」之后的**自愈看门狗**（2026-10-04 主人更新镜像前定）
#
# 为什么需要它：可分两类东西活不过重建，且退化是**静默**的 ——
#   ① 镜像层补丁：/opt/hermes/** 在容器镜像层，重建即消失。本机现有补丁
#      `hindsight 插件 _tool_retain 忘了带 retain_async`（症状：hindsight_retain 返回
#      "Failed to store memory: " 空错误）。重放脚本 scripts/hermes_patch_hindsight_retain_async.sh（幂等）。
#   ② 容器可写层设置：本机把容器时区改成了 Asia/Shanghai（/etc/localtime + /etc/timezone），
#      那在可写层，重建回到 UTC → 日志时间戳整体偏 8 小时。
#      （她的时间判断有 onebot_time.py 硬编码北京时间双保险，所以**业务逻辑不受影响**，
#        受影响的是「人看日志」和容器内 date 类脚本。）
#
# 约定（跟其它看门狗一致）：
#   * 健康 → **零输出**（cron 什么都不发）
#   * 真修了东西 / 修不动 → 打一行（deliver=origin 时主人能看到）
#   * 幂等：两个修复动作重复跑都安全；永远以「容器现在的实际状态」为准
set -uo pipefail

HOST=/opt/data/hspr_ssh.sh
ASKPASS_HOST=/vol1/1000/<USER>
PATCH=/opt/data/scripts/hermes_patch_hindsight_retain_async.sh
PLUGIN=/opt/hermes/plugins/memory/hindsight/__init__.py

say() { echo "[hermes-selfheal] $*"; }
host() { timeout 120 "$HOST" "$@" 2>&1 | grep -v '__HERMES_CWD' ; }

fixed=()
failed=()

# ── ① 镜像层补丁：hindsight retain_async ───────────────────────────────────
if grep -q "retain_async=self._retain_async" "$PLUGIN" 2>/dev/null; then
  :                                            # 在位 → 不吭声
else
  out=$(host "export SUDO_ASKPASS=$ASKPASS_HOST; sudo -A docker exec hermes bash $PATCH")
  if echo "$out" | grep -qE "PATCHED|SKIP_OK"; then
    fixed+=("镜像层补丁 hindsight retain_async 已重打（$(echo "$out" | tail -1 | cut -c1-80)）")
  else
    failed+=("镜像层补丁重打失败：$(echo "$out" | tail -2 | tr '\n' ' ' | cut -c1-160)")
  fi
fi

# ── ② 容器时区：必须是 Asia/Shanghai ──────────────────────────────────────
tz_now=$(date +%Z 2>/dev/null || echo "?")
if [ "$tz_now" != "CST" ]; then
  out=$(host "export SUDO_ASKPASS=$ASKPASS_HOST; sudo -A docker exec hermes sh -c 'ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && echo Asia/Shanghai > /etc/timezone && date'")
  if echo "$out" | grep -q "CST"; then
    fixed+=("容器时区已从 $tz_now 修正为 Asia/Shanghai")
  else
    failed+=("容器时区修正失败（现在 $tz_now）：$(echo "$out" | tail -2 | tr '\n' ' ' | cut -c1-160)")
  fi
fi

# ── 汇报 ─────────────────────────────────────────────────────────────────
if [ ${#failed[@]} -gt 0 ]; then
  say "❌ 有项目修不动（需要人看）"
  for f in "${failed[@]}"; do say "  · $f"; done
fi
if [ ${#fixed[@]} -gt 0 ]; then
  say "🔧 换镜像/重建后有东西需要恢复，已自动处理："
  for f in "${fixed[@]}"; do say "  · $f"; done
fi
exit 0
