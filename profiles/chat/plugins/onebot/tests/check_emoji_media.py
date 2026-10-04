"""第二批交付（2026-09-23）的三件事，逐条可复现验证。

覆盖：
  1. 入站表情**带 id**：`extract_text` 把 face/mface/marketface 渲染成 `[表情:14]` /
     `[表情包:4321/9988]` / `[超级表情:abc]`，而不是无信息的 `[表情]`。
  2. 出站**贴表情回应**：`adapter.emoji_like()` → 真的在 WS 上发出
     `{"action":"set_msg_emoji_like","params":{message_id,emoji_id,set}}`。
  3. 入站**图片落盘**：图片段的 `get_image` 响应（base64）被落成本地文件，
     正文占位变成 `[图片:<绝对路径>]`，供 vision 读取。

⚠️ 文件名用 `check_` 前缀（disk-cleanup 会删 test_*，见 check_segmentation.py 头注）。
⚠️ **全程只连自己进程内的假协议端**（MockNapCatMedia）—— 不碰 NapCat / 小号 / 主人会话。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PLUGIN_PARENT = os.path.dirname(_PLUGIN_DIR)
_PKG = os.path.basename(_PLUGIN_DIR)
sys.path.insert(0, _PLUGIN_PARENT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import aiohttp  # noqa: E402

import mock_onebot as _mock  # noqa: E402
from check_adapter_e2e import AdapterHarness  # noqa: E402

_proto = __import__(f"{_PKG}.onebot_proto", fromlist=["*"])

#: 1×1 的合法 PNG —— 落盘后能被任何图片库读懂，用它证明「真的是张图」
PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAE"
           "hQGAhKmMIQAAAABJRU5ErkJggg==")
PNG = base64.b64decode(PNG_B64)


def _evt(segments: List[Dict[str, Any]], message_type: str = "group") -> Dict[str, Any]:
    evt: Dict[str, Any] = {
        "post_type": "message", "message_type": message_type, "sub_type": "normal",
        "self_id": <BOT_QQ>, "user_id": <OWNER_QQ>, "message_id": 4242,
        "time": int(time.time()),
        "sender": {"user_id": <OWNER_QQ>, "nickname": "主人", "card": ""},
        "message": segments, "raw_message": "",
    }
    if message_type == "group":
        evt["group_id"] = 55555
    return evt


class MockNapCatMedia(_mock.MockNapCat):
    """MockNapCat + 可定制 action 响应。

    父类对**所有** action 都只回 `{"data":{"message_id":N}}`；但 `get_image` 必须回
    base64 才能验证落盘通路，所以这里加一个 `_reply_for` 钩子。
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.image_calls: List[Dict[str, Any]] = []

    async def _recv_loop(self) -> None:
        try:
            async for msg in self._ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                self.raw_frames.append(msg.data)
                try:
                    frame = json.loads(msg.data)
                except ValueError:
                    continue
                if not (isinstance(frame, dict) and "action" in frame):
                    continue
                self.actions.append({"t": time.monotonic(), **frame})
                await self._ws.send_str(json.dumps(self._reply_for(frame)))
        except (asyncio.CancelledError, Exception):
            pass

    def _reply_for(self, frame: Dict[str, Any]) -> Dict[str, Any]:
        echo = frame.get("echo")
        if frame.get("action") == "get_image":
            self.image_calls.append(dict(frame.get("params") or {}))
            return {"status": "ok", "retcode": 0,
                    "data": {"file": "", "url": "http://127.0.0.1:9/unreachable",
                             "base64": PNG_B64, "file_size": len(PNG)},
                    "echo": echo}
        return {"status": "ok", "retcode": 0,
                "data": {"message_id": 1000 + len(self.actions)}, "echo": echo}

    async def push_with_segments(self, segments: List[Dict[str, Any]], *,
                                 message_type: str = "private", user_id: str = "10001",
                                 group_id: str = "55555", message_id: int = 1) -> None:
        evt = _evt(segments, message_type=message_type)
        evt["user_id"] = int(user_id)
        evt["sender"] = {"user_id": int(user_id), "nickname": "主人", "card": ""}
        evt["message_id"] = message_id
        if message_type == "group":
            evt["group_id"] = int(group_id)
        else:
            evt.pop("group_id", None)
        await self.push(evt)


