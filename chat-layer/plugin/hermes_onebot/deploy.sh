#!/usr/bin/env bash
# 把 hermes_onebot 插件（源码真身）同步到聊天门 profile 的插件目录。
#
# 为什么要同步：Hermes 的用户插件目录是 **$HERMES_HOME/plugins/**，
# 聊天门跑的是 `hermes -p chat`（HERMES_HOME=/opt/data/profiles/chat），
# 所以它的插件目录是 /opt/data/profiles/chat/plugins/ —— 不是 /opt/data/plugins/。
#   证据：hermes -p chat config path -> /opt/data/profiles/chat/config.yaml
#         源码 plugins/plugin_loader.py:30-37 user_plugins_dir() = get_hermes_home()/"plugins"
#
# 用法（容器内或宿主上，路径视图不同）：
#   bash deploy.sh                    # 默认目标 = /opt/data/profiles/chat/plugins/onebot
#   HERMES_DATA_ROOT=/vol1/1000/<USER> bash deploy.sh    # 宿主视图
#
# 幂等；改完源码重跑即可。自愈 = 重跑本脚本。
set -Eeuo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="${HERMES_DATA_ROOT:-/opt/data}"
PROFILE="${HERMES_PROFILE:-chat}"
DST="${ROOT}/profiles/${PROFILE}/plugins/onebot"

echo "== 源: ${HERE}"
echo "== 目标: ${DST}"
[ -f "${HERE}/plugin.yaml" ] || { echo "!! 源目录不像插件（缺 plugin.yaml）"; exit 1; }

mkdir -p "${DST}/tests"
cp -a "${HERE}"/plugin.yaml "${HERE}"/__init__.py "${HERE}"/adapter.py \
      "${HERE}"/segmentation.py "${HERE}"/debounce.py "${HERE}"/onebot_proto.py \
      "${HERE}"/group_window.py "${HERE}"/group_wake.py "${HERE}"/health.py \
      "${HERE}"/doctor.py "${HERE}"/trust.py "${HERE}"/group_admin.py \
      "${HERE}"/sticker_lib.py "${DST}"/
# 2026-10-03：补上 sticker_lib.py（表情包库：入库/限量/挑图/打标 CLI）。
# 它被 adapter.py 直接 import，漏了会让表情包入库整条静默失效。
# 2026-10-03：补上 group_admin.py —— 它原先漏在这个清单外（线上有、源码没有，
# 跑一次 deploy.sh 就会把群管理打成"模块缺失→安全停用"）。源码真身已与线上逐文件对齐
# （cmp 全同），所以这个脚本现在可以安全重跑。
cp -a "${HERE}"/tests/mock_onebot.py "${HERE}"/tests/check_*.py "${HERE}"/tests/run_tests.sh "${DST}"/tests/

# C1：记忆隔离闸门（hindsight_guard）—— **profile 级 provider，源码真身在本目录的兄弟位置**
# （plugin/hindsight_guard/）。少了它、而 config 的 memory.provider 又指着它 → provider 加载失败
# = 私聊记忆一起停，所以这里一并同步（幂等）。
GUARD_SRC="${HERE%/hermes_onebot}/hindsight_guard"
GUARD_DST="${ROOT}/profiles/${PROFILE}/plugins/hindsight_guard"
if [ -d "${GUARD_SRC}" ]; then
  mkdir -p "${GUARD_DST}"
  cp -a "${GUARD_SRC}"/__init__.py "${GUARD_SRC}"/plugin.yaml "${GUARD_SRC}"/config.json "${GUARD_DST}"/
  echo "== 已同步记忆隔离闸门 → ${GUARD_DST}"
else
  echo "!! 找不到 ${GUARD_SRC}（闸门源码真身）—— 跳过同步；若 config 指向 hindsight_guard 会加载失败"
fi

echo "== 已同步的文件 =="
find "${DST}" -type f | sort
echo
echo "== 提醒 =="
echo "  1) 本脚本**不会**启用插件，也不会启用平台 —— 启用是主人拍板的事。"
echo "  2) 启用分两步（都要写 profile 的 config.yaml）："
echo "       plugins.enabled 加上 'onebot'        # 才轮到 register(ctx) 被调用"
echo "       platforms.onebot.enabled = true      # 才会真正起反向 WS 监听"
echo "  3) 平台级安全默认 read_only=true（只收不发），要真发再显式关。"
