#!/usr/bin/env python3
"""C1 硬前置：**记忆取证实验** —— 群回合到底会不会触发 profile 级 `auto_retain` 写主库？

背景：PLAN-group-chat.md §4.3 / §10 未决项 1。B 阶段「群消息零入库」是**免费**的，因为群消息
压根不到 agent；C1 一开 `group_wake_mode != collect-only`，命中唤醒的群消息就会走一次**正常回合**，
而 `auto_retain` 是 profile 级、无条件化的 → 「默认零入库」不再自动成立。

做法（**不碰网络、不发消息、不写库**）：
  1. 按聊天门 profile（`HERMES_HOME=/opt/data/profiles/chat`）**真实加载**配置里的记忆 provider；
  2. 把 provider 的 `_retain_batch`（真正发往 Hindsight 的那一层）换成捕获器；
  3. 模拟一次**群会话**完成的回合（`sync_turn`），看它有没有派发 retain、派给哪个 bank。

跑法（必须用 Hermes 的解释器）：
    HERMES_HOME=/opt/data/profiles/chat /opt/hermes/.venv/bin/python tests/evidence_group_retain.py
    # 加了 API 的用法（C1 上线后复核「群回合不写主库」）：
    ... evidence_group_retain.py --provider hindsight_guard --expect group:skip dm:write
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, "/opt/hermes")

GROUP_SESSION = "agent:main:onebot:group:<GROUP_ID>:<OWNER_QQ>"
DM_SESSION = "agent:main:onebot:dm:<OWNER_QQ>"
GROUP_TEXT = (
    "[OneBot 群聊回合 · source=qq-group · 群105***32 · 生人档]\n"
    "（下面这段是取证实验造的**假**群消息，不代表任何真实群聊内容）\n"
    "甲: 棉棉在吗\n"
)
DM_TEXT = "（取证实验造的假私聊消息）今天晚饭吃啥"


class _FakeResp:
    """`_retain_batch` 的假返回：只为让调用方继续走，不含任何真实字段。"""


def _install_capture(provider):
    calls = []

    def _fake(item, *, bank_id, document_id=None, retain_async=None):
        calls.append({
            "bank_id": bank_id,
            "document_id": document_id,
            "tags": item.get("tags"),
            "metadata": item.get("metadata"),
            "content_head": str(item.get("content") or "")[:90],
        })
        return _FakeResp()

    provider._retain_batch = _fake
    # 关掉异步路径：本实验要看的是「有没有派发 retain」，不是服务端排队。
    if hasattr(provider, "_retain_async"):
        provider._retain_async = False
    return calls


def _drain(provider, timeout: float = 5.0) -> None:
    q = getattr(provider, "_retain_queue", None)
    if q is None:
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        if getattr(q, "unfinished_tasks", 0) == 0:
            return
        time.sleep(0.05)


def _run_turn(provider, *, text: str, session_id: str, chat_type: str) -> dict:
    calls = _install_capture(provider)
    provider.initialize(session_id=session_id, platform="onebot", chat_type=chat_type,
                        user_id="<OWNER_QQ>", chat_id=session_id.rsplit(":", 1)[-1])
    provider.sync_turn(text, "（她的回复）", session_id=session_id)
    _drain(provider)
    return {"chat_type": chat_type, "session_id": session_id, "retain_calls": calls,
            "retain_count": len(calls)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default=None, help="记忆 provider 名（默认读 config.yaml）")
    ap.add_argument("--expect", default="", help="断言，形如 group:skip dm:write")
    args = ap.parse_args()

    from plugins.memory import load_memory_provider

    home = os.environ.get("HERMES_HOME", "/opt/data")
    name = args.provider
    if not name:
        import yaml  # noqa: PLC0415
        with open(os.path.join(home, "config.yaml"), encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        name = (cfg.get("memory") or {}).get("provider") or ""

    print(f"HERMES_HOME={home}")
    print(f"memory.provider={name}")
    provider = load_memory_provider(name)
    if provider is None:
        print("!! provider 加载失败（名字写错/目录缺失）")
        return 2
    print(f"loaded provider.name={provider.name} name_prop={getattr(provider, 'name', '?')} "
          f"is_available={provider.is_available()} class={type(provider).__name__}")

    group = _run_turn(provider, text=GROUP_TEXT, session_id=GROUP_SESSION, chat_type="group")
    dm = _run_turn(provider, text=DM_TEXT, session_id=DM_SESSION, chat_type="dm")

    print("\n── 群会话回合 ──")
    print(json.dumps(group, ensure_ascii=False, indent=2))
    print("── 私聊会话回合 ──")
    print(json.dumps(dm, ensure_ascii=False, indent=2))

    def _banks(r):
        return sorted({c["bank_id"] for c in r["retain_calls"]})

    print("\n=== 结论 ===")
    print(f"group: retain_count={group['retain_count']} banks={_banks(group)}")
    print(f"dm   : retain_count={dm['retain_count']} banks={_banks(dm)}")

    bad = 0
    for want in (args.expect or "").split():
        target, _, verdict = want.partition(":")
        r = group if target == "group" else dm
        ok = (r["retain_count"] == 0) if verdict == "skip" else (r["retain_count"] > 0)
        print(f"assert {want}: {'OK' if ok else 'FAIL'} (retain_count={r['retain_count']})")
        bad += 0 if ok else 1
    if args.expect:
        print("VERDICT:", "PASS" if bad == 0 else "FAIL")
        return 0 if bad == 0 else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
