#!/usr/bin/env python3
"""分段行为取证（只打印，不落状态）：规矩只剩一条 —— 出现 `⁂` 就切，没有就整段发。

不是单测（文件名不带 `check_`，不会被 discover 收走；那个前缀是给 Hermes 的 disk-cleanup
让路的，见 check_segmentation.py 头注）。单测版本见 `check_segmentation.py`，本脚本是给人看的证据。

运行： /opt/hermes/.venv/bin/python tests/show_segmentation_evidence.py   # 退出码 0 = 全 PASS
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

from segmentation import SEP, split_text, scrub  # noqa: E402

# 样本只有一份 —— 直接取单测里那份被断言过的真实长/短内容，免得两边漂移。
from check_segmentation import TestRealContentBehaviour as _Real  # noqa: E402

LONG_NO_SEP = _Real.LONG_CHECKLIST                      # 1000+ 字，不含 ⁂
SHORT_TURNS = _Real.SHORT_TURNS                         # 3 句短对话，含 ⁂
ENGLISH_HEAVY = SEP.join([_Real.ENGLISH_BLOCK] * 6)     # 1000+ 字、英文占比高，含 ⁂


def show(title: str, text: str) -> list:
    segs = split_text(text)
    print(f"\n===== {title} =====")
    print(f"输入长度        : {len(text)} 字；含 ⁂ 个数: {text.count(SEP)}")
    print(f"切成几条        : {len(segs)}")
    print(f"输出含 ⁂ 个数   : {sum(s.count(SEP) for s in segs)}")
    for i, s in enumerate(segs[:4], 1):
        print(f"  [{i}] len={len(s)}  {s[:56]}{'…' if len(s) > 56 else ''}")
    if len(segs) > 4:
        print(f"  …（共 {len(segs)} 条）")
    return segs


if __name__ == "__main__":
    short_segs = show("① 短对话（3 句，含 ⁂）", SHORT_TURNS)
    en_segs = show("② 英文占比高的 1000+ 字（含 ⁂）", ENGLISH_HEAVY)
    long_segs = show("③ 1000+ 字长内容（不含 ⁂）", LONG_NO_SEP)
    print("\n===== ④ 异常/降级路径（scrub 兜底）=====")
    dirty = "甲⁂乙※丙⸮丁"
    print(f"  输入 {dirty!r} → scrub → {scrub(dirty)!r}")
    print(f"  整条只有分隔符 '⁂⁂⁂' → split_text → {split_text('⁂⁂⁂')!r}（不发，也不回显符号）")

    checks = [
        ("① 短对话切成多条（3 条）", len(short_segs) == 3),
        ("① 短对话输出无 ⁂", all(SEP not in s for s in short_segs)),
        ("② 英文重的 1000+ 字照切（>1 条）", len(en_segs) > 1 and len(ENGLISH_HEAVY) > 1000),
        ("② 长英文输出无 ⁂", all(SEP not in s for s in en_segs)),
        ("③ 不含 ⁂ 的 1000+ 字一条不切", len(long_segs) == 1 and len(LONG_NO_SEP) > 1000),
        ("④ scrub 摘干净容错符号", scrub(dirty) == "甲乙丙丁"),
        ("④ 只有分隔符 → 空（不回显）", split_text("⁂⁂⁂") == []),
    ]
    print("\n===== 判定 =====")
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    sys.exit(0 if all(ok for _, ok in checks) else 1)
