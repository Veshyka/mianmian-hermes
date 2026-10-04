#!/usr/bin/env bash
# 跑 hermes_onebot 的全部单测（不需要 pytest，用 stdlib unittest）。
# ⚠️ 文件前缀是 check_ 而非 test_ —— Hermes 自带 disk-cleanup 插件会删 test_*（见 check_segmentation.py 头注）。
# 必须带上 /opt/hermes 才能 import gateway.*（适配器基类所在）。
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
export PYTHONPATH="/opt/hermes:$(dirname "${HERE}"):${PYTHONPATH:-}"
# 必须用 Hermes 自带的解释器：系统 python3 没有 yaml/aiohttp →
# check_adapter_e2e / check_live_gateway 会 import 失败（表现为 2 个 ERROR，看起来像真故障）。
PY="${HERMES_PYTHON:-/opt/hermes/.venv/bin/python}"
[ -x "$PY" ] || PY=python3
exec "$PY" -m unittest discover -s "${HERE}" -t "${HERE}" -p 'check_*.py' "$@"
