#!/usr/bin/env bash
# 启动/更新「聊天门」容器：与主容器同镜像，独立 HERMES_HOME，host 网络，只监听 127.0.0.1:<port>。
# 与主容器完全隔离：主容器不受影响，本容器坏掉也只影响聊天门。
# 用法：bash run-chat-door.sh [profile名] （在**宿主**上执行）
set -Eeuo pipefail

P="${1:-chat}"
IMAGE=nousresearch/hermes-agent:latest
NAME=hermes-chat
# 宿主与容器里同一目录的两种可见路径（宿主默认，容器内可覆盖）
ROOT="${HERMES_DATA_ROOT:-/vol1/1000/<USER>"

echo "== 前置：profile 与端口 =="
PORT=$(python3 - "$ROOT" "$P" <<'PY'
import re, sys
root, p = sys.argv[1], sys.argv[2]
txt = open(f'{root}/profiles/{p}/config.yaml', encoding='utf-8').read()
m = re.search(r'api_server:\s*\n(?:.*\n)*?\s+port:\s*(\d+)', txt)
print(m.group(1) if m else '8643')
PY
)
echo "profile=$P port=$PORT"

docker rm -f "$NAME" >/dev/null 2>&1 || true

# 与主容器保持同一套属主：数据目录的 uid/gid（主容器用 PUID/PGID 传入）
PUID_="$(stat -c %u "$ROOT")"
PGID_="$(stat -c %g "$ROOT")"

echo "== 启动容器（PUID=$PUID_ PGID=$PGID_）=="
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
echo "== 日志 =="
docker logs --tail 25 "$NAME" || true
echo
echo "== 探活 =="
KEY=$(sed -n 's/^API_SERVER_KEY=//p' "$ROOT/profiles/$P/.env" | head -1 | tr -d '\r')
curl -s --noproxy '127.0.0.1' -m 10 -H "Authorization: Bearer $KEY" \
  "http://127.0.0.1:$PORT/v1/models" | head -c 200
echo
echo "（聊天门就绪后，AstrBot 插件里 hermes_base_url=http://127.0.0.1:$PORT，hermes_profile 留空）"
