#!/usr/bin/env python3
"""表情包自动打标 + 出站发图的钉子（2026-10-04）。

主人原话：「入库自动就等着排队识别表情打标签吧，这个让本地就应该能做，这个模型能力应该是够的，
至于发的时候可以随机选，比如开心，就从几个里面随机一个就好了」

钉住的规矩：
  1. 入库只**入队**（不阻塞、不打网络），后台 worker 慢慢消化；
  2. 模型输出先描述再定标签也要能解析；标签必须落在受控词表里（认不出才退化）；
  3. 打标失败不抛、不吞：记账 + 留在「未标」，同一条最多试 max_attempts 次；
  4. 启动时扫一遍库里「未标」的（她偷图时不在场那些）；
  5. 出站 `[表情包:开心]` → 挑一张**带这个标签**的图发出去，标记本身绝不出现在正文里；
  6. `[表情包:/abs/x.gif]` 指定那张；`[表情包]` 随便一张；挑不到时正文照发、不许报错。

⚠️ 文件名用 `check_` 前缀（disk-cleanup 会删 test_*）；只连进程内的假协议端（MockNapCat）。
"""
from __future__ import annotations

import asyncio
import importlib
import os
import pathlib
import sys
import tempfile
import time
import unittest
from typing import Any, Dict, List

_PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PLUGIN_DIR.parent))
sys.path.insert(0, str(_PLUGIN_DIR))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, "/opt/hermes")

_lib = importlib.import_module("onebot.sticker_lib")
_lbl = importlib.import_module("onebot.sticker_labeler")
_proto = importlib.import_module("onebot.onebot_proto")
_ad = importlib.import_module("onebot.adapter")

JPG = bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffdb004300") + b"\x00" * 40 + b"\xff\xd9"


def _seed_sticker(root: pathlib.Path, *, tags: List[str] | None = None,
                  hash_hint: str = "auto") -> tuple[Any, str]:
    """在临时库里放一张真文件 + 一条索引（默认未标）。"""
    lib = _lib.StickerLib(root)
    name = f"{hash_hint}.jpg" if hash_hint != "auto" else "seed.jpg"
    p = root / "files" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(JPG + name.encode())
    r = lib.add(p.read_bytes(), {"sub_type": 1, "summary": "[笑]"}, str(p), tags=tags)
    return lib, r["hash"]


class TestParse(unittest.TestCase):
    def setUp(self):
        self.lab = _lbl.StickerLabeler(_lib.StickerLib(pathlib.Path(tempfile.mkdtemp())), post=lambda *a: "")

    def test_json_tag(self):
        self.assertEqual(self.lab._parse('{"tag":"无语","word":""}'), ["无语"])

    def test_fenced_json_and_extra_prose(self):
        self.assertEqual(self.lab._parse('```json\n{"tag":"笑死","word":"哈哈"}\n```'), ["笑死", "哈哈"])

    def test_tag_with_decoration_picks_vocab_word(self):
        self.assertEqual(self.lab._parse("我觉得是「开心（笑）」")[:1], ["开心"])

    def test_prose_fallback_keeps_short_tag(self):
        self.assertEqual(self.lab._parse("托腮发呆"), ["托腮发呆"])

    def test_garbage_is_empty(self):
        self.assertEqual(self.lab._parse(""), [])
        self.assertEqual(self.lab._parse("   "), [])

    def test_long_prose_is_rejected(self):
        long = "这张图里有一个非常复杂的长句子描述" * 3
        self.assertEqual(self.lab._parse(long), [], "过长的自由文本不许当标签")

    def test_word_field_kept_only_when_short(self):
        self.assertEqual(self.lab._parse('{"tag":"赞","word":"真棒"}'), ["赞", "真棒"])


