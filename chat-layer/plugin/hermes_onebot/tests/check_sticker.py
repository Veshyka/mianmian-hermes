"""表情包（PLAN-v6）的逐条可复现验证。

覆盖四件事：
  1. **判据**：``is_sticker_image()`` 认得收藏表情（sub_type=1/2/7）、超级表情（emoji_id）、
     动图摘要（summary），**普通照片判 False**。
  2. **入站渲染**：表情包 → ``[表情包:<路径>]``，照片 → ``[图片:<路径>]``（她据此决定
     「读情绪」还是「描述画面」）。
  3. **出站**：正文里的 ``[CQ:image,…]`` 变成**真 image 段**（她发文/发表情包的唯一通道）；
     ``[贴表情:赞]`` 被剥出正文、发送后真的发出 ``set_msg_emoji_like``。
  4. **表情库**：入库/去重/情绪标签/上限 LRU/清理 全部在临时目录里验证。

⚠️ 文件名用 `check_` 前缀（disk-cleanup 会删 test_*）。
⚠️ **全程只连进程内的假协议端**（MockNapCatMedia）—— 不碰真 NapCat、不往群里发东西。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PLUGIN_PARENT = os.path.dirname(_PLUGIN_DIR)
_PKG = os.path.basename(_PLUGIN_DIR)
sys.path.insert(0, _PLUGIN_PARENT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from check_adapter_e2e import AdapterHarness  # noqa: E402
from check_emoji_media import MockNapCatMedia, PNG_B64  # noqa: E402

_proto = __import__(f"{_PKG}.onebot_proto", fromlist=["*"])
_stick = __import__(f"{_PKG}.sticker_lib", fromlist=["*"])


def _evt(segments: List[Dict[str, Any]], message_type: str = "private") -> Dict[str, Any]:
    evt: Dict[str, Any] = {
        "post_type": "message", "message_type": message_type, "sub_type": "friend",
        "self_id": <BOT_QQ>, "user_id": <OWNER_QQ>, "message_id": 4242,
        "time": int(time.time()),
        "sender": {"user_id": <OWNER_QQ>, "nickname": "主人", "card": ""},
        "message": segments, "raw_message": "",
    }
    if message_type == "group":
        evt["group_id"] = 55555
    return evt


# ── 1. 判据 ────────────────────────────────────────────────────────────────


class TestStickerPredicate(unittest.TestCase):
    def test_custom_emoji_is_sticker(self):
        """sub_type=1（KCUSTOM）= 收藏/自定义表情 —— 主人说的那种。"""
        self.assertTrue(_proto.is_sticker_image({"sub_type": 1, "summary": "[动画表情]"}))
        self.assertTrue(_proto.is_sticker_image({"sub_type": "1"}))

    def test_hot_pic_is_sticker(self):
        self.assertTrue(_proto.is_sticker_image({"sub_type": 2}))

    def test_plain_photo_is_not_sticker(self):
        """sub_type=0（KNORMAL）= 普通照片，绝不能被判成表情包。"""
        self.assertFalse(_proto.is_sticker_image({"sub_type": 0, "file": "a.jpg"}))
        self.assertFalse(_proto.is_sticker_image({"sub_type": "0"}))

    def test_photo_without_sub_type_is_not_sticker(self):
        """字段缺失 → 保守按照片（照片只描述、不往库里塞）。"""
        self.assertFalse(_proto.is_sticker_image({"file": "IMG_0001.jpg", "url": "http://x/y.jpg"}))
        self.assertFalse(_proto.is_sticker_image({}))

    def test_marketface_sticker_by_emoji_id(self):
        """商城/超级表情：带 emoji_id、file 形如 xx-<id>.gif、不带 sub_type。"""
        self.assertTrue(_proto.is_sticker_image(
            {"file": "12-3456789.gif", "emoji_id": "3456789", "emoji_package_id": "12"}))

    def test_summary_hint(self):
        self.assertTrue(_proto.is_sticker_image({"summary": "[动画表情]"}))
        self.assertTrue(_proto.is_sticker_image({"summary": "热图分享"}))

    def test_sticker_label(self):
        self.assertEqual(_proto.sticker_label({"summary": "[动画表情]"}), "动画表情")
        self.assertEqual(_proto.sticker_label({"emoji_id": "99"}), "超级表情99")
        self.assertEqual(_proto.sticker_label({}), "表情包")


# ── 2. 入站渲染 ─────────────────────────────────────────────────────────────


class TestInboundRendering(unittest.TestCase):
    def test_sticker_and_photo_get_different_placeholders(self):
        evt = _evt([
            {"type": "text", "data": {"text": "哈哈"}},
            {"type": "image", "data": {"sub_type": 1, "summary": "[动画表情]", "file": "a.gif"}},
            {"type": "image", "data": {"sub_type": 0, "file": "b.jpg"}},
        ])
        self.assertEqual(_proto.extract_text(evt), "哈哈[表情包][图片]")
        self.assertEqual(_proto.extract_window_text(evt), "哈哈[表情包][图片]")

    def test_backfill_paths_by_kind(self):
        text = "看[图片]和[表情包]完了"
        out = _proto.with_media_paths(text, ["/media/a.jpg", "/media/b.gif"])
        self.assertEqual(out, "看[图片:/media/a.jpg]和[表情包:/media/b.gif]完了")

    def test_backfill_keeps_placeholder_on_failure(self):
        """落盘失败（None）时占位必须留着 —— 少一个她会以为那条消息里没有图。"""
        self.assertEqual(_proto.with_media_paths("看[表情包]", [None]), "看[表情包]")
        self.assertEqual(_proto.with_media_paths("看[图片]", [None]), "看[图片]")

    def test_old_single_placeholder_signature_still_works(self):
        """兼容旧的「只认一种占位」调用（既有测试/调用方在用）。"""
        out = _proto.with_media_paths("看[图片]", ["/a.jpg"], placeholder="[图片]")
        self.assertEqual(out, "看[图片:/a.jpg]")


# ── 3. 出站：正文 CQ 码 + 贴表情标记 ────────────────────────────────────────


class TestOutboundSyntax(unittest.TestCase):
    def test_cq_image_becomes_real_segment(self):
        frame = _proto.build_action(
            "send_private_msg", "123",
            "看图[CQ:image,file=/tmp/a.png,sub_type=1,summary=动画表情]好",
            is_group=False)
        segs = frame["params"]["message"]
        self.assertEqual([s["type"] for s in segs], ["text", "image", "text"])
        self.assertEqual(segs[1]["data"], {"file": "/tmp/a.png", "sub_type": 1,
                                           "summary": "动画表情"})
        # sub_type 必须是数字 —— 她写 `sub_type=1` 不是字符串
        self.assertIsInstance(segs[1]["data"]["sub_type"], int)

    def test_plain_text_unchanged(self):
        """没有 CQ 码时行为与以前**逐字一致**（一个 text 段）。"""
        frame = _proto.build_action("send_private_msg", "123", "普通一句话", is_group=False)
        self.assertEqual(frame["params"]["message"],
                         [{"type": "text", "data": {"text": "普通一句话"}}])

    def test_no_stray_cq_left_unparsed(self):
        frame = _proto.build_action(
            "send_group_msg", "55555",
            "[CQ:at,qq=<OWNER_QQ>] 在吗 [CQ:image,file=/tmp/a.png]",
            is_group=True)
        segs = frame["params"]["message"]
        self.assertEqual([s["type"] for s in segs], ["at", "text", "image"])
        self.assertEqual(segs[0]["data"]["qq"], <OWNER_QQ>)

    def test_emoji_like_marker_by_name_and_id(self):
        text, ids, unknown = _proto.extract_emoji_like_markers("笑死[贴表情:赞]哈哈[贴表情：76]")
        self.assertEqual(text, "笑死哈哈")
        self.assertEqual(ids, [76, 76])
        self.assertEqual(unknown, [])

    def test_emoji_like_unknown_name_is_stripped_but_reported(self):
        text, ids, unknown = _proto.extract_emoji_like_markers("嗯[贴表情:不存在的表情]")
        self.assertEqual(text, "嗯")
        self.assertEqual(ids, [])
        self.assertEqual(unknown, ["不存在的表情"])

    def test_only_marker_leaves_empty_text(self):
        text, ids, _ = _proto.extract_emoji_like_markers("[贴表情:吃瓜]")
        self.assertEqual(text, "")
        self.assertEqual(ids, [271])

    def test_emoji_id_table_is_sane_and_cross_checked(self):
        """id 表：名字唯一、id 是数字；能读到两源表时逐条交叉核对。"""
        tbl = _proto.EMOJI_LIKE_IDS
        self.assertTrue(tbl)
        self.assertEqual(len(set(tbl)), len(tbl))
        self.assertTrue(all(isinstance(v, int) and v > 0 for v in tbl.values()))
        src = Path("/opt/data/tmp/nc/qq_emoji_list.py")
        if not src.is_file():
            self.skipTest("两源表不在本机（/opt/data/tmp/nc/qq_emoji_list.py），跳过交叉核对")
        ns: Dict[str, Any] = {}
        exec(src.read_text(encoding="utf-8").replace("from typing import Dict", ""), ns)
        face = ns["QQ_FACE"]
        for name, eid in tbl.items():
            label = str(face.get(str(eid), ""))
            self.assertIn(name, label, f"{name}={eid} 与源表不符（源表写的是 {label!r}）")


# ── 4. 表情库（纯本地，临时目录）────────────────────────────────────────────


class TestStickerLibrary(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sticker-lib-"))
        self.lib = _stick.StickerLib(self.tmp, max_items=3, max_total_bytes=10 ** 6,
                                     retention_days=30)

    def _blob(self, n: int) -> bytes:
        return base64.b64decode(PNG_B64) + bytes([n])

    def _add(self, n: int, tags=None) -> Dict[str, Any]:
        blob = self._blob(n)
        p = self.tmp / f"item{n}.png"
        p.write_bytes(blob)
        return self.lib.add(blob, {"sub_type": 1}, str(p), tags=tags)

    def test_add_and_dedup(self):
        r1 = self._add(1)
        self.assertTrue(r1["ok"] and r1["dedup"] is False and r1["count"] == 1)
        r2 = self._add(1)
        self.assertTrue(r2["dedup"] is True, "同内容必须去重")
        self.assertEqual(len(self.lib.load()), 1)

    def test_index_is_readable_text(self):
        """索引是给模型/人读的纯文本：一行一条，空格分隔，路径绝对。"""
        self._add(1, tags=["开心", "笑"])
        line = self.lib.index_path.read_text(encoding="utf-8").strip()
        parts = line.split()
        self.assertEqual(len(parts), 5, f"索引行应为 5 段：{line!r}")
        self.assertEqual(parts[1], "开心,笑")
        self.assertTrue(parts[4].startswith("/"))

    def test_untagged_default(self):
        self._add(1)
        self.assertEqual(self.lib.stats()["untagged"], 1)

    def test_pick_by_emotion_and_fallback(self):
        self._add(1, tags=["开心"])
        self._add(2, tags=["生气"])
        got = {self.lib.pick("开心") for _ in range(20)}
        self.assertEqual(len(got), 1, "标了「开心」的只有一条，挑出来必须稳定是它")
        self.assertTrue(self.lib.pick("完全无关的词"), "无匹配时退到全部随机，不该返回 None")

    def test_pick_empty_library(self):
        self.assertIsNone(self.lib.pick("开心"))

    def test_lru_eviction_by_count(self):
        r1 = self._add(1)
        r2 = self._add(2)
        r3 = self._add(3)
        self.lib.touch(r1["hash"])          # 把第 1 条变成「最近用过」（必须晚于 2/3 的入库）
        r4 = self._add(4)                   # 第 4 条 → 超上限 3 → 淘汰最久没用的
        items = {it.hash for it in self.lib.load()}
        self.assertEqual(len(items), 3)
        self.assertIn(r1["hash"], items, "刚 touch 过的不该被删")
        self.assertIn(r4["hash"], items, "刚进来的当然留着")
        self.assertNotIn(r2["hash"], items, "最久没用的那条该被删")
        evicted = {e["hash"] for e in r4["evicted"]}
        self.assertIn(r2["hash"], evicted, "返回值里要带上被淘汰的清单")
        self.assertFalse(Path(r2["path"]).exists(), "被淘汰的文件要真删掉")

    def test_size_limit_evicts(self):
        lib = _stick.StickerLib(self.tmp / "small", max_items=100, max_total_bytes=100)
        for i in (1, 2, 3):
            blob = base64.b64decode(PNG_B64) + bytes([i]) * 60
            p = (self.tmp / "small")
            p.mkdir(parents=True, exist_ok=True)
            f = p / f"big{i}.png"
            f.write_bytes(blob)
            lib.add(blob, {"sub_type": 1}, str(f))
        self.assertLessEqual(lib.stats()["bytes"], 100 + 80, "体积上限没生效")

    def test_gc_removes_missing_and_expired(self):
        self._add(1, tags=["旧"])
        self._add(2, tags=["新"])
        items = self.lib.load()
        Path(items[0].path).unlink()                    # 文件没了
        old = items[1]
        old.last_used = time.time() - 40 * 86400        # 超过保留期
        self.lib.save([old])
        out = self.lib.gc()
        reasons = {r["reason"] for r in out["removed"]}
        self.assertEqual(out["kept"], 0)
        self.assertIn("expired", reasons)

    def test_tag_and_pick_after_tagging(self):
        r = self._add(1)
        t = _stick.tag(self.lib, hash_=r["hash"], tags="开心, 笑死")
        self.assertTrue(t["ok"])
        self.assertEqual(t["tags"], ["开心", "笑死"])
        self.assertEqual(self.lib.pick("开心"), self.lib.load()[0].path)

    def test_tag_last(self):
        self._add(1, tags=["a"])
        time.sleep(0.01)
        r2 = self._add(2)
        t = _stick.tag(self.lib, last=True, tags="无语")
        self.assertTrue(t["ok"])
        self.assertEqual(t["hash"], r2["hash"])

    def test_tag_unknown_hash(self):
        self.assertFalse(_stick.tag(self.lib, hash_="deadbeef", tags="开心")["ok"])

    def test_normalize_tags(self):
        self.assertEqual(_stick.normalize_tags("开心，无语;笑死 呀"), ["开心", "无语", "笑死 呀"])
        self.assertEqual(_stick.normalize_tags("a,b,c,d,e,f,g"), ["a", "b", "c", "d", "e"])
        self.assertEqual(_stick.normalize_tags("这个标签实在是太长了"), [])


# ── 5. 端到端（真反向 WS + 假协议端）────────────────────────────────────────


class StickerHarness(AdapterHarness):
    """复用适配器脚手架，把 media/sticker 目录指到临时目录（绝不碰 profile 真实路径）。"""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.adapter.media_dir = Path(self._tmp) / "onebot-media"
        self.adapter.media_download_enabled = True
        self.adapter.media_max_bytes = 1024 * 1024
        self.adapter.stickers_enabled = True
        self.adapter.emoji_like_enabled = True
        self.adapter.sticker_lib = _stick.StickerLib(Path(self._tmp) / "onebot-stickers")
        self.adapter.self_id = "<BOT_QQ>"


class TestInboundStickerStealing(StickerHarness):
    """入站表情包：落盘 → 顺手进库；照片不进库；同图去重。"""

    async def test_sticker_is_saved_to_library_and_labeled(self):
        async with MockNapCatMedia(self.url, token=\"<SECRET>\") as client:
            evt = _evt([{"type": "image",
                         "data": {"sub_type": 1, "summary": "[动画表情]", "file": "a.gif"}}])
            text = await self.adapter._text_with_media(evt)
            await asyncio.sleep(0.05)
            self.assertIn("[表情包:", text, f"表情包要标注成本地路径：{text!r}")
            self.assertTrue(text.endswith("]"), text)
            self.assertEqual(self.adapter.sticker_lib.stats()["count"], 1, "表情包应已进库")

            # 同一条再来一次 → 去重，不新增
            evt2 = _evt([{"type": "image",
                          "data": {"sub_type": 1, "summary": "[动画表情]", "file": "a.gif"}}])
            await self.adapter._text_with_media(evt2)
            await asyncio.sleep(0.05)
            self.assertEqual(self.adapter.sticker_lib.stats()["count"], 1, "同图必须去重")

    async def test_photo_is_not_stolen(self):
        async with MockNapCatMedia(self.url, token=\"<SECRET>\") as client:
            evt = _evt([{"type": "image",
                         "data": {"sub_type": 0, "file": "IMG_1.jpg"}}])
            text = await self.adapter._text_with_media(evt)
            await asyncio.sleep(0.05)
            self.assertIn("[图片:", text)
            self.assertNotIn("[表情包", text)
            self.assertEqual(self.adapter.sticker_lib.stats()["count"], 0, "照片不许进表情库")

    async def test_ingest_remembers_inbound_mid(self):
        async with MockNapCatMedia(self.url, token=\"<SECRET>\") as client:
            await client.push_with_segments([{"type": "text", "data": {"text": "在吗"}}],
                                            message_type="private", user_id="10001",
                                            message_id=7777)
            await asyncio.sleep(0.6)
            self.assertEqual(self.adapter._last_inbound_mid.get("10001"), "7777",
                             "入站 message_id 要记下来 —— 贴表情默认贴在它上面")


class TestOutboundStickerAndEmojiLike(StickerHarness):
    """出站：正文发图 + 贴表情回应，真跑一遍反向 WS。"""

    async def test_send_image_via_cq_code(self):
        img = Path(self._tmp) / "s.png"
        img.write_bytes(base64.b64decode(PNG_B64))
        async with MockNapCatMedia(self.url, token=\"<SECRET>\") as client:
            res = await self.adapter.send(
                "10001", f"给你[CQ:image,file={img},sub_type=1,summary=动画表情]",
                reply_to=None)
            self.assertTrue(res.success)
            sent = [a for a in client.actions if a.get("action") == "send_private_msg"]
            self.assertEqual(len(sent), 1)
            segs = sent[0]["params"]["message"]
            kinds = [s["type"] for s in segs]
            self.assertIn("image", kinds, f"应该发出真 image 段，实际：{kinds}")
            image_seg = [s for s in segs if s["type"] == "image"][0]
            self.assertEqual(image_seg["data"]["file"], str(img))
            self.assertEqual(image_seg["data"]["sub_type"], 1, "发表情包要带 sub_type=1")
            body = "".join(s["data"].get("text", "") for s in segs if s["type"] == "text")
            self.assertNotIn("[CQ:", body, "CQ 码不许漏到主人眼里")

    async def test_emoji_like_marker_is_stripped_and_executed(self):
        async with MockNapCatMedia(self.url, token=\"<SECRET>\") as client:
            self.adapter._last_inbound_mid["10001"] = "4242"
            res = await self.adapter.send("10001", "笑死我了[贴表情:赞]")
            self.assertTrue(res.success)
            texts = client.sent_texts("send_private_msg")
            self.assertEqual(texts, ["笑死我了"], f"标记必须被剥掉：{texts!r}")
            likes = [a for a in client.actions if a.get("action") == "set_msg_emoji_like"]
            self.assertEqual(len(likes), 1, "应当贴一个表情回应")
            self.assertEqual(likes[0]["params"]["emoji_id"], 76)
            self.assertEqual(likes[0]["params"]["message_id"], 4242,
                             "默认为「她正在回的那条消息」")
            self.assertEqual(self.adapter._emoji_likes, 1)

    async def test_emoji_like_targets_reply_to_when_present(self):
        async with MockNapCatMedia(self.url, token=\"<SECRET>\") as client:
            self.adapter._last_inbound_mid["10001"] = "1111"
            await self.adapter.send("10001", "嗯[贴表情:捂脸]", reply_to="9999")
            likes = [a for a in client.actions if a.get("action") == "set_msg_emoji_like"]
            self.assertEqual(likes[0]["params"]["message_id"], 9999,
                             "有 reply_to 时以回复的那条为准")
            self.assertEqual(likes[0]["params"]["emoji_id"], 264)

    async def test_marker_only_message_sends_nothing_but_likes(self):
        async with MockNapCatMedia(self.url, token=\"<SECRET>\") as client:
            self.adapter._last_inbound_mid["10001"] = "4242"
            await self.adapter.send("10001", "[贴表情:吃瓜]")
            self.assertEqual(client.sent_texts("send_private_msg"), [],
                             "只输出贴表情时不应发任何正文")
            likes = [a for a in client.actions if a.get("action") == "set_msg_emoji_like"]
            self.assertEqual(len(likes), 1)
            self.assertEqual(likes[0]["params"]["emoji_id"], 271)

    async def test_emoji_like_disabled_by_config(self):
        self.adapter.emoji_like_enabled = False
        async with MockNapCatMedia(self.url, token=\"<SECRET>\") as client:
            await self.adapter.send("10001", "笑死[贴表情:赞]")
            self.assertEqual([a for a in client.actions
                              if a.get("action") == "set_msg_emoji_like"], [])
            self.assertIn("[贴表情:赞]", client.sent_texts("send_private_msg")[0],
                          "关掉时标记按普通文本发出（不静默吞）")

    async def test_health_state_reports_sticker_counters(self):
        st = self.adapter.health_state()
        for k in ("stickers_enabled", "stickers_saved", "stickers_count",
                  "emoji_like_enabled", "emoji_likes", "emoji_like_failed"):
            self.assertIn(k, st)


if __name__ == "__main__":
    unittest.main(verbosity=2)
