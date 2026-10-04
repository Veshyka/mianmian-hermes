#!/usr/bin/env bash
# 「聊天门」/ 任意 profile sidecar 启动器模板（宿主上执行）。
# 与主容器同镜像、独立 HERMES_HOME、host 网络、只开 api_server。主容器零改动。
# 关键点：
#   1) 必须传 PUID/PGID = 数据目录属主，否则容器内跑成 uid 10000，读不到属主 1003 的共享文件
#      （auth.json / kanban.db → PermissionError 刷屏、kanban dispatcher 每分钟失败）。
#   2) 不能用 docker run --user <uid> 代替：entrypoint 明确拒绝任意 --user。
#   3) 脚本要同时能在宿主和容器里做前置检查 → 路径根用 ROOT 变量，别写死容器路径。
# 用法： bash chat-door-run.sh [profile名]      # 宿主上
#        HERMES_DATA_ROOT=/opt/data bash chat-door-run.sh --check-only   # 容器内只做检查
set -Eeuo pipefail

P="${1:-chat}"
IMAGE=nousresearch/hermes-agent:latest
NAME="hermes-${P}"
ROOT="${HERMES_DATA_ROOT:-/vol1/1000/<USER>"

[ -f "$ROOT/profiles/$P/config.yaml" ] || { echo "✗ 找不到 $ROOT/profiles/$P/config.yaml（先建 profile：hermes profile create $P + config set platforms.api_server.*）"; exit 1; }

PORT=$(python3 - "$ROOT" "$P" <<'PY'
import re, sys
root, p = sys.argv[1], sys.argv[2]
txt = open(f'{root}/profiles/{p}/config.yaml', encoding='utf-8').read()
m = re.search(r'api_server:\s*\n(?:.*\n)*?\s+port:\s*(\d+)', txt)
print(m.group(1) if m else '8643')
PY
)
echo "profile=$P port=$PORT root=$ROOT"

if [ "${1:-}" = "--check-only" ] || ! command -v docker >/dev/null 2>&1; then
  echo "（只做前置检查，未启动容器）"; exit 0
fi

PUID_="$(stat -c %u "$ROOT")"
PGID_="$(stat -c %g "$ROOT")"
docker rm -f "$NAME" >/dev/null 2>&1 || true

docker run -d --name "$NAME" \
  --network host --restart always \
  -e HERMES_HOME="/opt/data/profiles/$P" \
  -e PUID="$PUID_" -e PGID="$PGID_" \
  -e HERMES_WRITE_SAFE_ROOT=/opt/data \
  -e HERMES_DISABLE_LAZY_INSTALLS=1 \
  -e HERMES_LAZY_INSTALL_TARGET=/opt/data/lazy-packages \
  -e PLAYWRIGHT_BROWSERS_PATH=/opt/hermes/.playwright \
  -v "$ROOT:/opt/data" \
  -v /vol1/1000/<USER> \
  "$IMAGE" hermes gateway run

sleep 20
echo "== 日志尾（应无 PermissionError）=="
docker logs --tail 25 "$NAME" || true
echo
KEY=$(sed -n 's/^API_SERVER_KEY=//p' "$ROOT/profiles/$P/.env" | head -1 | tr -d '\r')
curl -s --noproxy '127.0.0.1' -m 10 -H "Authorization: Bearer $KEY" "http://127.0.0.1:$PORT/v1/models" | head -c 200
