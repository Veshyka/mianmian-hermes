"""群聊 B 阶段单测：滚动窗口 + 采集口径 + **「绝不入主库」的机制守卫**。

覆盖（B 阶段验收）：
  1. `GroupWindow`：每群一份、双上限（条数 + 字节）丢最旧、落盘可重载、坏行容忍、
     写失败不炸（errors 计数）、`render_context` 带 `source=qq-group` 标签。
  2. 采集口径 `extract_window_text`：`[图片]` / `[表情]` / `[文件]` / `[语音]` 占位，
     纯表情消息**有正文**（所以能进窗口），私聊正文口径（`extract_text`）**不变**。
  3. `should_ignore(media_counts_as_text=...)`：群里纯图不丢、私聊纯图照旧丢。
  4. **机制证明**：群路径的源码里没有任何记忆写入原语（hindsight / retain / recall /
     handle_message），见 `TestNoMemorySink`。

⚠️ 文件名用 `check_` 前缀（disk-cleanup 会删 `test_*`，见 check_segmentation.py 头注）。
纯 stdlib，不需要 aiohttp / gateway（`group_window` 与 `onebot_proto` 都是零依赖纯函数）。
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))     # …/hermes_onebot
sys.path.insert(0, os.path.dirname(_PLUGIN_DIR))
_PKG = os.path.basename(_PLUGIN_DIR)

_gw = importlib.import_module(f"{_PKG}.group_window")
_gwk = importlib.import_module(f"{_PKG}.group_wake")
_proto = importlib.import_module(f"{_PKG}.onebot_proto")
GroupWindow = _gw.GroupWindow


# ── 真实事件形状（2026-09-23 用 NapCat **只读** API `get_group_msg_history` 抓的键集）──
# 值全部换成占位：这里要的是**形状**（NapCat 真实推送的字段名），不是内容。
def real_group_event(*, text="样例文本", uid="10001", gid="1095283483", name="群友甲",
                     segments=None, message_id=123456789, ts=1758600000) -> dict:
    """按 NapCat 4.18.28 实测返回的字段集构造群消息事件（一字不差的键名）。"""
    return {
        "self_id": <BOT_QQ>, "user_id": int(uid), "time": ts, "message_id": message_id,
        "message_seq": message_id, "real_id": message_id, "real_seq": str(message_id),
        "message_type": "group", "sender": {"user_id": int(uid), "nickname": name,
                                            "card": "", "role": "member"},
        "raw_message": text, "font": 14, "sub_type": "normal",
        "message": segments if segments is not None else [{"type": "text", "data": {"text": text}}],
        "message_format": "array", "post_type": "message", "group_id": int(gid),
        "group_name": "样例群",
    }


class TestGroupWindow(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="onebot-gwin-")
        self.root = Path(self._tmp.name) / "onebot-groups"

    def tearDown(self):
        self._tmp.cleanup()

    def test_append_tail_roundtrip_and_per_group_isolation(self):
        w = GroupWindow(self.root)
        w.append("111", text="甲说的话", uid="10001", name="甲")
        w.append("111", text="甲又一句", uid="10001", name="甲")
        w.append("222", text="乙在另一个群", uid="20002", name="乙")

        self.assertEqual([r["text"] for r in w.tail("111")], ["甲说的话", "甲又一句"])
        self.assertEqual([r["text"] for r in w.tail("222")], ["乙在另一个群"])
        rec = w.tail("111")[0]
        self.assertEqual(rec["name"], "甲")
        self.assertEqual(rec["uid"], "10001")
        self.assertIn("t", rec)                      # 时间戳（给她的材料要带时间）
        self.assertTrue(w.path("111").is_file())
        self.assertEqual(w.path("111").name, "111.jsonl")

    def test_limits_drop_oldest_by_count(self):
        w = GroupWindow(self.root, max_msgs=5)
        for i in range(12):
            w.append("111", text=f"第{i}条", uid="1", name="甲")
        recs = w.tail("111")
        self.assertEqual(len(recs), 5)
        self.assertEqual(recs[-1]["text"], "第11条")
        self.assertEqual(recs[0]["text"], "第7条")
        self.assertEqual(w.trimmed, 7)
        self.assertEqual(w.stats()["msgs"], 5)       # 落盘的也只有 5 条

    def test_limits_drop_oldest_by_bytes(self):
        w = GroupWindow(self.root, max_msgs=1000, max_bytes=4096)
        for i in range(200):
            w.append("111", text="长" * 100, uid="1", name="甲")
        data = w.path("111").read_bytes()
        self.assertLessEqual(len(data), 4096, "总字节上限必须守住（别把磁盘吃光）")
        self.assertGreater(w.trimmed, 0)
        self.assertTrue(w.tail("111"), "字节上限也不该把窗口清空")

    def test_persists_across_instances(self):
        GroupWindow(self.root).append("111", text="跨实例也要在", uid="1", name="甲")
        w2 = GroupWindow(self.root)
        self.assertEqual([r["text"] for r in w2.tail("111")], ["跨实例也要在"])
        self.assertEqual(w2.groups(), ["111"])

    def test_tolerates_corrupt_line(self):
        w = GroupWindow(self.root)
        w.append("111", text="好的一条", uid="1", name="甲")
        with w.path("111").open("a", encoding="utf-8") as fh:
            fh.write('{"ts": 1, "tex')          # 半行（进程被杀留下的）
        w2 = GroupWindow(self.root)
        self.assertEqual([r["text"] for r in w2.tail("111")], ["好的一条"])

    def test_write_failure_does_not_raise_and_counts(self):
        blocked = Path(self._tmp.name) / "not-a-dir"
        blocked.write_text("我是文件不是目录", encoding="utf-8")
        w = GroupWindow(blocked)                 # 把「目录」指到一个文件 → 落盘必失败
        rec = w.append("111", text="写不进去的一条", uid="1", name="甲")
        self.assertIsNone(rec)                   # 不炸，只返回 None
        self.assertGreater(w.errors, 0)          # 记账，心跳里会报提醒
        self.assertIsInstance(w.summary(), str)

    def test_empty_text_not_recorded(self):
        w = GroupWindow(self.root)
        self.assertIsNone(w.append("111", text="   ", uid="1", name="甲"))
        self.assertEqual(w.appended, 0)

    def test_render_context_carries_source_label_and_lines(self):
        w = GroupWindow(self.root)
        w.append("1095283483", text="吃了吗", uid="10001", name="甲", ts=1758600000)
        w.append("1095283483", text="[图片]", uid="20002", name="乙", ts=1758600060)
        ctx = w.render_context("1095283483")
        self.assertIn("source=qq-group", ctx)    # C 阶段注入提示词时的来源标签
        # ⚠️ 2026-10-03 有意变更：行里带上了 QQ 号（`甲(QQ:10001): …`）——
        # 她要 @ 人必须知道对方号码（出站 @ = 正文里写 `[CQ:at,qq=…]`）。
        self.assertIn("甲(QQ:10001): 吃了吗", ctx)
        self.assertIn("乙(QQ:20002): [图片]", ctx)
        self.assertIn("群10***83", ctx)          # 群号脱敏
        self.assertLess(len(ctx.splitlines()[0]), 200)

    def test_summary_shape(self):
        w = GroupWindow(self.root)
        w.append("111", text="a", uid="1", name="甲")
        w.append("222", text="b", uid="2", name="乙")
        s = w.summary()
        self.assertIn("2 群/2 条", s)
        self.assertIn("未入主库", s)

    def test_gid_cannot_escape_root(self):
        w = GroupWindow(self.root)
        p = w.path("../../etc/passwd")
        self.assertEqual(p.parent, self.root)
        self.assertNotIn("/", p.name.replace(".jsonl", ""))


    def test_delta_since_returns_only_new_records(self):
        """增量口径：只给游标之后的新消息（跨轮不重复发）。"""
        w = GroupWindow(self.root)
        w.append("777", text="旧1", uid="1", name="甲", ts=1000)
        w.append("777", text="旧2", uid="1", name="甲", ts=1001)
        cursor = w.tail("777", limit=1)[0]
        w.append("777", text="新1", uid="2", name="乙", ts=1002)
        w.append("777", text="新2", uid="2", name="乙", ts=1003)
        fresh = w.delta_since("777", cursor)
        self.assertEqual([r["text"] for r in fresh], ["新1", "新2"])

    def test_delta_since_without_cursor_falls_back_to_tail(self):
        """首次唤醒 / 刚重启：游标是空的 → 先给一段底稿（不是空手）。"""
        w = GroupWindow(self.root)
        for i in range(5):
            w.append("777", text=f"m{i}", uid="1", name="甲", ts=1000 + i)
        self.assertEqual([r["text"] for r in w.delta_since("777", None, limit=3)],
                         ["m2", "m3", "m4"])

    def test_delta_since_pruned_cursor_falls_back_to_tail(self):
        """游标那条已被窗口淘汰：退回窗口口径，宁可多给也不让她断片。"""
        w = GroupWindow(self.root, max_msgs=2)
        w.append("777", text="最早", uid="1", name="甲", ts=1000)
        cursor = w.tail("777", limit=1)[0]
        w.append("777", text="中", uid="1", name="甲", ts=1001)
        w.append("777", text="新", uid="1", name="甲", ts=1002)   # 淘汰"最早"
        fresh = w.delta_since("777", cursor, limit=10)
        self.assertEqual([r["text"] for r in fresh], ["中", "新"], "该退化成 tail")

    def test_delta_since_respects_limit(self):
        """久未唤醒：一次最多灌 limit 条（取最新的那几条）。"""
        w = GroupWindow(self.root)
        w.append("777", text="底稿", uid="1", name="甲", ts=1000)
        cursor = w.tail("777", limit=1)[0]
        for i in range(6):
            w.append("777", text=f"n{i}", uid="1", name="甲", ts=1001 + i)
        fresh = w.delta_since("777", cursor, limit=2)
        self.assertEqual([r["text"] for r in fresh], ["n4", "n5"])

    def test_render_records_marks_delta_head(self):
        w = GroupWindow(self.root)
        w.append("777", text="你好", uid="1", name="甲", ts=1000)
        recs = w.tail("777", limit=1)
        self.assertIn("群聊新增", w.render_records("777", recs, delta=True))
        self.assertIn("群聊上下文", w.render_records("777", recs, delta=False))


class TestWindowText(unittest.TestCase):
    def test_placeholders_for_media(self):
        evt = real_group_event(segments=[
            {"type": "text", "data": {"text": "看这个"}},
            {"type": "image", "data": {"file": "x.jpg"}},
            {"type": "face", "data": {"id": "14"}},
            {"type": "mface", "data": {"emoji_id": "1"}},
            {"type": "record", "data": {"file": "a.silk"}},
            {"type": "at", "data": {"qq": "<BOT_QQ>"}},
            {"type": "reply", "data": {"id": "9"}},
        ])
        self.assertEqual(_proto.extract_window_text(evt),
                         # 2026-09-23 第二批：表情占位**带上 id**（`face` 的 id、`mface` 的
                         # 包/id）—— 只写 `[表情]` 时模型分不清是哪个表情，等于噪声。
                         "看这个[图片][表情:14][表情包:1][语音][@<BOT_QQ>]")

    def test_pure_emoji_message_has_text(self):
        """纯表情消息现在**两个口径都有正文**（2026-09-23 行为变更，见 `extract_text` 文档）。

        旧行为：窗口口径 `[表情]`、投递口径空串 → 私聊只发表情会被 `should_ignore` 判
        `no_text` 整条丢掉，她连"对方发了个表情"都不知道。
        **2026-09-23 第二批**：占位再带上 id → `[表情:14]`。
        """
        evt = real_group_event(segments=[{"type": "face", "data": {"id": "14"}}])
        self.assertEqual(_proto.extract_window_text(evt), "[表情:14]")
        self.assertEqual(_proto.extract_text(evt), "[表情:14]")

    def test_private_text_path_unchanged(self):
        """文字照样原样出来；表情**不再被吃掉**且带 id（9-23 两批变更）。"""
        evt = real_group_event(segments=[
            {"type": "text", "data": {"text": "你好"}},
            {"type": "face", "data": {"id": "14"}},
        ])
        self.assertEqual(_proto.extract_text(evt), "你好[表情:14]")

    def test_real_event_shape_is_accepted(self):
        evt = real_group_event(text="真形状的一条")
        self.assertEqual(_proto.extract_window_text(evt), "真形状的一条")
        self.assertEqual(_proto.extract_text(evt), "真形状的一条")
        self.assertEqual(_proto.group_id(evt), "1095283483")
        self.assertEqual(_proto.sender_name(evt), "群友甲")


class TestShouldIgnoreMedia(unittest.TestCase):
    """采集口径与投递口径**已经收敛**（2026-09-23 起两条链共用 `_render`）。

    历史背景：以前 `extract_text` 对 `face`/`mface` 是空串、窗口口径是 `[表情]`，
    于是「只发了个表情」的群消息进不了投递路径，私聊更惨 —— 整条被 `no_text` 丢掉。
    现在两种口径一致：**任何消息段都有正文**（媒体落占位、没见过的段落兜底），
    所以 `no_text` 只会出现在真正空的消息上。
    """

    def test_group_face_only_is_collected(self):
        evt = real_group_event(segments=[{"type": "face", "data": {"id": "14"}}])
        self.assertIsNone(_proto.should_ignore(evt, media_counts_as_text=True))
        self.assertIsNone(_proto.should_ignore(evt))              # 口径已收敛，不再落 no_text

    def test_private_face_only_is_no_longer_swallowed(self):
        """私聊只发一个表情 → 以前 `no_text` 整条消失；现在带 id 到她面前（`[表情:14]`）。"""
        evt = real_group_event(segments=[{"type": "face", "data": {"id": "14"}}])
        evt["message_type"] = "private"
        evt.pop("group_id", None)
        self.assertIsNone(_proto.should_ignore(evt))
        self.assertEqual(_proto.extract_text(evt), "[表情:14]")

    def test_super_emoji_is_visible_both_ways(self):
        """★ 这次的真病根：QQ「超级表情」（marketface）原来全仓零命中 → 静默丢弃。

        9-23 第二批起带 id（`emoji_id` 优先于 `summary`）——`summary` 只在没有 id 时兜底。
        """
        evt = real_group_event(segments=[{
            "type": "marketface",
            "data": {"emoji_id": "abc", "emoji_package_id": "1",
                     "key": "k", "summary": "[超级表情]"},
        }])
        self.assertEqual(_proto.extract_window_text(evt), "[超级表情:abc]")
        self.assertEqual(_proto.extract_text(evt), "[超级表情:abc]")
        self.assertIsNone(_proto.should_ignore(evt))

    def test_unknown_segment_is_not_silently_dropped(self):
        """没见过的段也要留痕 —— 「静默丢」才是这次 bug 的本体。"""
        evt = real_group_event(segments=[{"type": "未来新段", "data": {}}])
        self.assertEqual(_proto.extract_text(evt), "[未支持的消息段:未来新段]")

    def test_unknown_segment_next_to_text_keeps_the_text(self):
        evt = real_group_event(segments=[
            {"type": "text", "data": {"text": "看这个"}},
            {"type": "未来新段", "data": {}},
        ])
        self.assertEqual(_proto.extract_text(evt), "看这个[未支持的消息段:未来新段]")

    def test_group_image_only_kept_both_ways(self):
        evt = real_group_event(segments=[{"type": "image", "data": {"file": "x.jpg"}}])
        self.assertIsNone(_proto.should_ignore(evt))
        self.assertIsNone(_proto.should_ignore(evt, media_counts_as_text=True))

    def test_non_message_events_still_ignored(self):
        evt = real_group_event()
        evt["post_type"] = "meta_event"
        self.assertEqual(_proto.should_ignore(evt, media_counts_as_text=True),
                         "not_a_message_event")

    def test_qq_guanjia_still_ignored(self):
        evt = real_group_event(uid="2854196310")
        self.assertEqual(_proto.should_ignore(evt, media_counts_as_text=True), "qq_guanjia")


class TestNoMemorySink(unittest.TestCase):
    """★ 机制证明：群消息这条路**没有任何记忆写入原语**（不是靠自觉，是源码级守卫）。

    为什么这么测：Hindsight 的 `auto_retain` 挂在 **agent 回合**上，而群消息在
    `_ingest` 里就 return 了（不到 `handle_message`）→ 结构上不可能入库。
    这里再把「窗口模块 / 采集函数」的源码摊开，逐词证明里面连拼写都没有。
    """

    FORBIDDEN = ("hindsight", "retain", "recall", "memory", "memori", "embedding")

    @staticmethod
    def _code_identifiers(path: str) -> set:
        """模块里**真实代码**用到的标识符（导入名 / 类名 / 函数名 / 属性名）。

        故意用 AST 而不是全文正则：注释与 docstring 里为了讲清红线，本来就会提到
        「memory / retain」这些词 —— 要证明的是**代码里没有这些调用**，不是「文件里没这些字」。
        """
        import ast
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        names: set = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names.add((node.module or "").split(".")[0])
                names |= {a.name for a in node.names}
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Name):
                    names.add(f.id)
                elif isinstance(f, ast.Attribute):
                    names.add(f.attr)
        return {n for n in names if n}

    def test_group_window_module_has_no_memory_primitives(self):
        for path in (_gw.__file__, _proto.__file__):
            ids = self._code_identifiers(path)
            hits = sorted({w for w in self.FORBIDDEN
                           for n in ids if w in n.lower()})
            self.assertEqual(hits, [],
                             f"{Path(path).name} 的代码里出现了记忆原语：{hits}")

    def test_collect_group_calls_neither_agent_nor_memory(self):
        adapter_mod = importlib.import_module(f"{_PKG}.adapter")
        import inspect
        src = inspect.getsource(adapter_mod.OneBotAdapter._collect_group).lower()
        for bad in ("handle_message", "retain", "recall", "hindsight"):
            self.assertNotIn(bad, src, f"_collect_group 里出现了 {bad} —— 群路径不许碰这些")

    def test_group_window_package_has_no_network_or_db_imports(self):
        src = Path(_gw.__file__).read_text(encoding="utf-8")
        for mod in ("sqlite3", "requests", "aiohttp", "urllib", "hindsight_client"):
            self.assertNotIn(f"import {mod}", src, f"窗口模块不该依赖 {mod}")


class TestAdapterWiring(unittest.TestCase):
    """适配器侧接线：默认必须是「采集开、唤醒只在被 @ 时」（C1 红线靠这条默认值守）。"""

    def test_defaults(self):
        adapter_mod = importlib.import_module(f"{_PKG}.adapter")
        import inspect
        src = inspect.getsource(adapter_mod)
        self.assertIn('extra.get("group_collect_enabled"), True', src,
                      "group_collect_enabled 默认 True（只看不说：看要看得见）")
        # C1：唤醒模式由 `group_wake_mode` 唯一决定，**没有**默认 true 的唤醒开关
        self.assertIn("self.group_wake_mode = _gwk.parse_mode(extra)", src,
                      "唤醒模式必须走 group_wake.parse_mode（三态 + 老键映射）")
        self.assertNotIn('extra.get("group_wake_enabled"), True', src,
                         "不许有默认打开的 group_wake_enabled（那就是裸开全员唤醒）")
        self.assertFalse(_gwk.parse_mode({}) != _gwk.MODE_COLLECT,
                         "默认档必须是 collect-only（B 阶段语义不动）")
        self.assertEqual(_gwk.parse_mode({"group_wake_mode": "mention-only"}), "mention-only")
        self.assertEqual(_gwk.parse_mode({"group_wake_enabled": True}), "full",
                         "老键 true → full（老语义），老键 false/缺省 → collect-only")
        self.assertEqual(_gwk.parse_mode({"group_wake_enabled": False}), "collect-only")
        self.assertEqual(_gwk.parse_mode({"group_wake_mode": "垃圾值"}), "collect-only",
                         "模式键写坏 → **回落到不唤醒**（fail-closed）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
