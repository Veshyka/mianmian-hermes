#!/usr/bin/env python3
"""语音转写钉子（2026-10-04）。不用网络：STT 用进程内假服务，协议端动作用假 WS。

钉住的规矩：
  1. record 段 → 正文里 "[语音]" 变成 "[语音: 文本]"（私聊与群窗口两条路都要）
  2. 一条消息里多条语音按顺序回填，一条没转出来只影响它自己
  3. 取音频/转写任何一步失败 → 保留 "[语音]" 占位，不抛异常、不吞掉整条消息
  4. 超上限的语音不转写
  5. 缓存：同一个 event 只取一次、只转一次
"""
from __future__ import annotations

import asyncio
import base64
import importlib
import json
import os
import pathlib
import sys
import tempfile
import unittest

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(_PLUGIN_DIR))          # 让 `onebot` 成为包
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/opt/hermes")

_adapter = importlib.import_module("onebot.adapter")      # 相对 import 必须有包上下文
_proto = importlib.import_module("onebot.onebot_proto")

MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"fake-mp3-payload" * 8


def _ev(*segs, kind="private"):
    msg = []
    for s in segs:
        msg.append(s if isinstance(s, dict) else {"type": "text", "data": {"text": s}})
    base = {"post_type": "message", "message_type": kind, "message": msg,
            "raw_message": "".join(s.get("data", {}).get("text", "") for s in msg),
            "user_id": <OWNER_QQ>, "self_id": <BOT_QQ>, "message_id": 1,
            "sender": {"nickname": "主人", "user_id": <OWNER_QQ>}}
    if kind == "group":
        base["group_id"] = <GROUP_ID>
    return base


def _rec(fid="abc123.amr"):
    return {"type": "record", "data": {"file": fid,
                                       "path": f"/app/.config/QQ/nt_qq_x/nt_data/Ptt/{fid}",
                                       "file_size": "9534"}}


class _FakeAdapter(_adapter.OneBotAdapter):
    """只装出转写需要的那点东西：假协议端动作 + 真 STT（打到进程内假服务）。"""

    def __init__(self, *, stt_url, record_ok=True, b64=None, local_path=None,
                 has_path=False):
        self.media_download_enabled = True
        self.voice_transcribe_enabled = True
        self.media_group_download = True
        self.voice_out_format = "mp3"
        self.stt_url = stt_url
        self.stt_model = "test-model"
        self.stt_timeout = 10.0
        self.media_max_bytes = 8 * 1024 * 1024
        self.media_max_per_msg = 4
        self.media_dir = pathlib.Path(tempfile.mkdtemp(prefix="voice-test-"))
        self._voice_ok = 0
        self._voice_failed = 0
        self._media_saved = 0
        self._media_failed = 0
        self._calls = []
        self._record_ok = record_ok
        self._b64 = b64
        self._local_path = local_path
        self._has_path = has_path

    async def _call_action(self, payload, timeout=30.0):  # type: ignore[override]
        self._calls.append(payload)
        if not self._record_ok:
            return {"status": "failed", "retcode": 1, "message": "no such record"}
        data = {}
        if self._b64 is not None:
            data["base64"] = self._b64
        if self._has_path:
            data["file"] = str(self._local_path)
        return {"status": "ok", "retcode": 0, "data": data}

    async def _persist_images(self, event):  # 图片这条不参与
        return []

    async def _http_get(self, url):  # 图片兜底不参与
        return None


class _FakeStt:
    """进程内 OpenAI 兼容 STT：按请求里的模型名决定回什么。"""

    def __init__(self):
        self.calls = []
        self.mode = "ok"

    async def handle(self, request):
        from aiohttp import web
        body = await request.post()
        raw = body.get("file")
        content = raw.file.read() if hasattr(raw, "file") else b""
        self.calls.append({"model": body.get("model"), "language": body.get("language"),
                           "bytes": len(content)})
        if self.mode == "error":
            return web.json_response({"detail": "boom"}, status=500)
        if self.mode == "empty":
            return web.json_response({"text": "   "})
        return web.json_response({"text": "我现在还是在外面呢等会我要去帮我妈拿东西"})


class VoiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from aiohttp import web
        cls.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(cls.loop)
        cls.fake = _FakeStt()
        cls.app = web.Application()
        cls.app.router.add_post("/v1/audio/transcriptions", cls.fake.handle)
        cls.runner = web.AppRunner(cls.app)

        async def _up():
            await cls.runner.setup()
            site = web.TCPSite(cls.runner, "127.0.0.1", 0)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
            return f"http://127.0.0.1:{port}/v1/audio/transcriptions"

        cls.stt_url = cls.loop.run_until_complete(_up())

    @classmethod
    def tearDownClass(cls):
        cls.loop.run_until_complete(cls.runner.cleanup())
        cls.loop.close()

    def _ad(self, **kw):
        kw.setdefault("stt_url", self.stt_url)
        return _FakeAdapter(**kw)

    def _run(self, coro):
        return self.loop.run_until_complete(coro)

    # ── 正常路 ────────────────────────────────────────────────────────────────
    def test_private_voice_becomes_text(self):
        self.fake.mode = "ok"
        self.fake.calls.clear()
        ad = self._ad(b64=base64.b64encode(MP3).decode())
        out = self._run(ad._text_with_media(_ev(_rec())))
        self.assertIn("[语音: 我现在还是在外面呢等会我要去帮我妈拿东西]", out)
        self.assertNotIn("[语音]", out)
        self.assertEqual(ad._voice_ok, 1)
        self.assertEqual(self.fake.calls[0]["language"], "zh")
        self.assertEqual(self.fake.calls[0]["model"], "test-model")
        self.assertEqual(self.fake.calls[0]["bytes"], len(MP3))

    def test_group_window_voice_becomes_text(self):
        self.fake.mode = "ok"
        ad = self._ad(b64=base64.b64encode(MP3).decode())
        out = self._run(ad._group_text_with_media(_ev(_rec(), kind="group")))
        self.assertIn("[语音: 我现在还是在外面呢等会我要去帮我妈拿东西]", out)

    def test_two_voices_fill_in_order(self):
        self.fake.mode = "ok"
        ad = self._ad(b64=base64.b64encode(MP3).decode())
        out = self._run(ad._text_with_media(_ev(_rec("a.amr"), "然后呢", _rec("b.amr"))))
        self.assertEqual(out.count("[语音: 我现在还是在外面呢等会我要去帮我妈拿东西]"), 2)
        self.assertEqual(ad._voice_ok, 2)

    def test_path_fallback_when_no_base64(self):
        """没有 base64 时走本地路径兜底（容器前缀映射）。"""
        self.fake.mode = "ok"
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "x.amr.mp3"
            p.write_bytes(MP3)
            ad = self._ad(b64=None, local_path=p, has_path=True)
            out = self._run(ad._text_with_media(_ev(_rec())))
        self.assertIn("[语音: 我现在还是在外面呢等会我要去帮我妈拿东西]", out)

    # ── 失败路：一律保留占位 ──────────────────────────────────────────────────
    def test_get_record_failure_keeps_placeholder(self):
        ad = self._ad(record_ok=False)
        out = self._run(ad._text_with_media(_ev(_rec())))
        self.assertIn("[语音]", out)
        self.assertNotIn("[语音:", out)
        self.assertEqual(ad._voice_failed, 1)

    def test_stt_error_keeps_placeholder(self):
        self.fake.mode = "error"
        ad = self._ad(b64=base64.b64encode(MP3).decode())
        out = self._run(ad._text_with_media(_ev(_rec())))
        self.assertIn("[语音]", out)
        self.assertNotIn("[语音:", out)

    def test_stt_empty_text_keeps_placeholder(self):
        self.fake.mode = "empty"
        ad = self._ad(b64=base64.b64encode(MP3).decode())
        out = self._run(ad._text_with_media(_ev(_rec())))
        self.assertIn("[语音]", out)
        self.assertNotIn("[语音:", out)
        self.fake.mode = "ok"

    def test_oversize_voice_skipped(self):
        ad = self._ad(b64=base64.b64encode(MP3).decode())
        ad.media_max_bytes = 4
        out = self._run(ad._text_with_media(_ev(_rec())))
        self.assertIn("[语音]", out)
        self.assertNotIn("[语音:", out)

    def test_switch_off_returns_plain_text(self):
        ad = self._ad(b64=base64.b64encode(MP3).decode())
        ad.voice_transcribe_enabled = False
        out = self._run(ad._text_with_media(_ev(_rec())))
        self.assertEqual(out, "[语音]")
        self.assertEqual(ad._calls, [])

    # ── 只取一次 ──────────────────────────────────────────────────────────────
    def test_single_fetch_per_event(self):
        self.fake.mode = "ok"
        ad = self._ad(b64=base64.b64encode(MP3).decode())
        ev = _ev(_rec())
        first = self._run(ad._group_text_with_media(ev))
        second = self._run(ad._text_with_media(ev))
        self.assertIn("[语音: ", first)
        self.assertIn("[语音: ", second)
        self.assertEqual(len(ad._calls), 1)
        self.assertEqual(ad._voice_ok, 1)

    # ── 纯函数 ────────────────────────────────────────────────────────────────
    def test_voice_texts_helper(self):
        self.assertEqual(_proto.with_voice_texts("[语音]", ["你好"]), "[语音: 你好]")
        self.assertEqual(_proto.with_voice_texts("[语音]", [None]), "[语音]")
        self.assertEqual(_proto.with_voice_texts("[语音]", [""]), "[语音]")
        self.assertEqual(_proto.with_voice_texts("[语音]和[语音]", ["甲", "乙"]),
                         "[语音: 甲]和[语音: 乙]")
        # 没有语音段时原样返回
        self.assertEqual(_proto.with_voice_texts("说话", []), "说话")

    def test_record_segments(self):
        ev = _ev(_rec("a.amr"), _rec("b.amr"))
        self.assertEqual([d.get("file") for d in _proto.record_segments(ev)],
                         ["a.amr", "b.amr"])
        self.assertEqual(_proto.record_segments(_ev("只有字")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class VoiceWakeGateTest(unittest.TestCase):
    """主人的问题：群里发语音，内容明明相关，却一次都没被叫醒过。

    根因是判定打分读的是原始渲染 —— record 段在那层只有 "[语音]" 占位，等于没内容。
    这一对钉子：同一条语音，转写喂进去了就过闸，没喂就不过（证明机制真的在这)。
    """

    def _ad(self, *, transcribe=True):
        from aiohttp import web  # noqa: F401
        ad = _FakeAdapter(stt_url="http://127.0.0.1:1/none", b64=None, record_ok=False)
        ad.voice_transcribe_enabled = transcribe
        # 群唤醒判定要的那点状态（真实属性名，别自造）
        ad.group_wake_mode = "gated"
        ad.group_memory_guard_required = False
        ad.self_id = "<BOT_QQ>"
        ad.group_alias_names = ()
        ad.group_reply_to_her_wakes = True
        ad.group_gate_threshold = 0.35
        ad.group_enabled = True
        ad.group_collect_enabled = True
        ad._group_own_mids = {}
        ad._group_gate_state = {}
        ad._group_wake_limited = 0
        ad._group_wake_blocked = 0
        ad._group_full_count = 0
        ad._group_mention_count = 0
        ad._group_reply_count = 0
        ad._group_name_count = 0
        ad._gate_wake_count = 0
        ad._group_last_trigger = ""
        ad._gwk = importlib.import_module("onebot.group_wake")
        ad._group_wake_limiter = ad._gwk.WakeLimiter(per_minute=99, per_hour=999)
        return ad

    def test_voice_transcript_wakes_her(self):
        ad = self._ad()
        ev = _ev(_rec("v.amr"), kind="group")
        # 转写结果就是她该看到的正文（真样本：「上面有写了什么东西吗」）
        ev["_voice_texts"] = ["上面有写了什么东西吗"]
        text = ad._group_score_text(ev)
        self.assertEqual(text, "[语音: 上面有写了什么东西吗]")
        reason = ad._group_wake_trigger(ev, gid="<GROUP_ID>", score_text=text)
        self.assertEqual(reason, "gate", "转写文本必须能让她过闸（疑问词 + 有信息量）")

    def test_without_transcript_it_stays_quiet(self):
        ad = self._ad(transcribe=False)
        ev = _ev(_rec("v.amr"), kind="group")
        ev["_voice_texts"] = []                       # 没转写 → 判定只能看到 "[语音]" 占位
        text = ad._group_score_text(ev)
        self.assertEqual(text, "[语音]")
        reason = ad._group_wake_trigger(ev, gid="<GROUP_ID>", score_text=text)
        self.assertEqual(reason, "", "拿占位打分只该有闲置压力 0.25 < 0.35")

    def test_non_voice_message_keeps_original_path(self):
        """没有语音段时必须返回 None（判定走原口径，行为与改动前逐字一致）。"""
        ad = self._ad()
        ev = _ev("今天天气不错", kind="group")
        self.assertIsNone(ad._group_score_text(ev))

    def test_gate_state_uses_transcript_too(self):
        """门控状态里的复读判定也要看真内容。"""
        ad = self._ad()
        ev = _ev(_rec("v.amr"), kind="group")
        ev["_voice_texts"] = ["今天吃什么好呢"]
        ad._note_gate_incoming(ev, gid="<GROUP_ID>")
        st = ad._group_gate_state["<GROUP_ID>"]
        self.assertEqual(len(st._recent), 1, "转写文本该被喂进门控状态")
        # 门控里存的是指纹（_fp），比对指纹就证明喂进去的是**转写文本**而不是 "[语音]"
        self.assertEqual(st._recent[-1], ad._gwk._fp("[语音: 今天吃什么好呢]"))
        self.assertNotEqual(st._recent[-1], ad._gwk._fp("[语音]"))
