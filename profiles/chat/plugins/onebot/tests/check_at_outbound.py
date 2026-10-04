"""出站 @ 的端到端钉子（2026-10-03）。

背景：群里她说「我这头出站只往群里丢纯文本，段拼不进去」——**这个说法是错的**：
发送链路本来就会把正文里的 `[CQ:at,qq=…]` 走 `onebot_proto.cq_to_segments()` 变成
**真的 at 段**（`build_action` 里就是这么干的，`check_face_outbound.py` 也早钉过私聊那条）。
真正的缺口是另外两个，本文件把它们一起钉住：

  1. **上下文里没有对方的 QQ 号** → 她就算想 @ 也编号码（`group_window.render_context` 补号码）
  2. **没人告诉她写法** → 回合正文里必须教 `[CQ:at,qq=<号>]`（`group_wake.build_turn_text` 加一行）

再加一条线：`at.qq` 按 NapCat schema 必须是**字符串**（数字会被拒，整条发不出去）。

⚠️ 文件名用 `check_` 前缀（disk-cleanup 会删 test_*）。
⚠️ 只连进程内的假协议端（MockNapCat），不碰真 NapCat、不往任何真实群发东西。
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from typing import Any, Dict, List

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PLUGIN_PARENT = os.path.dirname(_PLUGIN_DIR)
_PKG = os.path.basename(_PLUGIN_DIR)
sys.path.insert(0, _PLUGIN_PARENT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from check_adapter_e2e import AdapterHarness  # noqa: E402
from mock_onebot import MockNapCat  # noqa: E402

_proto = __import__(f"{_PKG}.onebot_proto", fromlist=["*"])
_gw = __import__(f"{_PKG}.group_window", fromlist=["*"])
_gwk = __import__(f"{_PKG}.group_wake", fromlist=["*"])

OWNER = "<OWNER_QQ>"      # 主人的 QQ（测试里只当"某个群友"用）


class TestAtSegmentShape(unittest.TestCase):
    """纯函数层：CQ 码 → 段（不连网）。"""

    def test_at_becomes_real_segment_with_string_qq(self):
        segs = _proto.cq_to_segments(f"[CQ:at,qq={OWNER}] 在吗")
        self.assertEqual(segs[0], {"type": "at", "data": {"qq": OWNER}})
        self.assertIsInstance(segs[0]["data"]["qq"], str, "qq 必须是字符串（int 会被协议端拒）")
        self.assertEqual(segs[1]["type"], "text")
        self.assertIn("在吗", segs[1]["data"]["text"])

    def test_plain_text_is_unchanged(self):
        segs = _proto.cq_to_segments("就是普通一句话")
        self.assertEqual(segs, [{"type": "text", "data": {"text": "就是普通一句话"}}])

    def test_unknown_code_with_data_becomes_that_segment_type(self):
        """⚠️ 现状（不改，但要知道）：带参数的未知 CQ 码会**原样变成那个段类型**。

        对协议端的风险与今天那个 `face id=500` 同一类：NapCat 认不出就会回
        `retcode=1200 消息体无法解析`、**整条消息发不出去**。所以回合正文里只教
        她写 `[CQ:at,qq=…]`（唯一用途），别让她自由发挥 CQ 码。
        """
        segs = _proto.cq_to_segments("[CQ:no_such_thing,x=1]")
        self.assertEqual(segs, [{"type": "no_such_thing", "data": {"x": "1"}}])

    def test_bare_unknown_code_degrades_to_text(self):
        """没有参数的认不出码 → 保留成字面文本（不静默吞）。"""
        segs = _proto.cq_to_segments("[CQ:no_such_thing]")
        self.assertEqual(segs, [{"type": "text", "data": {"text": "[CQ:no_such_thing]"}}])


class TestContextGivesHerTheNumbers(unittest.TestCase):
    """她要 @ 人，前提是上下文里能看到号码。"""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.mkdtemp(prefix="onebot-at-")
        self.win = _gw.GroupWindow(self._tmp)

    def test_render_context_carries_qq_id(self):
        self.win.append("55555", text="在吗", uid=OWNER, name="<OWNER_NICK>", ts=1758600000)
        ctx = self.win.render_context("55555")
        self.assertIn(f"<OWNER_NICK>(QQ:{OWNER})", ctx, f"上下文里没有 QQ 号，她没法 @：{ctx}")

    def test_turn_text_teaches_the_convention(self):
        text = _gwk.build_turn_text(gid="55555", trigger="at", window_text="x")
        self.assertIn("[CQ:at,qq=", text, "回合正文没教 @ 的写法")
        # 2026-10-03 主人拍板「完全放开」：@全体 也教，且不再禁止
        self.assertIn("[CQ:at,qq=all]", text, "@全体 的写法没教给她")
        self.assertNotIn("不要 @ 全体", text, "主人已放开 @全体，别再叫住她")


class AtHarness(AdapterHarness):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.adapter.self_id = "<BOT_QQ>"
        self.adapter.group_ids.add("55555")          # 让 _is_group_chat 认得这是群


class TestAtOutboundE2E(AtHarness):
    """端到端：真反向 WS + 假协议端，看她 @ 人时线上到底发出什么。"""

    @staticmethod
    def _frames(client: MockNapCat, action: str) -> List[Dict[str, Any]]:
        return [a for a in client.actions if a.get("action") == action]

    async def test_at_goes_out_as_at_segment_in_group(self):
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            res = await self.adapter.send("55555", f"[CQ:at,qq={OWNER}] 这句是回你的")
            await asyncio.sleep(0.1)
            frames = self._frames(client, "send_group_msg")
        self.assertTrue(res.success)
        self.assertTrue(frames, "群里一条都没发出去")
        segs = frames[0]["params"]["message"]
        ats = [s for s in segs if s["type"] == "at"]
        self.assertEqual(ats, [{"type": "at", "data": {"qq": OWNER}}],
                         f"没发出真的 at 段，实际 {segs}")
        said = "".join(s["data"].get("text", "") for s in segs if s["type"] == "text")
        self.assertIn("这句是回你的", said)
        self.assertNotIn("[CQ:", said, "CQ 码以字面文本漏出去了")

    async def test_at_and_text_order_preserved(self):
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await self.adapter.send("55555", f"先说话[CQ:at,qq={OWNER}]后@人")
            await asyncio.sleep(0.1)
            segs = self._frames(client, "send_group_msg")[0]["params"]["message"]
        self.assertEqual([s["type"] for s in segs], ["text", "at", "text"], f"段顺序变了：{segs}")

    async def test_at_all_goes_out_as_all_segment(self):
        """@全体：主人已放开（2026-10-03），线上就该是 `qq="all"` 的 at 段。"""
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await self.adapter.send("55555", "[CQ:at,qq=all] 说个事")
            await asyncio.sleep(0.1)
            segs = self._frames(client, "send_group_msg")[0]["params"]["message"]
        ats = [s for s in segs if s["type"] == "at"]
        self.assertEqual(ats, [{"type": "at", "data": {"qq": "all"}}], f"实际 {segs}")
        self.assertIsInstance(ats[0]["data"]["qq"], str, "qq 必须是字符串")

    async def test_multiple_ats_in_one_message(self):
        """群里一次 @ 两个人是常见需求（分段用 ⁂，但同一条里也该支持）。"""
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await self.adapter.send("55555", f"[CQ:at,qq={OWNER}] [CQ:at,qq=10001] 你俩看下")
            await asyncio.sleep(0.1)
            segs = self._frames(client, "send_group_msg")[0]["params"]["message"]
        ats = [s["data"]["qq"] for s in segs if s["type"] == "at"]
        self.assertEqual(ats, [OWNER, "10001"])
        self.assertTrue(all(isinstance(q, str) for q in ats))


if __name__ == "__main__":
    unittest.main(verbosity=2)
