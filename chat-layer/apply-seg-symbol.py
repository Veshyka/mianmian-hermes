#!/usr/bin/env python3
"""把 AstrBot 通道层的分段符从 `※`(U+203B) 换成 `⁂`(U+2042)。

背景：`※` 在正常文档正文里也会出现（引用注记），会被 result_decorate 的
`re.findall` 误切成多条。`⁂` 只作分段用，不会自然出现。
容错集合仍是 `⁂` / `※` / `⸮`（见 hermes_onebot/segmentation.py 的 SEP_TOLERATED），
所以模型万一沿用旧话术也不会发出来。

用法（容器内）：python3 /AstrBot/data/_apply_seg_symbol.py [--dry]

改的是 `platform_settings.segmented_reply`：
  regex              `.*?[。？！~…※]+|.+$`  →  `.*?[。？！~…⁂※⸮]+|.+$`
  content_cleanup_rule  `[※]`              →  `[⁂※⸮]`

源码依据：`/AstrBot/astrbot/core/pipeline/result_decorate/stage.py`
  :59-66  从 ctx.astrbot_config["platform_settings"]["segmented_reply"] 读这几个键
  :229-245 re.findall(regex, text, re.DOTALL|re.MULTILINE) 切；再按 content_cleanup_rule 清

备份：/AstrBot/data/_cmd_config_backup_<ts>.json（写库前）
幂等：已是新值就只打印，不动文件。
"""

import json
import shutil
import sys
import time

CFG = "/AstrBot/data/cmd_config.json"

OLD_REGEX = ".*?[。？！~…※]+|.+$"
NEW_REGEX = ".*?[。？！~…⁂※⸮]+|.+$"
OLD_CLEAN = "[※]"
NEW_CLEAN = "[⁂※⸮]"

dry = "--dry" in sys.argv

raw = open(CFG, encoding="utf-8-sig").read()
cfg = json.loads(raw)  # 先证明文件是合法 JSON（不是就抛，不动它）
sr = cfg["platform_settings"]["segmented_reply"]
print("改前：regex=%r  content_cleanup_rule=%r" % (sr.get("regex"), sr.get("content_cleanup_rule")))

changed = []
if sr.get("regex") == OLD_REGEX:
    raw = raw.replace('"regex": "%s"' % OLD_REGEX, '"regex": "%s"' % NEW_REGEX)
    changed.append("regex")
elif sr.get("regex") == NEW_REGEX:
    print("regex 已是新值")
else:
    raise SystemExit("!! regex 不是预期的旧值也不是新值，人工看一下: %r" % sr.get("regex"))

if sr.get("content_cleanup_rule") == OLD_CLEAN:
    raw = raw.replace('"content_cleanup_rule": "%s"' % OLD_CLEAN,
                      '"content_cleanup_rule": "%s"' % NEW_CLEAN)
    changed.append("content_cleanup_rule")
elif sr.get("content_cleanup_rule") == NEW_CLEAN:
    print("content_cleanup_rule 已是新值")
else:
    raise SystemExit("!! cleanup 规则不是预期值，人工看一下: %r" % sr.get("content_cleanup_rule"))

if not changed:
    print("== 没有要改的，退出（幂等）")
    raise SystemExit(0)

print("要改的键:", changed)
if dry:
    print("== DRY RUN，没写文件")
    raise SystemExit(0)

ts = time.strftime("%Y%m%d-%H%M%S")
bak = "/AstrBot/data/_cmd_config_backup_%s.json" % ts
shutil.copy2(CFG, bak)
print("备份:", bak)

with open(CFG, "w", encoding="utf-8") as f:
    f.write(raw)

# 写回校验：重新 parse 并断言两个键
v = json.loads(open(CFG, encoding="utf-8-sig").read())["platform_settings"]["segmented_reply"]
assert v["regex"] == NEW_REGEX, v["regex"]
assert v["content_cleanup_rule"] == NEW_CLEAN, v["content_cleanup_rule"]
print("写回校验 OK：regex=%r cleanup=%r" % (v["regex"], v["content_cleanup_rule"]))
print("其余键未动：enable=%s interval=%s threshold=%s split_mode=%s" % (
    v.get("enable"), v.get("interval"), v.get("words_count_threshold"), v.get("split_mode")))
