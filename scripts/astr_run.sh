#!/bin/bash
# 在 astrbot 容器内执行本机 py 脚本（脚本先写到 stack/astrbot/data/mianmian-tmp/）
# 用法: ./astr_run.sh <脚本名> [参数...]
set -euo pipefail
export SSH_ASKPASS=/opt/data/scripts/askpass.sh SSH_ASKPASS_REQUIRE=force
S="$1"; shift || true
HOSTDIR=/vol1/1000/<USER>
setsid ssh -o StrictHostKeyChecking=no 棉棉@172.17.0.1 \
  "export SUDO_ASKPASS=/vol1/1000/<USER> \
   sudo -A docker exec -w /AstrBot -e PYTHONPATH=/AstrBot astrbot python3 /AstrBot/data/mianmian-tmp/$S $*" 2>&1 | grep -v 'Could not chdir to home'
