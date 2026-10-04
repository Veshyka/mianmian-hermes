#!/usr/bin/env bash
# 建「聊天门」profile：独立 SOUL / 独立记忆 / 只开 api_server（默认 8643），不接任何 IM 平台。
# 不触碰 default profile，也不触碰正在运行的 gateway。
# 用法：bash setup_chat_profile.sh [profile名] [端口]
set -Eeuo pipefail

H=/opt/hermes/.venv/bin/hermes
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
P="${1:-chat}"
PORT="${2:-8643}"

if $H profile list 2>/dev/null | grep -qw "$P"; then
  echo "== profile $P 已存在：只更新配置 =="
else
  echo "== 新建 profile $P =="
  $H profile create "$P" --description "聊天门：人味/分段/表情包，算力全部走 Hermes 自身 provider"
fi

echo "== 只开 api_server（绑 127.0.0.1:$PORT），不配任何 IM 平台 =="
$H -p "$P" config set platforms.api_server.enabled true
$H -p "$P" config set platforms.api_server.extra.host 127.0.0.1
$H -p "$P" config set platforms.api_server.extra.port "$PORT"

echo "== 从主 profile 复制必要密钥（不打印值） =="
python3 - "$P" <<'PY'
import pathlib, sys
p = sys.argv[1]
main = pathlib.Path('/opt/data/.env').read_text('utf-8')
want = {'API_SERVER_KEY', 'DEEPSEEK_API_KEY', 'DEEPSEEK_BASE_URL'}
lines = [l for l in main.splitlines() if l.split('=', 1)[0].strip() in want]
dst = pathlib.Path(f'/opt/data/profiles/{p}/.env')
txt = dst.read_text('utf-8')
have = {l.split('=', 1)[0].strip() for l in txt.splitlines() if '=' in l}
add = [l for l in lines if l.split('=', 1)[0].strip() not in have]
if add:
    dst.write_text(txt.rstrip() + '\n' + '\n'.join(add) + '\n', 'utf-8')
print(f'  {p}: 新增 {len(add)} 项密钥')
PY

SOUL_SRC="$DIR/chat-profile/SOUL.md"
if [ -f "$SOUL_SRC" ]; then
  cp "$SOUL_SRC" "/opt/data/profiles/$P/SOUL.md"
  echo "== 已装入聊天 SOUL（$SOUL_SRC） =="
else
  echo "!! 未找到 $SOUL_SRC —— 该 profile 的 SOUL.md 仍为空，先补人格文件再上线"
fi

echo
echo "下一步：bash $DIR/run-chat-door.sh $P"
