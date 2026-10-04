#!/usr/bin/env python3
"""群聊 B 阶段取证脚本（**不连任何服务、不发任何消息**）：把「只看不说」跑给人看。

它干的事：
  1. 用 NapCat 真实推送的**事件形状**（2026-09-23 只读 `get_group_msg_history` 抓的键集）
     造 4 条群消息（纯文本 / 图片 / 表情 / @），喂进 `GroupWindow`；
  2. 打印窗口里实际存了什么（含昵称、时间、`[图片]`/`[表情]` 占位）；
  3. 打印 `render_context()` —— C 阶段要注入给她的那段材料（带 `source=qq-group`）；
  4. 打印双上限生效后的条数/字节；
  5. 断言：窗口模块的代码里**没有任何记忆写入原语**（AST 级）。

用法：`/opt/hermes/.venv/bin/python tests/show_group_evidence.py`（退出码 0 = 全过）
"""

from __future__ import annotations

import ast
import importlib
import os
import sys
import tempfile
from pathlib import Path

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(_PLUGIN_DIR))
_PKG = os.path.basename(_PLUGIN_DIR)
_gw = importlib.import_module(f"{_PKG}.group_window")
_proto = importlib.import_module(f"{_PKG}.onebot_proto")

GID = "1095283483"
MEM_WORDS = ("hindsight", "retain", "recall", "memory", "memori", "embedding")
ok = True


def real_event(*, uid, name, segments, ts):
    """NapCat 4.18.28 实测字段集（值换成占位）。"""
    return {
        "self_id": <BOT_QQ>, "user_id": int(uid), "time": ts, "message_id": ts,
        "message_seq": ts, "real_id": ts, "real_seq": str(ts), "message_type": "group",
        "sender": {"user_id": int(uid), "nickname": name, "card": "", "role": "member"},
        "raw_message": "", "font": 14, "sub_type": "normal", "message": segments,
        "message_format": "array", "post_type": "message", "group_id": int(GID),
        "group_name": "样例群",
    }


def main() -> int:
    global ok
    tmp = tempfile.mkdtemp(prefix="onebot-group-evidence-")
    win = _gw.GroupWindow(Path(tmp) / "onebot-groups", max_msgs=200, max_bytes=256 * 1024)

    t0 = 1758600000
    events = [
        real_event(uid="10001", name="甲", ts=t0,
                   segments=[{"type": "text", "data": {"text": "今晚吃点啥"}}]),
        real_event(uid="20002", name="乙", ts=t0 + 30,
                   segments=[{"type": "text", "data": {"text": "看这个"}},
                             {"type": "image", "data": {"file": "x.jpg"}}]),
        real_event(uid="30003", name="丙", ts=t0 + 60,
                   segments=[{"type": "face", "data": {"id": "14"}}]),
        real_event(uid="10001", name="甲", ts=t0 + 90,
                   segments=[{"type": "at", "data": {"qq": "<BOT_QQ>"}},
                             {"type": "text", "data": {"text": " 棉棉在吗"}}]),
    ]

    print("① 真实事件形状 → 窗口口径文本")
    for evt in events:
        txt = _proto.extract_window_text(evt)
        print(f"   [{evt['sender']['nickname']}] {txt!r}   (投给 LLM 的口径: {_proto.extract_text(evt)!r})")
        win.append(GID, text=txt, uid=str(evt["user_id"]), name=evt["sender"]["nickname"],
                   ts=evt["time"])

    print("\n② 窗口里实际存了什么（每群一份 JSONL）")
    for rec in win.tail(GID):
        print(f"   {rec['t']}  uid={rec['uid']:<6} {rec['name']}: {rec['text']}")
    print(f"   文件: {win.path(GID)}")

    print("\n③ C 阶段要注入给她的上下文材料（render_context）")
    print("   " + "\n   ".join(win.render_context(GID).splitlines()))

    print("\n④ 双上限（此处压到 3 条 / 4KB 演示淘汰）")
    small = _gw.GroupWindow(Path(tmp) / "caps", max_msgs=3, max_bytes=4096)
    for i in range(10):
        small.append("99999", text=f"第{i}条" + "长" * 40, uid="1", name="甲")
    st = small.stats()
    print(f"   灌 10 条（每条 ~50 字）→ 留 {st['msgs']} 条 / {st['bytes']} 字节 / "
          f"淘汰 {st['trimmed']} 条（上限 {st['max_msgs']} 条 / {st['max_bytes']} 字节）")
    print(f"   摘要: {small.summary()}")

    print("\n⑤ 机制证明：窗口/协议模块的**代码**里没有任何记忆写入原语")
    for path in (_gw.__file__, _proto.__file__):
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or "").split(".")[0])
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
        hits = sorted({w for w in MEM_WORDS for n in names if w in n.lower()})
        print(f"   {Path(path).name}: 命中记忆原语 {hits or '无'}")
        ok = ok and not hits

    print("\n=== " + ("PASS" if ok else "FAIL") + " ===")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