class TestLabeler(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="sticker-label-"))

    def _lab(self, **kw):
        lib, h = _seed_sticker(self.tmp)
        kw.setdefault("post", lambda url, body, timeout: '{"tag":"无语"}')
        lab = _lbl.StickerLabeler(lib, interval_s=0.0, batch=1, **kw)
        return lab, lib, h

    async def test_label_one_writes_tag_and_hint(self):
        lab, lib, h = self._lab()
        lab.note_new(h, "[笑]")                      # 协议端给的人话线索
        tags = await lab._label_one(h)
        self.assertEqual(tags, ["无语"])
        item = [i for i in lib.load() if i.hash == h][0]
        self.assertIn("无语", item.tags)
        self.assertIn("笑", item.tags, "协议端 summary 的免费线索也要进标签")

    async def test_failure_keeps_untagged_and_counts(self):
        lab, lib, h = self._lab(post=lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
        lab.note_new(h)
        self.assertIsNone(await lab._label_one(h))
        self.assertEqual(lab.failed, 1)
        self.assertLessEqual(lab._attempts[h], 1)
        item = [i for i in lib.load() if i.hash == h][0]
        self.assertEqual(item.tags, [], "打标失败必须留在未标")

    async def test_gives_up_after_max_attempts(self):
        lab, lib, h = self._lab(max_attempts=2, post=lambda *a: "no json here at all")
        lab.note_new(h)
        lab._queue.clear()                           # 模拟 worker 已经取走
        await lab._label_one(h)
        await lab._label_one(h)
        lab.note_new(h)                              # 第三次不该再入队
        self.assertEqual(lab._queue, [], "超过 max_attempts 就不再重排队")

    async def test_empty_model_output_is_a_failure_not_a_tag(self):
        lab, lib, h = self._lab(post=lambda *a: "")
        lab.note_new(h)
        self.assertIsNone(await lab._label_one(h))
        self.assertEqual(lab.last_error, "no_tag")

    async def test_scan_untagged_only_picks_untagged(self):
        lab, lib, h = self._lab()
        lib2, h2 = _seed_sticker(self.tmp, tags=["开心"], hash_hint="already")
        lab.note_new(h)                              # 已有一条在队列里
        n = lab.scan_untagged()
        self.assertEqual(n, 1, "只扫未标的那条（已标的不排）")
        self.assertEqual(len(lab._queue), 1)

    async def test_worker_labels_in_background(self):
        lab, lib, h = self._lab()
        lab.start()
        try:
            lab.note_new(h)
            for _ in range(60):
                if lab.done:
                    break
                await asyncio.sleep(0.05)
            self.assertEqual(lab.done, 1, "worker 没把队列消化掉")
        finally:
            await lab.stop()

    async def test_queue_is_bounded(self):
        lab, lib, h = self._lab(max_queue=1)
        lab.note_new(h)
        lab.note_new("another-hash")
        self.assertEqual(len(lab._queue), 1)
        self.assertEqual(lab.skipped, 1)

    async def test_model_call_does_not_block_the_event_loop(self):
        """打标是阻塞 HTTP —— 必须在别的线程跑，否则她的网关会被卡死（实测抓到的 bug）。"""
        import time as _t

        def slow_post(url, body, timeout):
            _t.sleep(0.3)
            return '{"tag":"无语"}'

        lab, lib, h = self._lab(post=slow_post)
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        t = asyncio.create_task(ticker())
        try:
            await lab._ask(b"x", ".jpg")
        finally:
            t.cancel()
        self.assertGreater(ticks, 5, "事件循环被打标调用堵住了（说明是同步跑的）")

    async def test_disabled_is_a_noop(self):
        lab, lib, h = self._lab(enabled=False)
        lab.start()
        lab.note_new(h)
        self.assertEqual(lab._queue, [])
        self.assertIsNone(lab._task)


class TestProtoMarker(unittest.TestCase):
    def test_tag_form(self):
        clean, wants = _proto.extract_sticker_markers("发个表情 [表情包:开心] 给你")
        self.assertEqual(wants, ["开心"])
        self.assertNotIn("表情包", clean)
        self.assertIn("给你", clean)

    def test_path_form_is_not_treated_as_emotion(self):
        clean, wants = _proto.extract_sticker_markers("[表情包:/opt/data/x/a.jpg]")
        self.assertEqual(wants, ["/opt/data/x/a.jpg"])

    def test_bare_marker(self):
        _, wants = _proto.extract_sticker_markers("[表情包]")
        self.assertEqual(wants, [""])

    def test_multiple(self):
        _, wants = _proto.extract_sticker_markers("[表情包:开心] [表情包:无语]")
        self.assertEqual(wants, ["开心", "无语"])

    def test_no_marker_untouched(self):
        text = "就是普通一句话，没有标记"
        clean, wants = _proto.extract_sticker_markers(text)
        self.assertEqual(clean, text)
        self.assertEqual(wants, [])

    def test_inbound_render_still_intact(self):
        """入站渲染没被动过：路径形式照旧出现在正文里。"""
        m = {"message": [{"type": "image", "data": {"file": "x.jpg", "sub_type": 1, "url": "u"}}]}
        self.assertIn("[表情包", _proto.extract_text(m))


# ── 端到端：真适配器 + 假协议端，看她发出去的是什么 ──────────────────────────
from check_adapter_e2e import AdapterHarness      # noqa: E402
from mock_onebot import MockNapCat                # noqa: E402

OWNER = "10001"


class StickerSendHarness(AdapterHarness):
    async def asyncSetUp(self) -> None:
        self.stickers_dir = pathlib.Path(tempfile.mkdtemp(prefix="sticker-send-"))
        await super().asyncSetUp()
        self.adapter.self_id = "<BOT_QQ>"

    @staticmethod
    def _frames(client: MockNapCat, action: str) -> List[Dict[str, Any]]:
        return [a for a in client.actions if a.get("action") == action]

    @staticmethod
    def _segs(frames: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return (frames[0].get("params") or {}).get("message") or []


class TestStickerSendE2E(StickerSendHarness):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.adapter.stickers_dir = self.stickers_dir
        self.adapter.sticker_lib = _lib.StickerLib(self.stickers_dir)
        self.adapter.sticker_labeler.lib = self.adapter.sticker_lib

    def _add(self, tags: List[str], hash_hint: str) -> str:
        p = self.stickers_dir / f"{hash_hint}.jpg"
        p.write_bytes(JPG + hash_hint.encode())
        r = self.adapter.sticker_lib.add(p.read_bytes(), {"sub_type": 1}, str(p), tags=tags)
        return r["path"]

    async def test_emotion_marker_sends_matching_sticker(self):
        happy = self._add(["开心"], "happy")
        self._add(["无语"], "speechless")
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            res = await self.adapter.send(OWNER, "发你一张 [表情包:开心]")
            await asyncio.sleep(0.1)
            segs = self._segs(self._frames(client, "send_private_msg"))
        self.assertTrue(res.success)
        imgs = [s for s in segs if s["type"] == "image"]
        self.assertEqual(len(imgs), 1, f"该发一张图，实际 {segs}")
        self.assertEqual(imgs[0]["data"]["file"], happy, "发错图了（没按标签挑）")
        said = "".join(s["data"].get("text", "") for s in segs if s["type"] == "text")
        self.assertIn("发你一张", said)
        self.assertNotIn("表情包", said, "标记漏给主人了")

    async def test_unknown_emotion_falls_back_to_random(self):
        p = self._add(["无语"], "only-one")
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await self.adapter.send(OWNER, "[表情包:根本没有这个标签]")
            await asyncio.sleep(0.1)
            segs = self._segs(self._frames(client, "send_private_msg"))
        imgs = [s for s in segs if s["type"] == "image"]
        self.assertEqual(len(imgs), 1, f"挑不到标签也该随机发一张，实际 {segs}")
        self.assertEqual(imgs[0]["data"]["file"], p)

    async def test_explicit_path_wins(self):
        a = self._add(["开心"], "aaa")
        b = self._add(["无语"], "bbb")
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await self.adapter.send(OWNER, f"[表情包:{b}]")
            await asyncio.sleep(0.1)
            segs = self._segs(self._frames(client, "send_private_msg"))
        imgs = [s for s in segs if s["type"] == "image"]
        self.assertEqual([s["data"]["file"] for s in imgs], [b], f"指定路径没生效（实际 {segs}）")

    async def test_empty_library_sends_text_without_error(self):
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            res = await self.adapter.send(OWNER, "库是空的 [表情包:开心]")
            await asyncio.sleep(0.1)
            segs = self._segs(self._frames(client, "send_private_msg"))
        self.assertTrue(res.success, "挑不到图不该让整条消息失败")
        self.assertFalse([s for s in segs if s["type"] == "image"])
        said = "".join(s["data"].get("text", "") for s in segs if s["type"] == "text")
        self.assertIn("库是空的", said)
        self.adapter.sticker_lib  # 占位（保持引用，便于排障）

    async def test_switch_off_leaves_marker_alone(self):
        self.adapter.sticker_send_enabled = False
        self._add(["开心"], "happy2")
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await self.adapter.send(OWNER, "看 [表情包:开心]")
            await asyncio.sleep(0.1)
            segs = self._segs(self._frames(client, "send_private_msg"))
        said = "".join(s["data"].get("text", "") for s in segs if s["type"] == "text")
        self.assertIn("表情包", said, "开关关掉后标记按原文发（便于排障）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
