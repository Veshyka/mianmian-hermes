#!/usr/bin/env bash
# 重放补丁：hindsight 插件 _tool_retain 忘了带 retain_async（本机 bug，2026-10-03 定位）
#
# 症状：hindsight_retain 工具调用返回 "Failed to store memory: "（错误信息为空）。
# 根因：_tool_retain 调 _retain_batch 时**没传 retain_async** → 客户端默认 False → 同步等
#       整篇抽取；本机单批抽取实测 1367s，远超 HINDSIGHT_TIMEOUT=180 → _run_sync 抛裸
#       TimeoutError（无参数 → str(e) 为空串）→ 工具层拼成 "Failed to store memory: "。
# 修法：带上 retain_async=self._retain_async（默认 True），并把返回话说清楚（排队而非已完成）。
#
# 用法（宿主侧执行，文件 root 所有、容器内 hermes 用户写不进去）：
#   SUDO_ASKPASS=/vol1/1000/<USER> sudo -A \
#     docker exec hermes bash /opt/data/scripts/hermes_patch_hindsight_retain_async.sh
#
# 幂等：已是目标状态 → 打印 SKIP_OK；每次改动前把原文件备份到
#       /opt/data/framework-patches/hindsight__init__.py.pre-patch-<ts>
# 容器重建/换镜像后**必须重打**（补丁在可写层，随重建消失，且退化是静默的）。
set -uo pipefail

TARGET=${TARGET:-/opt/hermes/plugins/memory/hindsight/__init__.py}
BK=/opt/data/framework-patches
PY=${PY:-/opt/hermes/.venv/bin/python}
mkdir -p "$BK"

[ -f "$TARGET" ] || { echo "FATAL 找不到 $TARGET"; exit 1; }

"$PY" - "$TARGET" "$BK" <<'PYEOF'
import shutil, sys, time, py_compile, pathlib

target, bk = sys.argv[1], sys.argv[2]
p = pathlib.Path(target)
src = p.read_text(encoding="utf-8")
MARK = "本机补丁（2026-10-03）"
OLD = '''        self._retain_batch(item, bank_id=self._bank_id)
        logger.debug("Tool hindsight_retain: success")
        return "Memory stored successfully."'''
NEW = '''        # ⚠️ 本机补丁（2026-10-03）：必须带上 retain_async，否则客户端默认 False →
        # **同步**等整篇抽取完成；本机抽取单批要 20+ 分钟（实测 1367s），远超
        # HINDSIGHT_TIMEOUT=180 → _run_sync 抛裸 TimeoutError（str(e) 为空）→
        # 工具层拼成 "Failed to store memory: "（当天失败 3 次）。
        # 详情 reports/hindsight-retain-failure-2026-10-03.md；重放脚本
        # scripts/hermes_patch_hindsight_retain_async.sh（重建后要重打）。
        resp = self._retain_batch(item, bank_id=self._bank_id,
                                  retain_async=self._retain_async)
        logger.debug("Tool hindsight_retain: success (async=%s)", self._retain_async)
        if self._retain_async:
            op_id = ""
            try:
                op_id = str(getattr(resp, "operation_id", "") or "")
            except Exception:
                op_id = ""
            return ("Memory accepted and queued for processing"
                    + (f" (operation {op_id})" if op_id else "")
                    + ". Extraction runs in the background; it may take a while to become recall-visible.")
        return "Memory stored successfully."'''

if MARK in src and "retain_async=self._retain_async" in src:
    print("SKIP_OK 已是目标状态（补丁在）")
    sys.exit(0)
if OLD not in src:
    print("FATAL 锚点没命中 —— 上游代码变了，别硬改，先人工比对：%s" % target)
    sys.exit(2)

backup = pathlib.Path(bk) / f"hindsight__init__.py.pre-patch-{time.strftime('%Y%m%d-%H%M%S')}"
shutil.copy2(p, backup)
p.write_text(src.replace(OLD, NEW, 1), encoding="utf-8")
try:
    py_compile.compile(target, doraise=True)
except Exception as e:
    shutil.copy2(backup, p)
    print(f"FATAL 语法检查失败，已回滚：{e}")
    sys.exit(3)
print(f"PATCH_OK 改动 1 处；备份 {backup}")
PYEOF
rc=$?
echo "---- 复核：_tool_retain 现在带了 retain_async 吗 ----"
grep -n -A3 "retain_async=self._retain_async" "$TARGET" | head -8
exit $rc
