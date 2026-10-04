#!/usr/bin/env python3
"""自动引用的钉子（2026-10-04）。

主人原话：「为啥每一次他发消息都会引用我第一条信息？其实，如果没必要引用，那就别引用啊。
感觉每一条都引用怪怪的。」

背景：引用段来自 Hermes 的**通用机制** —— 它把「这条响应对应哪条入站消息」当 `reply_to`
传下来（`gateway/stream_consumer.py` 的 `initial_reply_to_id` = 触发那条消息的 id）。
私聊里「触发那条」= 他这一轮的第一条消息（防抖把几条并成一轮）→ 于是每条回复都挂着引用。

钉住的规矩：
  1. 私聊：默认**不**自动引用（谁传下来的 reply_to 一律丢掉）
  2. 群聊：默认保留（引用有上下文价值），`auto_quote_group: false` 可关
  3. 显式引用永远有效 —— 她自己在正文写 `[CQ:reply,id=…]` 照样引用（不经过这个闸）
  4. 丢的只是「引用段」，正文照发（不许因为不引用就把消息吞了）

⚠️ 文件名用 `check_` 前缀（disk-cleanup 会删 test_*）；只连进程内的假协议端（MockNapCat）。
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from typing import Any, Dict, List

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(_PLUGIN_DIR))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/opt/hermes")

from check_adapter_e2e import AdapterHarness      # noqa: E402
from mock_onebot import MockNapCat                # noqa: E402

OWNER = "10001"      # 夹具里 allowlist 内的私聊对象
GROUP = "55555"      # 夹具里当群用的号


class QuoteHarness(AdapterHarness):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.adapter.self_id = "<BOT_QQ>"
        self.adapter.group_ids.add(GROUP)

    @staticmethod
    def _frames(client: MockNapCat, action: str) -> List[Dict[str, Any]]:
        return [a for a in client.actions if a.get("action") == action]

    @staticmethod
    def _segs(frames: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return (frames[0].get("params") or {}).get("message") or []


def _reply(segs: List[Dict[str, Any]]):
    return next((s for s in segs if s.get("type") == "reply"), None)


def _text(segs: List[Dict[str, Any]]) -> str:
    return "".join(s["data"].get("text", "") for s in segs if s.get("type") == "text")


class TestDmNeverAutoQuotes(QuoteHarness):
    async def test_dm_drops_auto_quote(self):
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            res = await self.adapter.send(OWNER, "在呢", reply_to="777")
            await asyncio.sleep(0.1)
            frames = self._frames(client, "send_private_msg")
        self.assertTrue(res.success)
        self.assertTrue(frames, "私聊一条都没发出去")
        segs = self._segs(frames)
        self.assertIsNone(_reply(segs), f"私聊不该自动引用，实际 {segs}")
        self.assertEqual(_text(segs), "在呢", "正文照发（丢的只是引用段）")

    async def test_dm_without_reply_to_is_unchanged(self):
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await self.adapter.send(OWNER, "嗯")
            await asyncio.sleep(0.1)
            segs = self._segs(self._frames(client, "send_private_msg"))
        self.assertIsNone(_reply(segs))
        self.assertEqual(_text(segs), "嗯")

    async def test_explicit_cq_reply_still_works_in_dm(self):
        """她真想引用哪条就自己写 CQ 码 —— 这条路不经过自动引用闸。"""
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await self.adapter.send(OWNER, "[CQ:reply,id=999]这条我回一下", reply_to="777")
            await asyncio.sleep(0.1)
            segs = self._segs(self._frames(client, "send_private_msg"))
        seg = _reply(segs)
        self.assertIsNotNone(seg, f"显式引用必须有效，实际 {segs}")
        self.assertEqual(seg["data"]["id"], "999")
        self.assertIn("这条我回一下", _text(segs))
        self.assertNotIn("[CQ:", _text(segs), "CQ 码以字面文本漏出去了")


class TestGroupKeepsAutoQuote(QuoteHarness):
    async def test_group_still_quotes(self):
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await self.adapter.send(GROUP, "收到", reply_to="888")
            await asyncio.sleep(0.1)
            segs = self._segs(self._frames(client, "send_group_msg"))
        seg = _reply(segs)
        self.assertIsNotNone(seg, f"群聊默认该保留引用，实际 {segs}")
        self.assertEqual(seg["data"]["id"], "888")
        self.assertIn("收到", _text(segs))


class TestSwitchesCanFlip(QuoteHarness):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.adapter.auto_quote_dm = True
        self.adapter.auto_quote_group = False

    async def test_dm_switch_on(self):
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await self.adapter.send(OWNER, "在呢", reply_to="777")
            await asyncio.sleep(0.1)
            segs = self._segs(self._frames(client, "send_private_msg"))
        self.assertIsNotNone(_reply(segs), "auto_quote_dm=true 时私聊该引用")

    async def test_group_switch_off(self):
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await self.adapter.send(GROUP, "收到", reply_to="888")
            await asyncio.sleep(0.1)
            segs = self._segs(self._frames(client, "send_group_msg"))
        self.assertIsNone(_reply(segs), "auto_quote_group=false 时群聊不该引用")


class TestGatePure(QuoteHarness):
    async def test_defaults(self):
        self.assertIsNone(self.adapter._auto_quote(is_group=False, reply_to="1"))
        self.assertEqual(self.adapter._auto_quote(is_group=True, reply_to="1"), "1")
        self.assertIsNone(self.adapter._auto_quote(is_group=True, reply_to=None))
        self.assertIsNone(self.adapter._auto_quote(is_group=False, reply_to=None))

    async def test_config_defaults_are_the_documented_ones(self):
        self.assertFalse(self.adapter.auto_quote_dm, "私聊默认必须是关的（主人要求）")
        self.assertTrue(self.adapter.auto_quote_group)


if __name__ == "__main__":
    unittest.main(verbosity=2)
