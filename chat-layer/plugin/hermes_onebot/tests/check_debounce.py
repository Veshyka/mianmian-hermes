"""入站防抖单测：重置窗口 / 硬上限 / 仅私聊 / 群聊按「会话+发送者」分桶。

⚠️ 文件名用 `check_` 前缀而非 `test_` —— Hermes 的 disk-cleanup 插件会删 `test_*`（详见 check_segmentation.py 头注）。

运行： python3 tests/check_debounce.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from debounce import (  # noqa: E402
    SCOPE_BOTH, SCOPE_PRIVATE, Round, Store, build_key, is_command, merge_texts, wait_window,
)


class TestBuildKey(unittest.TestCase):
    def test_private_key_is_umo(self):
        self.assertEqual(build_key("onebot:dm:10001"), "onebot:dm:10001")

    def test_private_ignores_sender(self):
        self.assertEqual(build_key("onebot:dm:10001", sender_id="10001"), "onebot:dm:10001")

    def test_empty_umo_not_in_scope(self):
        self.assertIsNone(build_key(""))
        self.assertIsNone(build_key(None))

    def test_group_default_scope_is_out_of_range(self):
        """默认「仅私聊」→ 群聊直接不生效（返回 None 表示不做防抖）。"""
        self.assertIsNone(build_key("onebot:group:555:1", group_id="555", sender_id="1",
                                    scope=SCOPE_PRIVATE))

    def test_group_with_both_scope_splits_by_sender(self):
        a = build_key("onebot:group:555", group_id="555", sender_id="1", scope=SCOPE_BOTH)
        b = build_key("onebot:group:555", group_id="555", sender_id="2", scope=SCOPE_BOTH)
        self.assertNotEqual(a, b)          # 不同人的话不混成一轮
        self.assertEqual(a, "onebot:group:555|1")

    def test_group_same_sender_is_stable(self):
        a = build_key("onebot:group:555", group_id="555", sender_id="1", scope=SCOPE_BOTH)
        b = build_key("onebot:group:555", group_id="555", sender_id="1", scope=SCOPE_BOTH)
        self.assertEqual(a, b)

    def test_group_missing_sender_still_buckets(self):
        self.assertEqual(build_key("onebot:group:555", group_id="555", scope=SCOPE_BOTH),
                         "onebot:group:555|unknown")


class TestCommand(unittest.TestCase):
    def test_commands_pass_through(self):
        for t in ("/help", "/new", "\\reset"):
            self.assertTrue(is_command(t))

    def test_wake_prefix(self):
        self.assertTrue(is_command("小棉在吗", wake_prefixes=["小棉"]))

    def test_plain_text_is_debounced(self):
        self.assertFalse(is_command("在吗"))

    def test_empty_is_command(self):
        self.assertTrue(is_command(""))
        self.assertTrue(is_command(None))


class TestMerge(unittest.TestCase):
    def test_merge_drops_blanks(self):
        self.assertEqual(merge_texts(["甲", "", "  ", "乙"]), "甲\n乙")

    def test_merge_single(self):
        self.assertEqual(merge_texts(["只有一句"]), "只有一句")

    def test_merge_strips(self):
        self.assertEqual(merge_texts(["  甲  ", "  乙  "]), "甲\n乙")


class TestRoundTiming(unittest.TestCase):
    def test_quiet_window_resets_on_new_message(self):
        """重置式窗口：每条新消息都把放行时刻往后推。"""
        rnd = Round(wait_seconds=10, max_wait_seconds=45, now=1000.0)
        rnd.add("a", now=1000.0)
        self.assertEqual(rnd.quiet_deadline(), 1010.0)
        rnd.add("b", now=1007.0)
        self.assertEqual(rnd.quiet_deadline(), 1017.0)   # 重置，而不是 1010
        self.assertEqual(rnd.next_wait(1007.0), 10.0)

    def test_hard_cap_wins_over_reset(self):
        """硬上限从本轮第一条算起，连续消息不能把它推后。"""
        rnd = Round(wait_seconds=10, max_wait_seconds=45, now=0.0)
        rnd.add("a", now=0.0)
        for t in (8, 16, 24, 32, 40):
            rnd.add("x", now=float(t))
        self.assertEqual(rnd.hard_deadline(), 45.0)
        self.assertEqual(rnd.next_wait(40.0), 5.0)      # min(quiet=50, hard=45) - 40
        self.assertTrue(rnd.hard_cap_hit(45.0))
        self.assertFalse(rnd.hard_cap_hit(44.9))

    def test_hard_cap_shorter_than_quiet_window(self):
        """硬上限配得比静默窗口还小时，硬上限说了算。"""
        rnd = Round(wait_seconds=10, max_wait_seconds=3, now=0.0)
        rnd.add("a", now=0.0)
        self.assertEqual(rnd.next_wait(0.0), 3.0)

    def test_next_wait_never_negative(self):
        rnd = Round(wait_seconds=10, max_wait_seconds=45, now=0.0)
        rnd.add("a", now=0.0)
        self.assertEqual(rnd.next_wait(999.0), 0.0)


class TestStore(unittest.TestCase):
    def test_first_message_is_leader(self):
        s = Store(10, 45)
        rnd, leader = s.decide("k", "第一条", now=0.0)
        self.assertTrue(leader)
        self.assertEqual(rnd.count, 1)

    def test_followers_join_same_round(self):
        s = Store(10, 45)
        r1, l1 = s.decide("k", "a", now=0.0)
        r2, l2 = s.decide("k", "b", now=1.0)
        r3, l3 = s.decide("k", "c", now=2.0)
        self.assertTrue(l1)
        self.assertFalse(l2)
        self.assertFalse(l3)
        self.assertIs(r1, r2)
        self.assertIs(r2, r3)
        self.assertEqual(r3.count, 3)

    def test_different_keys_are_independent(self):
        s = Store(10, 45)
        a, la = s.decide("k1", "甲的", now=0.0)
        b, lb = s.decide("k2", "乙的", now=0.0)
        self.assertTrue(la)
        self.assertTrue(lb)
        self.assertIsNot(a, b)
        self.assertEqual(s.active_count(), 2)

    def test_close_lets_next_message_start_new_round(self):
        s = Store(10, 45)
        r1, _ = s.decide("k", "a", now=0.0)
        self.assertTrue(s.close("k", r1))
        r2, leader2 = s.decide("k", "b", now=5.0)
        self.assertTrue(leader2)
        self.assertIsNot(r1, r2)

    def test_closed_round_is_not_pending(self):
        s = Store(10, 45)
        r1, _ = s.decide("k", "a", now=0.0)
        s.close("k", r1)
        self.assertIsNone(s.pending("k"))

    def test_stale_close_does_not_remove_newer_round(self):
        s = Store(10, 45)
        r1, _ = s.decide("k", "a", now=0.0)
        s.close("k", r1)
        r2, _ = s.decide("k", "b", now=1.0)
        self.assertFalse(s.close("k", r1))          # 旧轮次不能关掉新一轮
        self.assertIs(s.pending("k"), r2)


class TestWaitWindow(unittest.IsolatedAsyncioTestCase):
    async def test_returns_after_quiet_window(self):
        s = Store(0.2, 5)
        rnd, _ = s.decide("k", "a")
        t0 = time.monotonic()
        elapsed = await wait_window(rnd)
        waited = time.monotonic() - t0
        self.assertGreaterEqual(waited, 0.15)
        self.assertLess(waited, 1.5)
        self.assertGreaterEqual(elapsed, 0.15)

    async def test_new_message_extends_the_wait(self):
        """重置式：窗口期内来新消息 → 总等待明显超过单个静默窗口。"""
        s = Store(0.25, 5)
        rnd, _ = s.decide("k", "a")
        t0 = time.monotonic()
        task = asyncio.create_task(wait_window(rnd))

        async def poke(delay):
            await asyncio.sleep(delay)
            rnd.add("more")

        await asyncio.gather(task, poke(0.15), poke(0.30), poke(0.45))
        waited = time.monotonic() - t0
        self.assertGreater(waited, 0.6)   # 0.45 + 0.25 ≈ 0.7，而不是 0.25
        self.assertEqual(rnd.count, 4)

    async def test_hard_cap_releases_even_while_still_chatting(self):
        """硬上限到点必放行 —— 别无限等（主人明确要求）。"""
        s = Store(10.0, 0.3)   # 静默窗口远大于硬上限
        rnd, _ = s.decide("k", "a")
        task = asyncio.create_task(wait_window(rnd))

        async def chatter():
            for _ in range(20):
                await asyncio.sleep(0.02)
                rnd.add("还在说")

        await asyncio.gather(task, chatter())
        self.assertTrue(rnd.hard_cap_hit())
        self.assertGreater(rnd.count, 1)

    async def test_follower_returns_immediately(self):
        """follower 不阻塞（适配器侧靠这个保证接收循环不被卡住）。"""
        s = Store(0.3, 5)
        rnd, leader = s.decide("k", "a")
        self.assertTrue(leader)
        _, leader2 = s.decide("k", "b")
        self.assertFalse(leader2)


class TestMergedOutput(unittest.TestCase):
    def test_merged_text(self):
        s = Store(10, 45)
        s.decide("k", "第一句", now=0.0)
        s.decide("k", "第二句", now=0.1)
        rnd = s.pending("k")
        self.assertEqual(rnd.merged(), "第一句\n第二句")


if __name__ == "__main__":
    unittest.main(verbosity=2)