# ── 1. 纯函数：表情带 id ────────────────────────────────────────────────────


#: 主人 2026-10-03 发的那张真实大表情的**原始报文**（一字不改，摘自 NapCat 的
#: get_friend_msg_history 返回）。报告里有完整 JSON：reports/onebot-marketface-roundtrip-2026-10-03.md
BIG_FACE_SEGMENT = {
    "type": "face",
    "data": {
        "id": "500",
        "raw": {
            "faceIndex": 500, "faceText": "/秋秋赏月", "faceType": 3, "packId": "1",
            "stickerId": "102", "sourceType": 1, "stickerType": 3, "resultId": "0",
            "surpriseId": "", "randomType": 0, "chainCount": 1,
        },
        "resultId": "0",
        "chainCount": 1,
    },
}


class TestFaceIdRendering(unittest.TestCase):
    def test_face_has_id(self):
        """要求 ①：face 段渲染成 `[表情:<id>]`，不再是裸 `[表情]`。"""
        evt = _evt([{"type": "face", "data": {"id": "14"}}])
        self.assertEqual(_proto.extract_text(evt), "[表情:14]")
        self.assertEqual(_proto.extract_window_text(evt), "[表情:14]")

    def test_face_without_id_still_not_empty(self):
        """没有 id 时退回无 id 占位 —— 但**绝不返回空串**。"""
        evt = _evt([{"type": "face", "data": {}}])
        self.assertEqual(_proto.extract_text(evt), "[表情]")

    def test_mface_carries_package_and_emoji_id(self):
        evt = _evt([{"type": "mface", "data": {"emoji_package_id": 4321, "emoji_id": "9988"}}])
        self.assertEqual(_proto.extract_text(evt), "[表情包:4321/9988]")

    def test_marketface_prefers_id_over_summary(self):
        evt = _evt([{"type": "marketface", "data": {"emoji_id": "abc", "summary": "[小丑]"}}])
        self.assertEqual(_proto.extract_text(evt), "[超级表情:abc]")

    def test_marketface_summary_fallback_strips_brackets(self):
        evt = _evt([{"type": "marketface", "data": {"summary": "[小丑]"}}])
        self.assertEqual(_proto.extract_text(evt), "[超级表情:小丑]")

    def test_mixed_text_and_faces(self):
        evt = _evt([
            {"type": "text", "data": {"text": "哈哈"}},
            {"type": "face", "data": {"id": "14"}},
            {"type": "text", "data": {"text": "笑死"}},
            {"type": "face", "data": {"id": "9"}},
        ])
        self.assertEqual(_proto.extract_text(evt), "哈哈[表情:14]笑死[表情:9]")

    def test_big_face_carries_name(self):
        """★ 2026-10-03 新增：大表情（QQ 内置超表情）带名字，方便区分不同的那几张。

        真实入站报文（抓自 NapCat，主人发的 faceIndex=500）：
        ``{"type":"face","data":{"id":"500","raw":{"faceType":3,"packId":"1",
        "stickerId":"102","faceText":"/秋秋赏月",…},"resultId":"0","chainCount":1}}``
        """
        evt = _evt([BIG_FACE_SEGMENT])
        self.assertEqual(_proto.extract_text(evt), "[表情:500 秋秋赏月]")

    def test_big_face_without_name_falls_back_to_id_only(self):
        """raw 里没有 faceText → 退回 `[表情:500]`（不留半截占位）。"""
        seg = {"type": "face", "data": {"id": "500",
                                        "raw": {"faceType": 3, "packId": "1", "stickerId": "102"}}}
        self.assertEqual(_proto.extract_text(_evt([seg])), "[表情:500]")

    def test_native_face_rendering_unchanged(self):
        """兼容钉子：原生小黄脸**渲染一字不变**（她已按这个写规矩了）。"""
        for fid in ("0", "14", "326"):
            self.assertEqual(_proto.extract_text(_evt([{"type": "face", "data": {"id": fid}}])),
                             f"[表情:{fid}]")

    def test_two_different_big_faces_are_distinguishable(self):
        """验收项：两张不同的大表情必须能区分（判据 = faceText，兜底 stickerId）。"""
        other = {"type": "face", "data": {"id": "500",
                                          "raw": {"faceType": 3, "packId": "1", "stickerId": "103",
                                                  "faceText": "/别的东西"}}}
        a = _proto.extract_text(_evt([BIG_FACE_SEGMENT]))
        b = _proto.extract_text(_evt([other]))
        self.assertNotEqual(a, b)
        self.assertIn("秋秋赏月", a)
        self.assertIn("别的东西", b)

    def test_pure_face_message_not_ignored(self):
        """纯表情私聊不再被判 `no_text`。"""
        evt = _evt([{"type": "face", "data": {"id": "14"}}], message_type="private")
        self.assertIsNone(_proto.should_ignore(evt))


