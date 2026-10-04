"""出站消息段的**类型**必须合 NapCat 的 OB11 schema —— 这个文件就是那道闸。

## 为什么有它（真实事故）

2026-10-03 06:51，聊天门把表情当消息发出去时整条失败：

```
WARNING [onebot] send attempt 1/3 failed (retcode=1200 status=failed
        msg=消息体无法解析, 请检查是否发送了不支持的消息类型)
ERROR   [onebot] segment 3/3 failed: retcode=1200 …
```

根因：`cq_to_segments()` 用的是一个**全局**「要转数字的 key」集合
``{"qq","id","user_id","group_id","sub_type"}``，于是
``[CQ:face,id=500]`` → ``{"type":"face","data":{"id":500}}``（数字）。而 NapCat 的
``OB11MessageFaceSchema`` 要求 ``data.id`` 是 **String** → schema 校验失败 → 整条消息被拒。

同一个坑还埋着第二颗雷：``at.data.qq`` 也要求 **String**，所以
``[CQ:at,qq=<OWNER_QQ>]``（群里真 @ 主人、群管理欢迎语 ``{at}``）同样会 1200。

修法：**按段类型**决定哪些 key 转数字，只有 schema 明确写 ``Type.Number`` 的才转
（``image.sub_type`` / ``mface.emoji_package_id``），其余一律保持字符串。

## 第二层：编号本身要合法（同一事故的另一半）

改完类型还不算完 —— 事故里那个编号 **500 不是原生小黄脸**。2026-10-03 实测（NapCat HTTP）：

```
形如 {"type":"face","data":{"id":"14"}}   → {"status":"ok","retcode":0,"data":{"message_id":…}}
形如 {"type":"face","data":{"id":"500"}}  → {"status":"failed","retcode":200,
                                            "message":"消息体无法解析, 请检查是否发送了不支持的消息类型"}
```

500 不在 NapCat 自带的 `qq_emoji_list.QQ_FACE`（219 项，0–128563）里 —— 它是 QQ 的
「大表情/超级表情」，**不能当 face 段发**。所以 `cq_to_segments()` 现在拿这张表当**白名单**：
编号不在表里就丢掉那一段（其余文本照常发），整条只有它时退化成字面 `[表情:500]`。

## 依据（一手）

``packages/napcat-onebot/types/message.ts``（本地存档
``/opt/data/tmp/napcat_src/packages__napcat-onebot__types__message.ts``）：
  face.id → String / at.qq → String / reply.id → String / image.sub_type → Number /
  mface.emoji_package_id → Number、emoji_id・key → String / text.text → String

⚠️ 文件名用 `check_` 前缀（disk-cleanup 会删 test_*）。
⚠️ 只连进程内的假协议端（MockNapCat），不碰真 NapCat、不往任何真实会话发东西。
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from typing import Any, Dict, List, Tuple

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PLUGIN_PARENT = os.path.dirname(_PLUGIN_DIR)
_PKG = os.path.basename(_PLUGIN_DIR)
sys.path.insert(0, _PLUGIN_PARENT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from check_adapter_e2e import AdapterHarness  # noqa: E402
from check_emoji_media import MockNapCatMedia  # noqa: E402

_proto = __import__(f"{_PKG}.onebot_proto", fromlist=["*"])
_stick = __import__(f"{_PKG}.sticker_lib", fromlist=["*"])

#: 我们要发的段类型 → 每个 key 的**必需 Python 类型**（照 NapCat OB11 schema 抄）。
#: 只列我们真的会构造的段；schema 里是 Optional 的 key 不列（不强制）。
SCHEMA: Dict[str, Dict[str, type]] = {
    "text": {"text": str},
    "face": {"id": str},
    "at": {"qq": str},
    "reply": {"id": str},
    "image": {"file": str, "sub_type": int},
    "mface": {"emoji_package_id": int, "emoji_id": str, "key": str, "summary": str},
}


def assert_schema(test: unittest.TestCase, segs: List[Dict[str, Any]], where: str = "") -> None:
    """逐段核对类型：多出来的 key 不管，列进 SCHEMA 的 key 类型必须对。"""
    for seg in segs:
        stype = seg.get("type")
        test.assertIn(stype, SCHEMA, f"{where} 出现了没登记的段类型 {stype!r}：{seg}")
        data = seg.get("data") or {}
        for key, want in SCHEMA[stype].items():
            if key not in data:
                continue
            got = data[key]
            test.assertIsInstance(
                got, want,
                f"{where} 段 {stype}.{key} 类型不对：要 {want.__name__}，实际 "
                f"{type(got).__name__}（{got!r}）—— 这正是 2026-10-03 retcode=1200 的成因")


def _segs(text: str, *, is_group: bool = False, reply_to: str = None) -> List[Dict[str, Any]]:
    return _proto.build_action("send_private_msg", "<OWNER_QQ>", text,
                               is_group=is_group, reply_to=reply_to)["params"]["message"]


class TestSegmentTypes(unittest.TestCase):
    """纯函数层：每个段类型的 key 类型。"""

    def test_face_id_is_string(self):
        segs = _segs("[CQ:face,id=14]")
        self.assertEqual(segs, [{"type": "face", "data": {"id": "14"}}])
        assert_schema(self, segs, "face")

    def test_face_id_500_is_not_a_native_face(self):
        """事故里的那个值：500 **不是**原生小黄脸 → 现在会被丢掉（见文件头「第二层」）。"""
        self.assertNotIn(500, _proto.NATIVE_FACE_IDS)
        segs = _segs("[CQ:face,id=500]")
        self.assertEqual(segs, [{"type": "text", "data": {"text": "[表情:500]"}}])

    def test_at_qq_is_string(self):
        """群里真 @、群管理欢迎语 ``{at}`` 都走这条 —— 数字会被 1200 拒。"""
        segs = _segs("[CQ:at,qq=<OWNER_QQ>] 在吗", is_group=True)
        self.assertEqual(segs[0], {"type": "at", "data": {"qq": "<OWNER_QQ>"}})
        assert_schema(self, segs, "at")

    def test_at_all_stays_string(self):
        segs = _segs("[CQ:at,qq=all]")
        self.assertEqual(segs[0]["data"]["qq"], "all")

    def test_image_sub_type_is_int(self):
        """图片那边的 sub_type 是 schema 里的 Number —— 别被「都改成字符串」误伤。"""
        segs = _segs("[CQ:image,file=/tmp/a.png,sub_type=1,summary=动画表情]")
        img = [s for s in segs if s["type"] == "image"][0]
        self.assertIsInstance(img["data"]["sub_type"], int)
        self.assertEqual(img["data"]["sub_type"], 1)
        self.assertEqual(img["data"]["file"], "/tmp/a.png")
        assert_schema(self, segs, "image")

    def test_mface_types(self):
        segs = _segs("[CQ:mface,emoji_package_id=4321,emoji_id=9988,key=abc,summary=笑]")
        mf = segs[0]
        self.assertIsInstance(mf["data"]["emoji_package_id"], int)
        self.assertIsInstance(mf["data"]["emoji_id"], str)
        assert_schema(self, segs, "mface")

    def test_reply_segment_id_is_string(self):
        segs = _segs("嗯", reply_to=12345)
        self.assertEqual(segs[0], {"type": "reply", "data": {"id": "12345"}})
        assert_schema(self, segs, "reply")

    def test_non_native_face_id_is_dropped(self):
        """500 不是原生小黄脸 → 丢掉这一段，保住同一轮里的文字。"""
        segs = _segs("看这个[CQ:face,id=500]")
        self.assertEqual(segs, [{"type": "text", "data": {"text": "看这个"}}],
                         f"非原生表情必须被丢掉，实际 {segs}")

    def test_non_native_face_alone_degrades_to_text(self):
        """整条只有那个发不出去的表情 → 退化成字面标签（不静默吞、也不空消息）。"""
        segs = _segs("[CQ:face,id=500]")
        self.assertEqual([s["type"] for s in segs], ["text"])
        self.assertEqual(segs[0]["data"]["text"], "[表情:500]")

    def test_native_face_ids_kept(self):
        """表里的编号一个都不能被误伤。"""
        for fid in (0, 14, 307, 326, 128563):
            segs = _segs(f"[CQ:face,id={fid}]")
            self.assertEqual(segs, [{"type": "face", "data": {"id": str(fid)}}],
                             f"原生表情 {fid} 被误丢了：{segs}")

    def test_face_drop_counter_records(self):
        _proto._DROPPED_FACES.clear()
        _segs("[CQ:face,id=500][CQ:face,id=500]")
        self.assertEqual(_proto._DROPPED_FACES.get("500"), 2)

    def test_unknown_segment_keeps_strings(self):
        """没登记的段类型：保守保持字符串（不许再猜数字）。"""
        segs = _segs("[CQ:somefuture,id=7,foo=8]")
        self.assertEqual(segs[0]["data"], {"id": "7", "foo": "8"})

    def test_mixed_message_all_types_ok(self):
        segs = _segs("[CQ:at,qq=<OWNER_QQ>] 看图[CQ:image,file=/tmp/a.png,sub_type=1]"
                     "哈哈哈[CQ:face,id=182]",
                     is_group=True)
        self.assertEqual([s["type"] for s in segs], ["at", "text", "image", "text", "face"])
        assert_schema(self, segs, "mixed")

    def test_num_keys_no_longer_global(self):
        """回归钉子：旧写法是全局集合，改回全局就会让 face/at 再挂。"""
        self.assertNotIn("id", _proto._CQ_NUM_KEYS)
        self.assertNotIn("qq", _proto._CQ_NUM_KEYS)
        self.assertEqual(_proto._CQ_NUM_KEYS_BY_TYPE["image"], ("sub_type",))


class FaceHarness(AdapterHarness):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.adapter.media_dir = __import__("pathlib").Path(self._tmp) / "onebot-media"
        self.adapter.stickers_enabled = False          # 本文件只关心出站类型
        self.adapter.self_id = "<BOT_QQ>"


class TestFaceOutboundE2E(FaceHarness):
    """端到端：真反向 WS + 假协议端，看她那条「只有表情」的回复实际发了什么。"""

    async def test_only_face_segment_goes_out(self):
        async with MockNapCatMedia(self.url, token=\"<SECRET>\") as client:
            res = await self.adapter.send("10001", "你[表情:14]⁂[CQ:face,id=14]")
            self.assertTrue(res.success)
            sent = [a for a in client.actions if a.get("action") == "send_private_msg"]
            self.assertEqual(len(sent), 2, "⁂ 切成两段 → 两次发送（这正是事故当时的形状）")
            segs = sent[-1]["params"]["message"]
            self.assertEqual(segs, [{"type": "face", "data": {"id": "14"}}],
                             f"事故重现：那条只含表情的段应当是**一个 face 段**"
                             f"（id 为字符串），实际 {segs}")
            assert_schema(self, segs, "e2e face")

    async def test_mixed_reply_face_and_text(self):
        async with MockNapCatMedia(self.url, token=\"<SECRET>\") as client:
            res = await self.adapter.send("10001", "好⁂[CQ:at,qq=<OWNER_QQ>] 看这个 [CQ:face,id=307]")
            self.assertTrue(res.success)
            all_segs = []
            for a in client.actions:
                if a.get("action") == "send_private_msg":
                    all_segs.extend(a["params"]["message"])
            assert_schema(self, all_segs, "e2e mixed")
            faces = [s for s in all_segs if s["type"] == "face"]
            ats = [s for s in all_segs if s["type"] == "at"]
            self.assertEqual(faces[0]["data"]["id"], "307")   # 307 = 喵喵（原生，可发）
            self.assertEqual(ats[0]["data"]["qq"], "<OWNER_QQ>")

    async def test_non_native_face_never_hits_the_wire(self):
        """事故重现（带 id=500 的那条）：文字照发，那个表情一个字节都不发出去。"""
        async with MockNapCatMedia(self.url, token=\"<SECRET>\") as client:
            res = await self.adapter.send("10001", "那就直接当消息发给你⁂[CQ:face,id=500]")
            self.assertTrue(res.success)
            sent = [a for a in client.actions if a.get("action") == "send_private_msg"]
            faces = [s for a in sent for s in a["params"]["message"] if s["type"] == "face"]
            self.assertEqual(faces, [], f"非原生表情还是发出去了：{faces}")

    async def test_no_cq_code_leaks_as_text(self):
        """CQ 码不许以字面文本漏给主人（漏了就是「解析失败但不报错」的静默故障）。"""
        async with MockNapCatMedia(self.url, token=\"<SECRET>\") as client:
            await self.adapter.send("10001", "嗯[CQ:face,id=14]")
            bodies = "".join(
                s["data"].get("text", "")
                for a in client.actions if a.get("action") == "send_private_msg"
                for s in a["params"]["message"] if s["type"] == "text")
            self.assertNotIn("[CQ:", bodies, f"字面 CQ 码漏出来了：{bodies!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
