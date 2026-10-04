"""群聊 C1「@ 必答」的判定层单测（纯函数，不连网、不起 agent）。

守四条线：
  1. 三态语义：`collect-only` 永不唤醒 / `mention-only` 只认三类命中 / `full` 全唤醒
  2. 命中判据：@ 小号（分段 + CQ 串）、回复**她说的**那条、正文点名；@全体、@别人、回复别人 **不算**
  3. 成本闸：每分钟/每小时上限**必须留痕**（不静默丢）
  4. 记忆隔离体检：provider 指向闸门 + 插件在位才 `ok`（缺一就是没就位）
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(_PLUGIN_DIR))
_PKG = os.path.basename(_PLUGIN_DIR)

_gwk = importlib.import_module(f"{_PKG}.group_wake")

SELF = "90001"


def _ev(text: str = "", segs=None, *, mid=1, uid="10001") -> dict:
    if segs is None:
        segs = [{"type": "text", "data": {"text": text}}]
    return {"post_type": "message", "message_type": "group", "group_id": "55555",
            "user_id": uid, "message_id": mid, "message": segs,
            "sender": {"nickname": "甲"}, "time": int(time.time())}


def _at(text: str = "", qq: str = SELF) -> dict:
    return _ev(segs=[{"type": "at", "data": {"qq": qq}},
                     {"type": "text", "data": {"text": f" {text}"}}])


def _decide(ev, *, mode="mention-only", own=(), reply_wakes=True, aliases=None):
    kw = {"alias_names": aliases} if aliases is not None else {}
    return _gwk.decide(ev, self_id=SELF, own_message_ids=own,
                       reply_wakes=reply_wakes, mode=mode, **kw)[0]


class TestModeTriState(unittest.TestCase):
    def test_collect_only_never_wakes(self):
        for ev in (_at("在吗"), _ev("棉棉在吗"), _ev("随便", segs=[{"type": "reply", "data": {"id": 7}}])):
            self.assertEqual(_decide(ev, mode="collect-only", own=["7"]), "none")

    def test_unknown_mode_is_fail_closed(self):
        self.assertEqual(_gwk.parse_mode({"group_wake_mode": "yolo"}), "collect-only")

    def test_full_wakes_everything(self):
        self.assertEqual(_decide(_ev("与唤醒无关的一句"), mode="full"), "full")

    def test_legacy_bool_key_mapping(self):
        self.assertEqual(_gwk.parse_mode({"group_wake_enabled": True}), "full")
        self.assertEqual(_gwk.parse_mode({"group_wake_enabled": False}), "collect-only")
        self.assertEqual(_gwk.parse_mode({}), "collect-only")


class TestMentionTriggers(unittest.TestCase):
    def test_at_self_wakes(self):
        self.assertEqual(_decide(_at("你看看")), "at")

    def test_at_all_does_not_wake(self):
        """@全体成员（qq=all）不许把她叫醒 —— 那不是冲她来的。"""
        self.assertEqual(_decide(_at("通知一下", qq="all")), "none")

    def test_at_someone_else_does_not_wake(self):
        self.assertEqual(_decide(_at("你看看", qq="12345")), "none")

    def test_cq_string_form_wakes(self):
        ev = {"message_type": "group", "group_id": "55555", "user_id": "1",
              "message": f"[CQ:at,qq={SELF}] 在吗", "message_id": 1}
        self.assertEqual(_decide(ev), "at")

    def test_reply_to_her_own_message_wakes(self):
        ev = _ev("接着聊", segs=[{"type": "reply", "data": {"id": 777}},
                                {"type": "text", "data": {"text": "接着聊"}}])
        self.assertEqual(_decide(ev, own=["777"]), "reply")
        self.assertEqual(_decide(ev, own=["888"]), "none", "回复的不是她说的就不算")

    def test_reply_wakes_can_be_switched_off(self):
        ev = _ev(segs=[{"type": "reply", "data": {"id": 777}},
                       {"type": "text", "data": {"text": "嗯"}}])
        self.assertEqual(_decide(ev, own=["777"], reply_wakes=False), "none")

    def test_nickname_wakes(self):
        self.assertEqual(_decide(_ev("棉棉你觉得呢")), "name")
        self.assertEqual(_decide(_ev("小棉~")), "name")

    def test_alias_must_appear_verbatim(self):
        """只认整串（防话痨）：不是「棉」这种单字就点她。"""
        self.assertEqual(_decide(_ev("今天买了棉花糖")), "none",
                         "「棉花」不该被当成点名")
        self.assertEqual(_decide(_ev("棉棉"), aliases=("棉棉说",)), "none")

    def test_priority_at_over_name(self):
        self.assertEqual(_decide(_at("棉棉你看看")), "at")

    def test_empty_message_does_not_wake(self):
        self.assertEqual(_decide(_ev(segs=[{"type": "face", "data": {"id": 1}}])), "none")


class TestWakeLimiter(unittest.TestCase):
    def test_per_minute_cap_leaves_a_trace(self):
        lim = _gwk.WakeLimiter(per_minute=2, per_hour=10)
        self.assertEqual([lim.allow("55555")[0] for _ in range(3)], [True, True, False])
        self.assertEqual(lim.last_reason, "per-minute(2/2)")
        self.assertEqual(lim.limited, 1)

    def test_caps_are_per_group(self):
        lim = _gwk.WakeLimiter(per_minute=1, per_hour=10)
        self.assertTrue(lim.allow("55555")[0])
        self.assertFalse(lim.allow("55555")[0])
        self.assertTrue(lim.allow("66666")[0], "不同群各算各的")

    def test_per_hour_cap(self):
        lim = _gwk.WakeLimiter(per_minute=99, per_hour=2)
        self.assertTrue(lim.allow("55555")[0])
        self.assertTrue(lim.allow("55555")[0])
        self.assertFalse(lim.allow("55555")[0])


class TestTurnText(unittest.TestCase):
    def test_marker_and_window_are_present(self):
        text = _gwk.build_turn_text(
            gid="<GROUP_ID>", trigger="at",
            window_text="【群聊上下文 · source=qq-group】\n[10:00] 甲: 吃了吗")
        self.assertIn("source=qq-group", text)          # 闸门认这个标记
        self.assertIn("触发=被@", text)
        self.assertIn("甲: 吃了吗", text)
        self.assertIn("生人档", text)                    # 场合规矩（不接活/不谈内部）
        self.assertNotIn("<GROUP_ID>", text, "群号要脱敏，别把完整群号塞进上下文")

    def test_marker_constant_matches_guard_config(self):
        self.assertEqual(_gwk.SOURCE_MARKER, "source=qq-group")
        cfg = Path("/opt/data/profiles/chat/plugins/hindsight_guard/config.json")
        if cfg.is_file():
            import json
            self.assertEqual(json.loads(cfg.read_text(encoding="utf-8"))["marker"],
                             _gwk.SOURCE_MARKER,
                             "闸门配置里的 marker 必须与适配器写的标记逐字一致")
            self.assertIn("group", json.loads(cfg.read_text(encoding="utf-8"))["blocked_chat_types"])


class TestMemoryIsolationStatus(unittest.TestCase):
    """记忆隔离体检的判据本身（静态：provider 指向 + 插件在位）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        (self.home / "plugins" / _gwk.GUARD_PROVIDER).mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _write_cfg(self, provider: str):
        (self.home / "config.yaml").write_text(f"memory:\n  provider: {provider}\n",
                                               encoding="utf-8")

    def test_ok_when_provider_is_guard_and_plugin_present(self):
        (self.home / "plugins" / _gwk.GUARD_PROVIDER / "__init__.py").write_text("", encoding="utf-8")
        self._write_cfg(_gwk.GUARD_PROVIDER)
        st = _gwk.memory_isolation_status(self.home)
        self.assertTrue(st["ok"], st)
        self.assertTrue(st["config_ok"])
        self.assertTrue(st["plugin_ok"])

    def test_not_ok_when_provider_is_stock_hindsight(self):
        (self.home / "plugins" / _gwk.GUARD_PROVIDER / "__init__.py").write_text("", encoding="utf-8")
        self._write_cfg("hindsight")
        self.assertFalse(_gwk.memory_isolation_status(self.home)["ok"],
                         "provider 还是原版 → 群回合会写主库 → 不许唤醒")

    def test_not_ok_when_plugin_missing(self):
        self._write_cfg(_gwk.GUARD_PROVIDER)
        self.assertFalse(_gwk.memory_isolation_status(self.home)["ok"])

    def test_missing_config_is_not_ok(self):
        self.assertFalse(_gwk.memory_isolation_status(self.home)["ok"])

    def test_state_evidence_is_optional(self):
        (self.home / "plugins" / _gwk.GUARD_PROVIDER / "__init__.py").write_text("", encoding="utf-8")
        self._write_cfg(_gwk.GUARD_PROVIDER)
        (self.home / "hindsight_guard").mkdir()
        (self.home / "hindsight_guard" / "state.json").write_text(
            '{"enabled": true, "retain_skipped": 3}', encoding="utf-8")
        st = _gwk.memory_isolation_status(self.home)
        self.assertTrue(st["ok"])
        self.assertEqual(st["evidence"]["retain_skipped"], 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