# ── 2. 纯函数：图片段抽取 + 占位回填 ────────────────────────────────────────


class TestImageSegmentsAndPaths(unittest.TestCase):
    def test_image_segments_from_array(self):
        evt = _evt([
            {"type": "text", "data": {"text": "看图"}},
            {"type": "image", "data": {"file": "a.jpg", "url": "https://x/a.jpg"}},
            {"type": "face", "data": {"id": "1"}},
            {"type": "image", "data": {"file": "b.png"}},
        ])
        segs = _proto.image_segments(evt)
        self.assertEqual([s.get("file") for s in segs], ["a.jpg", "b.png"])

    def test_image_segments_from_cq_string(self):
        evt = {"message": "看图[CQ:image,file=abc.image,url=https://x/y.png]好"}
        segs = _proto.image_segments(evt)
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0]["file"], "abc.image")

    def test_with_media_paths_substitutes_in_order(self):
        text = "第一张[图片]第二张[图片]"
        out = _proto.with_media_paths(text, ["/tmp/1.jpg", "/tmp/2.png"])
        self.assertEqual(out, "第一张[图片:/tmp/1.jpg]第二张[图片:/tmp/2.png]")

    def test_with_media_paths_keeps_placeholder_on_failure(self):
        """落盘失败（None）时**保留占位** —— 少一个会让模型以为那条没有图。"""
        out = _proto.with_media_paths("[图片][图片]", [None, "/tmp/2.png"])
        self.assertEqual(out, "[图片][图片:/tmp/2.png]")

    def test_with_media_paths_noop_when_no_images(self):
        self.assertEqual(_proto.with_media_paths("没有图", []), "没有图")


# ── 3. 纯函数：贴表情 action 构造 ───────────────────────────────────────────


class TestEmojiLikeFrame(unittest.TestCase):
    def test_frame_shape_and_numeric_normalisation(self):
        frame = _proto.build_emoji_like_action("12345", "66", self_id="<BOT_QQ>")
        self.assertEqual(frame["action"], "set_msg_emoji_like")
        # id 统一归一成数字（本机实测数字/字符串都被 NapCat 接受，归一只为与其它 action 一致）
        self.assertEqual(frame["params"], {
            "message_id": 12345, "emoji_id": 66, "set": True, "self_id": <BOT_QQ>})

    def test_set_false_is_unset(self):
        frame = _proto.build_emoji_like_action(1, 2, set_=False)
        self.assertIs(frame["params"]["set"], False)

    def test_non_numeric_id_kept_as_string(self):
        frame = _proto.build_emoji_like_action("abc", "def")
        self.assertEqual(frame["params"]["message_id"], "abc")
        self.assertEqual(frame["params"]["emoji_id"], "def")


# ── 4. 端到端：真跑一遍反向 WS ──────────────────────────────────────────────


class MediaHarness(AdapterHarness):
    """复用 check_adapter_e2e 的适配器脚手架；只把 media_dir 指到临时目录。"""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        # 默认 media_dir 是 profile 真实路径 —— 单测绝不许往那儿写
        self.adapter.media_dir = Path(self._tmp) / "onebot-media"
        self.adapter.media_download_enabled = True
        self.adapter.media_max_bytes = 1024 * 1024
        # 生产里由 ONEBOT_SELF_ID 注入；单测显式给上，贴表情帧才会带 self_id
        self.adapter.self_id = "<BOT_QQ>"
        # 群开关默认 false（见 check_adapter_e2e.py:175）—— 要验群采集就得显式开
        self.adapter.group_enabled = True


