#!/usr/bin/env python3
"""上线补投的钉子（2026-10-04）。

钉住的规矩（主人原话）：
  1. 不在线期间的消息**补**给她，不管私聊还是群聊
  2. 上限「最近 5 条」（更早的不补，且要说清楚没补）
  3. 有一句「你刚上线」的抬头
  4. 私聊合成**一个**回合（不是 N 个）
  5. 已经收到过的消息不重复补（游标）
  6. 协议端问不到历史 → 什么也不做，绝不抛
  7. 群里漏掉的最后一条若能叫醒她（@/点名/过闸）→ 一上线就叫她
"""
from __future__ import annotations

import asyncio
import importlib
import json
import pathlib
import sys
import tempfile
import time
import unittest

_PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PLUGIN_DIR.parent))
sys.path.insert(0, str(_PLUGIN_DIR))
sys.path.insert(0, "/opt/hermes")

_adapter = importlib.import_module("onebot.adapter")
_proto = importlib.import_module("onebot.onebot_proto")
_gw = importlib.import_module("onebot.group_window")
_gwk = importlib.import_module("onebot.group_wake")

NOW = 1791090000.0        # 2026-10-04 13:00 附近（北京）


def _hist_msg(*, uid, nick, text, t, group=None, at=None, mtype="text"):
    """历史 API 返回的一条消息（形状与事件一致，实测如此）。"""
    segs = []
    if at:
        segs.append({"type": "at", "data": {"qq": str(at)}})
    if text:
        segs.append({"type": "text", "data": {"text": text}})
    m = {"post_type": "message", "user_id": uid, "time": t, "message_id": int(t),
         "self_id": <BOT_QQ>, "sender": {"nickname": nick, "user_id": uid},
         "message": segs, "raw_message": text}
    if group:
        m.update({"message_type": "group", "group_id": group})
    return m


class _BackfillAdapter(_adapter.OneBotAdapter):
    """只装出补投需要的那点东西；历史用假 `_call_action` 喂。"""

    def __init__(self, *, tmp, dm_hist=None, group_hist=None, cursor=None, fail=False,
                 fail_first=0):
        self.self_id = "<BOT_QQ>"
        self.offline_backfill_enabled = True
        self.offline_backfill_max = 5
        self.offline_backfill_delay_s = 0.0
        self.offline_backfill_retry_wait = 0.0
        self.offline_backfill_save_every = 0.0    # 测试里每次都写，方便断言
        self._backfill_dirty = False
        self._backfill_saved_at = 0.0
        self.backfill_path = pathlib.Path(tmp) / "onebot-backfill.json"
        self._last_seen = dict(cursor or {})
        self._backfill_running = False
        self._backfill_injected = 0
        self._backfill_skipped = 0
        self._calls = []
        self._dm_hist = dm_hist or []
        self._group_hist = group_hist or []
        self._fail = fail
        self._fail_first = int(fail_first or 0)
        self.dispatched = []
        self.render_calls = []
        # 群采集/唤醒判定要的状态
        self.group_enabled = True
        self.group_collect_enabled = True
        self.group_wake_mode = "gated"
        self.group_memory_guard_required = False
        self.group_alias_names = ()
        self.group_reply_to_her_wakes = True
        self.group_gate_threshold = 0.35
        self.group_ids = set()
        self._group_gate_state = {}
        self._group_own_mids = {}
        self._group_rx_count = 0
        self._group_wake_limited = 0
        self._group_wake_blocked = 0
        self._group_wake_count = 0
        self._gate_wake_count = 0
        self._group_mention_count = 0
        self._group_reply_count = 0
        self._group_name_count = 0
        self._group_full_count = 0
        self._group_llm_calls = 0
        self._group_last_trigger = ""
        self._group_wake_limiter = _gwk.WakeLimiter(per_minute=99, per_hour=999)
        self.group_window = _gw.GroupWindow(pathlib.Path(tmp) / "groups.jsonl")
        self.group_admin = None
        self.history_limit = 30

    async def _call_action(self, payload, timeout=30.0):  # type: ignore[override]
        self._calls.append(payload)
        if self._fail:
            return {"status": "failed", "retcode": 1}
        if self._fail_first and len(self._calls) <= self._fail_first:   # 前 N 次失败（模拟热身）
            return {"status": "failed", "retcode": 1200, "message": "无法获取用户信息"}
        action = payload.get("action")
        msgs = self._dm_hist if action == "get_friend_msg_history" else self._group_hist
        return {"status": "ok", "retcode": 0, "data": {"messages": msgs}}

    async def _dispatch(self, text, event, *, is_group, gid, uid, trigger="",
                        internal=False):  # type: ignore[override]
        self.dispatched.append({"text": text, "is_group": is_group, "gid": gid,
                                "uid": uid, "trigger": trigger, "internal": internal})

    async def _voice_texts(self, event):  # 语音段：测试里直接给「转写结果」
        if getattr(self, "voice_ok", True) and _proto.record_segments(event):
            return ["这是转写出来的话"]
        return []

    async def _text_with_media(self, event):     # 接收侧链路替身：正文 + 落盘路径
        self.render_calls.append(("dm", event.get("message_id")))
        text = _proto.with_voice_texts(_proto.extract_text(event), await self._voice_texts(event))
        if any(seg.get("type") in ("image", "mface") for seg in event.get("message") or []):
            text = text.replace("[表情]", "[表情包:/tmp/lib/x.jpg]").replace("[图片]", "[图片:/tmp/m/x.jpg]")
        return text

    async def _group_text_with_media(self, event):   # 群窗口口径同理
        self.render_calls.append(("group", event.get("message_id")))
        return await self._text_with_media(event)


class BackfillTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.tmp = tempfile.mkdtemp(prefix="backfill-test-")

    def _ad(self, **kw):
        return _BackfillAdapter(tmp=self.tmp, **kw)

    # ── 私聊 ────────────────────────────────────────────────────────────────
    async def test_dm_backfill_is_one_turn_with_last_five(self):
        hist = [_hist_msg(uid=<OWNER_QQ>, nick="<OWNER_NICK>", text=f"第{i}条",
                          t=NOW - 6000 + i * 60) for i in range(1, 13)]      # 12 条
        ad = self._ad(dm_hist=hist, cursor={"dm:<OWNER_QQ>": NOW - 7000})
        await ad._online_backfill()
        self.assertEqual(len(ad.dispatched), 1, "私聊补投必须是**一个**回合")
        got = ad.dispatched[0]
        self.assertFalse(got["is_group"])
        self.assertIn("你刚上线", got["text"])
        self.assertIn("只给你最近 5 条", got["text"])
        for i in range(8, 13):                                              # 最近 5 条在
            self.assertIn(f"第{i}条", got["text"])
        for i in range(1, 8):                                              # 更早的不在
            self.assertNotIn(f"第{i}条", got["text"])

    async def test_dm_cursor_prevents_repeats(self):
        hist = [_hist_msg(uid=<OWNER_QQ>, nick="<OWNER_NICK>", text="新的", t=NOW - 60),
                _hist_msg(uid=<OWNER_QQ>, nick="<OWNER_NICK>", text="旧的", t=NOW - 3600)]
        ad = self._ad(dm_hist=hist, cursor={"dm:<OWNER_QQ>": NOW - 600})
        await ad._online_backfill()
        self.assertEqual(len(ad.dispatched), 1)
        self.assertIn("新的", ad.dispatched[0]["text"])
        self.assertNotIn("旧的", ad.dispatched[0]["text"])
        # 游标推进后再跑一次 → 不重复补
        ad.dispatched.clear()
        await ad._online_backfill()
        self.assertEqual(ad.dispatched, [])
        self.assertEqual(ad._backfill_skipped, 1)

    async def test_her_own_messages_are_not_backfilled(self):
        hist = [_hist_msg(uid=<BOT_QQ>, nick="[我]", text="她自己说的话", t=NOW - 60),
                _hist_msg(uid=<OWNER_QQ>, nick="<OWNER_NICK>", text="他的话", t=NOW - 50)]
        ad = self._ad(dm_hist=hist, cursor={"dm:<OWNER_QQ>": NOW - 600})
        await ad._online_backfill()
        self.assertEqual(len(ad.dispatched), 1)
        self.assertNotIn("她自己说的话", ad.dispatched[0]["text"])

    # ── 群聊 ────────────────────────────────────────────────────────────────
    async def test_group_backfill_goes_into_window_with_marker(self):
        hist = [_hist_msg(uid=<OWNER_QQ>, nick="<OWNER_NICK>", text=f"群消息{i}", t=NOW - 6000 + i * 60,
                          group=<GROUP_ID2>) for i in range(1, 9)]
        ad = self._ad(group_hist=hist, cursor={"group:<GROUP_ID2>": NOW - 7000})
        await ad._online_backfill()
        recs = ad.group_window.tail("<GROUP_ID2>", limit=50)
        texts = [r["text"] for r in recs]
        self.assertTrue(any("你刚上线" in t for t in texts), "窗口里要有一句「你刚上线」")
        for i in range(4, 9):
            self.assertTrue(any(f"群消息{i}" in t for t in texts), f"群消息{i} 该补进窗口")
        self.assertFalse(any("群消息1" in t for t in texts), "超过上限的更早消息不补")
        self.assertEqual(len(texts), 6, "5 条消息 + 1 行说明")

    async def test_group_at_on_missed_message_wakes_her(self):
        hist = [_hist_msg(uid=<OWNER_QQ>, nick="<OWNER_NICK>", text="在吗", t=NOW - 120,
                          group=<GROUP_ID2>, at=<BOT_QQ>)]
        ad = self._ad(group_hist=hist, cursor={"group:<GROUP_ID2>": NOW - 600})
        await ad._online_backfill()
        self.assertEqual(len(ad.dispatched), 1, "漏掉的 @ 要一上线就叫她")
        self.assertTrue(ad.dispatched[0]["is_group"])
        self.assertEqual(ad.dispatched[0]["trigger"], "at")

    async def test_group_plain_message_does_not_wake(self):
        hist = [_hist_msg(uid=<OWNER_QQ>, nick="<OWNER_NICK>", text="嗯", t=NOW - 120,
                          group=<GROUP_ID2>)]
        ad = self._ad(group_hist=hist, cursor={"group:<GROUP_ID2>": NOW - 600})
        await ad._online_backfill()
        self.assertEqual(ad.dispatched, [], "平淡的群消息只进窗口，不花钱")

    async def test_history_voice_is_transcribed_too(self):
        """补投回来的语音不能是 "[语音]" 空壳（刚修好的坑不许在新的路上重犯）。"""
        hist = [_hist_msg(uid=<OWNER_QQ>, nick="<OWNER_NICK>", text=None, t=NOW - 60),
                _hist_msg(uid=<OWNER_QQ>, nick="<OWNER_NICK>", text="文字的消息", t=NOW - 50)]
        hist[0]["message"] = [{"type": "record", "data": {"file": "v.amr"}}]
        ad = self._ad(dm_hist=hist, cursor={"dm:<OWNER_QQ>": NOW - 600})
        await ad._online_backfill()
        self.assertEqual(len(ad.dispatched), 1)
        self.assertIn("这是转写出来的话", ad.dispatched[0]["text"])
        self.assertNotIn("[语音]", ad.dispatched[0]["text"])

    async def test_history_voice_transcription_failure_keeps_placeholder(self):
        hist = [_hist_msg(uid=<OWNER_QQ>, nick="<OWNER_NICK>", text=None, t=NOW - 60)]
        hist[0]["message"] = [{"type": "record", "data": {"file": "v.amr"}}]
        ad = self._ad(dm_hist=hist, cursor={"dm:<OWNER_QQ>": NOW - 600})
        ad.voice_ok = False
        await ad._online_backfill()          # 不许抛
        self.assertEqual(len(ad.dispatched), 1)
        self.assertIn("[语音]", ad.dispatched[0]["text"])

    async def test_history_sticker_keeps_its_path(self):
        """补回来的表情包要带本地路径（光秃秃的 [表情包] 她看不见图）。"""
        hist = [_hist_msg(uid=<OWNER_QQ>, nick="<OWNER_NICK>", text=None, t=NOW - 60)]
        hist[0]["message"] = [{"type": "mface", "data": {"summary": "[笑]"}}]
        ad = self._ad(dm_hist=hist, cursor={"dm:<OWNER_QQ>": NOW - 600})
        await ad._online_backfill()
        self.assertEqual(len(ad.dispatched), 1)
        self.assertIn("/tmp/lib/x.jpg", ad.dispatched[0]["text"], "表情包没带路径")
        self.assertIn(("dm", hist[0]["message_id"]), ad.render_calls, "没走接收侧渲染链路")

    async def test_group_history_uses_window_rendering(self):
        hist = [_hist_msg(uid=<OWNER_QQ>, nick="<OWNER_NICK>", text="群里的话", t=NOW - 60,
                          group=<GROUP_ID2>)]
        ad = self._ad(group_hist=hist, cursor={"group:<GROUP_ID2>": NOW - 600})
        await ad._online_backfill()
        self.assertTrue(any(k == "group" for k, _ in ad.render_calls), "群补投该走窗口口径渲染")

    async def test_cursor_is_saved_without_waiting_for_backfill(self):
        """游标要即时落盘：重启正好发生在「刚处理完一批」之后（实测踩过重复补）。"""
        ad = self._ad()
        ad._mark_seen(gid="", uid="<OWNER_QQ>", ts=NOW)
        self.assertTrue(ad._backfill_dirty or ad.backfill_path.exists())
        ad._flush_backfill_cursor()
        saved = json.loads(ad.backfill_path.read_text(encoding="utf-8"))
        self.assertAlmostEqual(saved["dm:<OWNER_QQ>"], NOW, delta=1)

    async def test_group_window_ts_is_a_floor(self):
        """窗口里已经有更新的消息 → 就算游标很旧也不重复补（双保险）。"""
        ad = self._ad()
        ad.group_window.append("<GROUP_ID2>", text="窗口里已有的", uid="<OWNER_QQ>",
                               name="<OWNER_NICK>", ts=NOW - 30)
        hist = [_hist_msg(uid=<OWNER_QQ>, nick="<OWNER_NICK>", text="历史里的", t=NOW - 60,
                          group=<GROUP_ID2>)]
        ad._group_hist = hist
        ad._last_seen = {"group:<GROUP_ID2>": NOW - 600}
        await ad._backfill_group("<GROUP_ID2>")
        texts = [r["text"] for r in ad.group_window.tail("<GROUP_ID2>", limit=50)]
        self.assertFalse(any("历史里的" in t for t in texts), "窗口已有的时间点之前的消息不该再补")

    async def test_backfill_turn_is_machinery_so_silence_is_allowed(self):
        """补投回合里她可以 [SILENT]（否则 Hermes 判「该回不回」→ 弹 ⚠️ 给主人）。"""
        hist = [_hist_msg(uid=<OWNER_QQ>, nick="<OWNER_NICK>", text="早看过的", t=NOW - 60)]
        ad = self._ad(dm_hist=hist, cursor={"dm:<OWNER_QQ>": NOW - 600})
        await ad._online_backfill()
        self.assertEqual(len(ad.dispatched), 1)
        self.assertTrue(ad.dispatched[0]["internal"],
                        "补投回合必须标记 internal=True（机器回合才允许她装死）")

    # ── 兜底 ────────────────────────────────────────────────────────────────
    async def test_history_api_failure_is_silent(self):
        ad = self._ad(fail=True, cursor={"dm:<OWNER_QQ>": NOW - 600,
                                         "group:<GROUP_ID2>": NOW - 600})
        await ad._online_backfill()          # 不许抛
        self.assertEqual(ad.dispatched, [])

    async def test_history_retries_while_protocol_warmup(self):
        """刚上线那几秒协议端还没热身（1200）→ 要退避重试，不能白丢整批补投。"""
        hist = [_hist_msg(uid=<OWNER_QQ>, nick="<OWNER_NICK>", text="睡醒了没", t=NOW - 30)]
        ad = self._ad(dm_hist=hist, cursor={"dm:<OWNER_QQ>": NOW - 600}, fail_first=2)
        await ad._online_backfill()
        self.assertEqual(len(ad.dispatched), 1, "重试成功后必须补上")
        self.assertIn("睡醒了没", ad.dispatched[0]["text"])
        self.assertEqual(len(ad._calls), 3, "两次失败 + 一次成功")

    async def test_no_known_chats_is_noop(self):
        ad = self._ad()
        await ad._online_backfill()
        self.assertEqual(ad._calls, [], "没有已知会话就不该去问历史")

    async def test_cursor_persists_to_disk(self):
        ad = self._ad(dm_hist=[_hist_msg(uid=<OWNER_QQ>, nick="<OWNER_NICK>", text="在", t=NOW - 10)],
                      cursor={"dm:<OWNER_QQ>": NOW - 600})
        await ad._online_backfill()
        saved = json.loads(ad.backfill_path.read_text(encoding="utf-8"))
        self.assertIn("dm:<OWNER_QQ>", saved)
        self.assertAlmostEqual(saved["dm:<OWNER_QQ>"], NOW - 10, delta=1)
        # 新实例读回来（重启不重复补）
        ad2 = self._ad(dm_hist=[_hist_msg(uid=<OWNER_QQ>, nick="<OWNER_NICK>", text="在", t=NOW - 10)])
        ad2._load_backfill_cursor()
        self.assertAlmostEqual(ad2._last_seen["dm:<OWNER_QQ>"], NOW - 10, delta=1)

    async def test_mark_seen_moves_forward_only(self):
        ad = self._ad()
        ad._mark_seen(gid="", uid="<OWNER_QQ>", ts=NOW)
        ad._mark_seen(gid="", uid="<OWNER_QQ>", ts=NOW - 9999)
        self.assertAlmostEqual(ad._last_seen["dm:<OWNER_QQ>"], NOW, delta=0.001)
        ad._mark_seen(gid="<GROUP_ID>", uid="<OWNER_QQ>", ts=NOW + 5)
        self.assertAlmostEqual(ad._last_seen["group:<GROUP_ID>"], NOW + 5, delta=0.001)


if __name__ == "__main__":
    unittest.main(verbosity=2)
