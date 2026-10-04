#!/usr/bin/env bash
# 回退「PLAN v3 第一步」对 chat profile 的改动（工具面收窄 + SOUL 改稿）。
#
#   默认 dry-run：只打印将要做什么。
#   真执行：      bash rollback-chat-agent.sh --yes
#
# 只碰 chat profile。不碰 AstrBot / NapCat / 干活门(default)。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP="$HERE/backup"
CONFIG_BAK="$BACKUP/chat-config.yaml.bak-before-narrow-20260923-060059"
SOUL_BAK="$BACKUP/chat-SOUL.md.bak-before-conv-20260923-060059"
PROFILE_DIR="/opt/data/profiles/chat"
S6="/package/admin/s6/command/s6-svc"
SLOT="/run/service/gateway-chat"

DRY=1
[ "${1:-}" = "--yes" ] && DRY=0

run () {
  if [ "$DRY" = "1" ]; then echo "  [dry-run] $*"; else echo "  + $*"; eval "$@"; fi
}

echo "== 检查备份在位 =="
for f in "$CONFIG_BAK" "$SOUL_BAK"; do
  if [ -f "$f" ]; then echo "  ok  $f"; else echo "  !!  缺 $f —— 停手，别猜"; exit 1; fi
done

echo "== 1) 工具面：加回 delegation =="
run "export PATH=/opt/hermes/bin:\$PATH; hermes -p chat config set platform_toolsets.api_server '[\"a2a\",\"delegation\",\"file\",\"memory\",\"vision\",\"web\"]' --force"

echo "== 2) 人设：还原 SOUL.md =="
run "cp '$SOUL_BAK' '$PROFILE_DIR/SOUL.md'"

echo "== 3) 重启聊天门（不承载在线会话；-r 对已 down 的服务无效，故带 -u 兜底）=="
run "$S6 -r $SLOT"
run "sleep 20"
run "$S6 -u $SLOT"
run "$S6 -svstat $SLOT"

echo "== 4) 复核 =="
run "export PATH=/opt/hermes/bin:\$PATH; hermes -p chat config get platform_toolsets"
echo "   期望：api_server 含 delegation；svstat 显示 up；8643 返回 401（活着、需鉴权）"

echo
if [ "$DRY" = "1" ]; then echo "（dry-run 结束。真执行加 --yes）"; else echo "回退完成。"; fi
