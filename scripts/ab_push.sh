#!/bin/bash
# ab_push.sh <本地文件> <容器内目标路径>
# 把本地文件推进 astrbot 容器（走宿主 SSH + docker exec -i，无需共享挂载）。
set -euo pipefail
SRC="$1"
DST="$2"
B64=$(base64 -w0 "$SRC")
cd "$(dirname "$0")"
bash fygo_ssh.sh "export SUDO_ASKPASS=/tmp/mian_sudo.sh; echo '$B64' | sudo -A docker exec -i astrbot sh -c 'base64 -d > $DST && chmod 644 $DST && wc -c $DST'"
