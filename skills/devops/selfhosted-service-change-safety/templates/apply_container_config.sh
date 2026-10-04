#!/bin/bash
# apply_container_config.sh —— 把新配置文件生效到「配置文件驱动的自建容器服务」
#
# 适用于：容器里某配置文件（settings.yml / config.yaml / *.conf）是挂载进来的，
#          改完要重启生效，且必须能回滚（SearXNG、mihomo、各类自建服务都是这个形状）。
#
# 用法（在宿主上跑）：
#   bash apply_container_config.sh <容器名> <容器内配置路径> <新配置文件> [健康检查URL]
# 例：bash apply_container_config.sh searxng /etc/searxng/settings.yml ./settings.yml.new \
#       "http://127.0.0.1:18888/search?q=test&format=json"
#
# 需要 root 时：把 SUDO 改成 'sudo -A' 并 export SUDO_ASKPASS=<密码脚本>（用 askpass 机制，别把密码写进命令行）
# 幂等：可重复跑，每次都先备份当前版本
set -u

C=${1:?用法: apply_container_config.sh <容器名> <容器内配置路径> <新配置文件> [健康检查URL]}
CPATH=${2:?缺少容器内配置路径}
NEW=${3:?缺少新配置文件}
HEALTH=${4:-}
SUDO=${SUDO:-sudo}
D=$(dirname "$(readlink -f "$NEW")")
STAMP=$(date +%Y%m%d-%H%M%S)
S() { $SUDO "$@"; }

# 健康检查：默认只判「容器起来了」；给了 URL 就判「真能出结果」（健康检查别只看进程）
check() {
  S docker ps --format '{{.Names}}' | grep -qx "$C" || return 1
  [ -z "$HEALTH" ] && return 0
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 10 "$HEALTH" 2>/dev/null)
  [ "$code" = "200" ]
}

wait_healthy() {
  for i in $(seq 1 20); do
    check && { echo "  第 $i 次检查: 正常 ✅"; return 0; }
    echo "  第 $i 次检查: 未就绪"; sleep 2
  done
  return 1
}

echo "=== 0. 前置检查 ==="
S docker ps --format '{{.Names}}' | grep -qx "$C" || { echo "❌ 容器 $C 不在运行"; exit 1; }
[ -f "$NEW" ] || { echo "❌ 找不到 $NEW"; exit 1; }

# 1. 定位挂载源（容器内路径往往只是挂载点——docker cp 覆盖会 device or resource busy）
echo "=== 1. 定位配置真身（挂载源）==="
SRC=$(S docker inspect "$C" --format "{{range .Mounts}}{{if eq .Destination \"$CPATH\"}}{{.Source}}{{end}}{{end}}")
if [ -z "$SRC" ] || [ ! -f "$SRC" ]; then
  echo "  ⚠️ 未找到挂载源（可能不是 bind mount，而是容器可写层里的文件）"
  echo "     那就改用 docker cp $NEW $C:$CPATH —— 但先确认重建容器不会把它冲掉"
  exit 1
fi
echo "  挂载源: $SRC"
OWNER=$(stat -c '%U:%G' "$SRC"); MODE=$(stat -c '%a' "$SRC")
echo "  原属主/权限: $OWNER $MODE"

# 2. 备份真身
echo "=== 2. 备份 ==="
BAK="$D/$(basename "$SRC").bak-$STAMP"
S cp "$SRC" "$BAK" && echo "  ✅ $BAK ($(stat -c%s "$BAK") 字节)"

# 3. 先校验临时副本，别先覆盖生产
echo "=== 3. 校验新配置（临时副本）==="
if S docker cp "$NEW" "$C:/tmp/.newcfg" >/dev/null 2>&1; then
  for PY in /usr/local/searxng/.venv/bin/python python3 python; do
    OUT=$(S docker exec "$C" sh -lc "command -v $PY >/dev/null 2>&1 && $PY -c 'import yaml,sys;d=yaml.safe_load(open(\"/tmp/.newcfg\"));print(\"YAML OK\",type(d).__name__)'" 2>&1)
    echo "$OUT" | grep -q "YAML OK" && { echo "  ✅ $OUT"; break; }
  done
  echo "$OUT" | grep -q "YAML OK" || echo "  ⚠️ 容器内没验成（非 YAML 或没 python）——自己想办法验，别无校验就上生产"
else
  echo "  ⚠️ 拷临时副本失败，跳过校验"
fi

# 4. 写挂载源 + 还原属主权限（别改既有目录的属主）
echo "=== 4. 写入 + 还原属主 ==="
S cp "$NEW" "$SRC" && echo "  ✅ 已写挂载源"
S chown "$OWNER" "$SRC" && S chmod "$MODE" "$SRC" && echo "  ✅ 属主/权限还原为 $OWNER $MODE"

# 5. 重启 + 健康检查
echo "=== 5. 重启 + 健康检查 ==="
S docker restart "$C" >/dev/null && echo "  ✅ 已重启"
if wait_healthy; then
  echo "✅ 生效完成。回滚：$SUDO cp $BAK $SRC && $SUDO docker restart $C"
  exit 0
fi

# 6. 失败自动回滚
echo "❌ 健康检查失败 → 自动回滚"
S cp "$BAK" "$SRC" && S chown "$OWNER" "$SRC" && S chmod "$MODE" "$SRC"
S docker restart "$C" >/dev/null
wait_healthy && { echo "✅ 已回滚并恢复"; exit 1; }
echo "⚠️ 回滚后仍未就绪 → 看 docker logs $C --tail 50"; exit 1
