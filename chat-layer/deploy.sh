#!/usr/bin/env bash
# 部署聊天层（NapCat + AstrBot）。在**宿主**上执行。
# 关键：只新增容器，不动 hermes / 其它任何现有服务。
set -Eeuo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo "== 0. 前置检查 =="
docker compose version >/dev/null || { echo "缺 docker compose"; exit 1; }
mkdir -p astrbot/data napcat/config ntqq stickers

echo "== 1. 拉镜像（走宿主代理） =="
docker pull soulter/astrbot:latest
docker pull mlikiowa/napcat-docker:latest

echo "== 2. 起容器 =="
NAPCAT_UID="${NAPCAT_UID:-$(id -u)}" NAPCAT_GID="${NAPCAT_GID:-$(id -g)}" \
  docker compose up -d
sleep 8

echo "== 3. 状态 =="
docker compose ps
echo
echo "接下来（手动，一次性）："
echo "  1) 打开 http://<宿主IP>:6099 进 NapCat WebUI → 扫码登录小号 QQ"
echo "  2) NapCat 里配「反向 WebSocket」→ ws://127.0.0.1:6199/ws  （token 与 AstrBot 侧一致）"
echo "  3) http://<宿主IP>:6185 进 AstrBot WebUI → 设强口令"
echo "  4) AstrBot: 平台=aiocqhttp / 插件 hermes_forward 填 hermes_api_key（Hermes 的 API_SERVER_KEY）"
echo "  5) AstrBot: 全局关掉自带 AI —— provider_settings.enable=false"
echo "     并把「分段回复 → 仅对 LLM 结果分段」设为**关闭**，否则插件回复不会被分段"
