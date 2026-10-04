#!/usr/bin/env bash
# 回滚：停掉并移除聊天层容器。**数据一律保留**（astrbot/ napcat/ ntqq/ stickers/）。
# 现有 Hermes / QQ 官方 bot / 微信通道不受影响 —— 本层是独立新增的。
set -Eeuo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo "== 停止并移除聊天层容器 =="
docker compose down || true

echo "== 结果 =="
docker ps -a --format '{{.Names}}\t{{.Status}}' | grep -E '^(napcat|astrbot)\b' || echo "聊天层容器已全部移除"

echo
echo "数据目录仍在（如需彻底清除请自行确认后再删）："
du -sh astrbot napcat ntqq stickers 2>/dev/null || true