class TestEmojiLikeOutbound(MediaHarness):
    """要求 ②：`set_msg_emoji_like` 真跑一次成功。"""

    async def test_emoji_like_sends_correct_frame_and_succeeds(self):
        async with MockNapCatMedia(self.url, token=\"<SECRET>\") as client:
            ok, detail = await self.adapter.emoji_like(123456789, 66)
            self.assertTrue(ok, f"应当成功，实际：{detail}")
            sent = [a for a in client.actions if a.get("action") == "set_msg_emoji_like"]
            self.assertEqual(len(sent), 1, "必须恰好发出一条 set_msg_emoji_like")
            self.assertEqual(sent[0]["params"], {
                "message_id": 123456789, "emoji_id": 66, "set": True,
                "self_id": <BOT_QQ>})
            # 返回的是协议端原始响应 JSON
            payload = json.loads(detail)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["retcode"], 0)

    async def test_emoji_like_unset(self):
        async with MockNapCatMedia(self.url, token=\"<SECRET>\") as client:
            ok, _ = await self.adapter.emoji_like(1, 4, set_=False)
            self.assertTrue(ok)
            sent = [a for a in client.actions if a.get("action") == "set_msg_emoji_like"]
            self.assertIs(sent[0]["params"]["set"], False)

    async def test_emoji_like_blocked_by_read_only(self):
        """read_only=true 时贴表情也必须被拦（出站一律拦，与 send 同规矩）。"""
        self.adapter.read_only = True
        async with MockNapCatMedia(self.url, token=\"<SECRET>\") as client:
            ok, detail = await self.adapter.emoji_like(1, 4)
            self.assertFalse(ok)
            self.assertIn("read_only", detail)
            self.assertEqual([a for a in client.actions
                              if a.get("action") == "set_msg_emoji_like"], [])


class TestImageLanding(MediaHarness):
    """要求 ③：入站图片落盘成功，正文带本地路径。"""

    async def test_private_image_is_saved_and_path_is_in_text(self):
        async with MockNapCatMedia(self.url, token=\"<SECRET>\") as client:
            await client.push_with_segments([
                {"type": "text", "data": {"text": "看这个"}},
                {"type": "image", "data": {"file": "abcdef.image"}},
            ], message_type="private")
            await asyncio.sleep(0.6)

            self.assertEqual(len(self.dispatched), 1)
            text = self.dispatched[0].text
            self.assertTrue(text.startswith("看这个[图片:"), f"实际正文：{text!r}")
            self.assertTrue(text.endswith("]"), f"实际正文：{text!r}")

            path = Path(text[len("看这个[图片:"):-1])
            self.assertTrue(path.is_file(), f"落盘文件不存在：{path}")
            self.assertEqual(path.read_bytes(), PNG, "落盘内容必须与协议端返回的字节一致")
            self.assertEqual(path.read_bytes()[:8], b"\x89PNG\r\n\x1a\n", "magic 必须是 PNG")
            # 落盘目录在 media_dir/<日期>/ 下
            self.assertEqual(path.parent.parent, self.adapter.media_dir)
            # 协议端确实被问过 get_image
            self.assertEqual(client.image_calls, [{"file": "abcdef.image"}])

    async def test_multiple_images_keep_order(self):
        async with MockNapCatMedia(self.url, token=\"<SECRET>\") as client:
            await client.push_with_segments([
                {"type": "image", "data": {"file": "a.image"}},
                {"type": "image", "data": {"file": "b.image"}},
            ], message_type="private")
            await asyncio.sleep(0.6)
            text = self.dispatched[0].text
            self.assertEqual(text.count("[图片:"), 2, f"实际正文：{text!r}")
            self.assertEqual(len(client.image_calls), 2)
            self.assertEqual([c["file"] for c in client.image_calls], ["a.image", "b.image"])

    async def test_media_disabled_falls_back_to_plain_placeholder(self):
        """关掉开关 → 行为与改动前一致（`[图片]`），且**不去调** get_image。"""
        self.adapter.media_download_enabled = False
        async with MockNapCatMedia(self.url, token=\"<SECRET>\") as client:
            await client.push_with_segments([
                {"type": "image", "data": {"file": "a.image"}},
            ], message_type="private")
            await asyncio.sleep(0.6)
            self.assertEqual(self.dispatched[0].text, "[图片]")
            self.assertEqual(client.image_calls, [])

    async def test_image_without_file_falls_back_to_direct_url(self):
        """段里没有 `file` 只有 `url` 时走直连下载兜底（这里 URL 不可达 → 保留占位）。"""
        async with MockNapCatMedia(self.url, token=\"<SECRET>\") as client:
            await client.push_with_segments([
                {"type": "image", "data": {"url": "http://127.0.0.1:9/nope.png"}},
            ], message_type="private")
            await asyncio.sleep(0.6)
            self.assertEqual(self.dispatched[0].text, "[图片]")
            self.assertEqual(client.image_calls, [])

    async def test_group_window_keeps_local_path_after_fix(self):
        """**2026-10-03 反转旧断言**：群窗口里的图必须落盘、窗口文本要带本地路径。

        旧行为是"群采集不下载，只留 `[图片]` 占位"（省磁盘）。主人实测的后果：
        群里发的图和表情包她**一律只看到 `[图片]`/`[表情包]` 俩字**，包括那张
        `get_image` 明明成功落盘了的照片 —— 因为窗口存的是占位、路径只在被叫醒的
        那条消息上。动图（表情包）同理：文件是 `.gif`，落下来她就能看图，落不下来
        就只剩三个字。
        """
        async with MockNapCatMedia(self.url, token=\"<SECRET>\") as client:
            await client.push_with_segments([
                {"type": "image", "data": {"file": "a.image"}},
            ], message_type="group", group_id="55555")
            await asyncio.sleep(0.6)
            recs = self.adapter.group_window.tail("55555")
            self.assertEqual(len(recs), 1)
            text = recs[0]["text"]
            self.assertTrue(text.startswith("[图片:"), f"窗口文本没带路径：{text!r}")
            path = Path(text[len("[图片:"):-1])
            self.assertTrue(path.is_file(), f"落盘文件不存在：{path}")
            self.assertEqual(path.read_bytes(), PNG)
            self.assertEqual(client.image_calls, [{"file": "a.image"}],
                             "群采集路径必须真去取图（否则她永远看不见）")
            self.assertEqual(self.dispatched, [], "collect-only 不该唤醒回合")

    async def test_group_sticker_gif_lands_with_path_too(self):
        """表情包（sub_type=1，QQ 里就是动图）走同一条路：窗口里也要有本地路径。"""
        async with MockNapCatMedia(self.url, token=\"<SECRET>\") as client:
            await client.push_with_segments([
                {"type": "image",
                 "data": {"file": "s.image", "sub_type": 1, "summary": "[动画表情]"}},
            ], message_type="group", group_id="55556")
            await asyncio.sleep(0.6)
            recs = self.adapter.group_window.tail("55556")
            self.assertEqual(len(recs), 1)
            text = recs[0]["text"]
            self.assertTrue(text.startswith("[表情包:"), f"实际：{text!r}")
            self.assertTrue(Path(text[len("[表情包:"):-1]).is_file())

    async def test_group_window_download_can_be_switched_off(self):
        """`media_group_download=false` → 退回旧行为（只占位、不调 get_image）。"""
        self.adapter.media_group_download = False
        async with MockNapCatMedia(self.url, token=\"<SECRET>\") as client:
            await client.push_with_segments([
                {"type": "image", "data": {"file": "a.image"}},
            ], message_type="group", group_id="55557")
            await asyncio.sleep(0.6)
            recs = self.adapter.group_window.tail("55557")
            self.assertEqual([r["text"] for r in recs], ["[图片]"])
            self.assertEqual(client.image_calls, [], "关掉开关就不该取图")


if __name__ == "__main__":
    unittest.main(verbosity=2)
