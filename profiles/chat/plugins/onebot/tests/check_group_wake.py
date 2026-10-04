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

    def test_at_all_wakes(self):
        """`@全体成员`（qq=all）**要**把她叫醒 —— 2026-10-03 主人拍板「别人@全体也要能唤醒」。

        ⚠️ 有意反转：这条原来是 `== "none"`（理由是「不是冲她来的」）。群公告/通知
        也算信息，主人要她在场。开关是 `group_wake.AT_ALL_WAKES`。
        """
        self.assertEqual(_decide(_at("通知一下", qq="all")), "at")
        self.assertTrue(_gwk.AT_ALL_WAKES, "开关被关了，这条测试的语义就变了")

    def test_at_all_as_cq_string_wakes(self):
        ev = {"message_type": "group", "group_id": "55555", "user_id": "1",
              "message": "[CQ:at,qq=all] 通知一下", "message_id": 1}
        self.assertEqual(_decide(ev), "at")

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
        # 2026-10-03 反转：主人看到"群聊生人档"那套觉得把群里人设整刻薄了 → 拿掉，
        # 换成一句热的（"就当在朋友群里说话"）。别把这词加回来。
        self.assertIn("朋友群", text)
        self.assertNotIn("生人档", text, "别把冷规矩加回注入")
        self.assertIn("[CQ:at,qq=", text)                # 出站 @ 的写法必须教给她（2026-10-03 加）
        # 2026-10-03 反转：这一行**必须**是真群号。曾经脱敏成 `<GROUP_ID>`，而闸门正是靠它认群，
        # 结果群记忆分流静默失效（带标记=True 但认不出群）。这行只出现在她自己的回合正文里，
        # 她本来就知道自己在哪个群 → 无脱敏价值。**别改回去**（`check_group_memory_redirect`
        # 里有对应的漂移断言）。
        self.assertIn("<GROUP_ID>", text, "标记行要带真群号，闸门靠它决定写哪个群库")
        self.assertIn("群聊上下文", text)

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


class TestGatedMode(unittest.TestCase):
    """`gated` 档（「让她自己考虑」）：免费闸决定**叫不叫模型**，模型决定**开不开口**。

    守三条线：
      A. @/被回复/点名 在 gated 下依然**直通**（闸管不到它们）
      B. 闸本身是**免费纯函数**：过不了 → 连模型都不叫（这才是省钱的地方）
      C. 门控模块里**不许**出现任何 LLM / 记忆库调用（同 `TestNoMemorySink` 思路）
    """

    def setUp(self):
        self.now = 1_700_000_000.0

    # ── A. 直通不被闸影响 ──────────────────────────────────────────────
    def test_at_still_passes_in_gated(self):
        r, _ = _gwk.decide(_at(""), self_id=SELF, mode=_gwk.MODE_GATED,
                           gate_state=_gwk.GateState(), now=self.now)
        self.assertEqual(r, "at")

    def test_name_still_passes_in_gated(self):
        r, _ = _gwk.decide(_ev("棉棉在吗"), self_id=SELF, mode=_gwk.MODE_GATED,
                           gate_state=_gwk.GateState(), now=self.now)
        self.assertEqual(r, "name")

    def test_reply_still_passes_in_gated(self):
        ev = _ev("接着说")
        ev["message"] = [{"type": "reply", "data": {"id": "777"}},
                         {"type": "text", "data": {"text": " 接着说"}}]
        r, _ = _gwk.decide(ev, self_id=SELF, own_message_ids=["777"],
                           mode=_gwk.MODE_GATED, gate_state=_gwk.GateState(), now=self.now)
        self.assertEqual(r, "reply")

    # ── B. 闸：fail-closed + 该拦的拦、该放的放 ─────────────────────────
    def test_gated_fail_closed_without_state(self):
        """没状态就不唤醒（宁少说，不多花钱）。"""
        r, detail = _gwk.decide(_ev("这个报错怎么处理啊？"), self_id=SELF,
                                mode=_gwk.MODE_GATED, gate_state=None, now=self.now)
        self.assertEqual(r, "none")
        self.assertIn("fail-closed", detail)

    def test_gated_wakes_on_real_question(self):
        r, detail = _gwk.decide(_ev("这个报错怎么处理啊？"), self_id=SELF,
                                mode=_gwk.MODE_GATED, gate_state=_gwk.GateState(), now=self.now)
        self.assertEqual(r, "gate", detail)

    def test_gated_skips_low_value_chatter(self):
        for text in ("哈哈", "嗯", "😀😀😀", "666"):
            with self.subTest(text=text):
                r, _ = _gwk.decide(_ev(text), self_id=SELF, mode=_gwk.MODE_GATED,
                                   gate_state=_gwk.GateState(), now=self.now)
                self.assertEqual(r, "none", f"{text!r} 不该唤醒")

    def test_question_outranks_chatter(self):
        st = _gwk.GateState()
        q, _ = _gwk.gate_score(_ev("这个报错怎么处理啊？"), state=st, now=self.now)
        c, _ = _gwk.gate_score(_ev("哈哈"), state=st, now=self.now)
        self.assertGreater(q, c)
        self.assertGreaterEqual(q, _gwk.GATE_DEFAULT_THRESHOLD)
        self.assertLess(c, _gwk.GATE_DEFAULT_THRESHOLD)

    def test_threshold_is_respected(self):
        """阈值拉到 1.0 → 连好问题也不唤醒（配置真能省钱）。"""
        r, _ = _gwk.decide(_ev("这个报错怎么处理啊？"), self_id=SELF, mode=_gwk.MODE_GATED,
                           gate_state=_gwk.GateState(), now=self.now, gate_threshold=1.0)
        self.assertEqual(r, "none")

    # ── C. 门控信号（复读 / 闲置压力 / 自说率）─────────────────────────
    def test_dedup_ratio_high_for_repeat_low_for_unrelated(self):
        st = _gwk.GateState()
        st.note_incoming("今晚吃什么好呢")
        self.assertGreaterEqual(st.dedup_ratio("今晚吃什么好呢"), 0.9)
        self.assertLess(st.dedup_ratio("后端接口超时怎么排查"), 0.4)

    def test_idle_pressure_raises_score(self):
        text = "今天天气不错"
        cold = _gwk.GateState()
        cold.note_self_spoke(now=self.now)                 # 她刚说过 → 无闲置压力
        warm = _gwk.GateState()
        warm.note_self_spoke(now=self.now - 1800)          # 半小时没说话 → 满额补偿
        s_cold, _ = _gwk.gate_score(_ev(text), state=cold, now=self.now)
        s_warm, _ = _gwk.gate_score(_ev(text), state=warm, now=self.now)
        self.assertGreater(s_warm, s_cold)

    def test_self_ratio_penalises_talkative_her(self):
        st = _gwk.GateState()
        for _ in range(10):
            st.note_self_spoke(now=self.now)               # 最近全是她在说
        self.assertGreater(st.self_ratio(now=self.now), 0.5)
        talked, _ = _gwk.gate_score(_ev("今天天气不错"), state=st, now=self.now)
        quiet, _ = _gwk.gate_score(_ev("今天天气不错"), state=_gwk.GateState(), now=self.now)
        self.assertLess(talked, quiet)

    # ── D. 闭嘴标记 ───────────────────────────────────────────────────
    def test_silence_marker_recognised(self):
        for text in ("[SILENT]", "  [SILENT]\n", "\u200b[SILENT]\u200b"):
            with self.subTest(text=text):
                self.assertTrue(_gwk.silence_of(text))

    def test_silence_marker_does_not_eat_real_replies(self):
        for text in ("[SILENT] 但我想说这个报错得先看日志", "你说得对", ""):
            with self.subTest(text=text):
                self.assertFalse(_gwk.silence_of(text))

    # ── E. 模式注册 + 源码守卫 ────────────────────────────────────────
    def test_gated_is_a_registered_mode(self):
        self.assertIn(_gwk.MODE_GATED, _gwk.MODES)
        self.assertEqual(_gwk.parse_mode({"group_wake_mode": "gated"}), "gated")

    def test_gate_layer_never_touches_llm_or_memory(self):
        """源码级守卫：门控段里不许出现任何 LLM/记忆库/网络调用。

        这是成本红线的机器判据 —— 只要有人往闸里塞一次 recall/retain/HTTP，这里就红。
        **只看代码（AST），不看注释/文档串** —— 否则"不碰 LLM"这种说明文字自己就把守卫弄红。
        """
        import ast

        src = Path(_PLUGIN_DIR, "group_wake.py").read_text(encoding="utf-8")
        marker = "# ── gated 档：免费前置闸"
        self.assertIn(marker, src, "门控段不见了？")
        gate_src = src.split(marker, 1)[1]
        # split 落在注释行中间 —— 补一个 # 让首行仍是注释，ast 才解析得动
        tree = ast.parse("#" + gate_src)

        banned = {
            # 记忆库
            "hindsight", "retain", "recall", "reflect", "memory_client", "vectorstore",
            # LLM / agent
            "openai", "anthropic", "llm", "completion", "chat_completion", "model_call",
            "generate", "invoke", "agent", "oracle",
            # 网络
            "requests", "httpx", "aiohttp", "urllib", "urlopen", "socket",
        }
        seen = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                seen.add(node.id.lower())
            elif isinstance(node, ast.Attribute):
                seen.add(node.attr.lower())
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    seen.add(alias.name.split(".")[0].lower())
        hit = sorted(seen & banned)
        self.assertEqual(hit, [], f"门控层混进了 {hit} —— 它是免费闸，不许碰 LLM/记忆库/网络")



def _voice_ev():
    return {"post_type": "message", "message_type": "group", "group_id": "<GROUP_ID>",
            "user_id": "<OWNER_QQ>", "self_id": "<BOT_QQ>", "message_id": 7,
            "message": [{"type": "record", "data": {"file": "v.amr"}}]}


class TestGateTextOverride(unittest.TestCase):
    """2026-10-04：判定要能读**转写后**的正文（语音专用）。

    不给 text 时按老口径（原始渲染 → "[语音]" 占位）；给了就按转写文本打分。
    """

    def test_score_uses_override(self):
        ev = _voice_ev()
        st = _gwk.GateState()
        plain, why1 = _gwk.gate_score(ev, state=st, threshold=0.35)
        self.assertLess(plain, 0.35)
        self.assertNotIn("疑问", why1)
        ov, why2 = _gwk.gate_score(ev, state=st, threshold=0.35,
                                   text="[语音: 上面有写了什么东西吗]")
        self.assertGreaterEqual(ov, 0.35)
        self.assertIn("疑问", why2)

    def test_decide_uses_override(self):
        ev = _voice_ev()
        st = _gwk.GateState()
        reason, _ = _gwk.decide(ev, self_id="<BOT_QQ>", alias_names=(),
                                mode=_gwk.MODE_GATED, gate_state=st,
                                gate_threshold=0.35)
        self.assertEqual(reason, "none")
        reason2, detail2 = _gwk.decide(ev, self_id="<BOT_QQ>", alias_names=(),
                                       mode=_gwk.MODE_GATED, gate_state=st,
                                       gate_threshold=0.35,
                                       text="[语音: 上面有写了什么东西吗]")
        self.assertEqual(reason2, "gate")
        self.assertIn("过闸", detail2)

    def test_alias_hit_on_transcript_counts_as_name(self):
        """语音里念她的名字 = 点名（原来永远命中不了）。"""
        ev = _voice_ev()
        reason, _ = _gwk.decide(ev, self_id="<BOT_QQ>", alias_names=("棉棉",),
                                mode=_gwk.MODE_GATED, gate_state=_gwk.GateState(),
                                text="[语音: 棉棉你在干嘛]")
        self.assertEqual(reason, "name")

    def test_none_text_keeps_old_behaviour(self):
        """text=None 必须回落到原口径（不是空串、不是 False 之类）。"""
        ev = _voice_ev()
        st = _gwk.GateState()
        a, _ = _gwk.gate_score(ev, state=st, threshold=0.35)
        b, _ = _gwk.gate_score(ev, state=st, threshold=0.35, text=None)
        self.assertEqual(a, b)

class TestConversationState(unittest.TestCase):
    """「对话态」：她说过话后的 3 分钟里，群里**不 @ 也能叫醒她**（主人 2026-10-03 报的问题）。

    守两条线：
      A. 对话态能把一条**普通消息**顶过阈值（潜水态拦下、对话态放行）——这是「可以主动插话」的机制
      B. 它**有时间窗**，窗口外回到潜水态、仍要过免费闸；纯表情/过短即使在对话态也不唤醒
    """

    def setUp(self):
        self.now = 1_700_000_000.0
        self.text = "群聊上下文能看见几条"      # 无疑问词、长度 10 → 潜水态只有 +0.20

    def _states(self):
        # 两边都把「闲置压力」中和掉（她刚说过话），只比对话态这一项
        idle = _gwk.GateState()
        idle.note_self_spoke(now=self.now)
        conv = _gwk.GateState()
        conv.note_self_spoke(now=self.now)
        conv.note_conversation(now=self.now)
        return idle, conv

    def test_plain_message_passes_when_in_conversation(self):
        idle, conv = self._states()
        r_idle, d_idle = _gwk.decide(_ev(self.text), self_id=SELF, mode=_gwk.MODE_GATED,
                                     gate_state=idle, now=self.now)
        self.assertEqual(r_idle, "none", f"潜水态本该拦下：{d_idle}")
        r_conv, detail = _gwk.decide(_ev(self.text), self_id=SELF, mode=_gwk.MODE_GATED,
                                     gate_state=conv, now=self.now)
        self.assertEqual(r_conv, "gate", detail)
        self.assertIn("对话态", detail)

    def test_conversation_window_expires(self):
        st = _gwk.GateState()
        st.note_self_spoke(now=self.now)
        st.note_conversation(now=self.now - _gwk.CONV_WINDOW_S - 1)   # 窗口已过
        r, detail = _gwk.decide(_ev(self.text), self_id=SELF, mode=_gwk.MODE_GATED,
                                gate_state=st, now=self.now)
        self.assertEqual(r, "none", detail)

    def test_conversation_does_not_lift_low_value_chatter(self):
        _, conv = self._states()
        for text in ("哈哈", "嗯", "😀😀😀"):
            with self.subTest(text=text):
                r, _ = _gwk.decide(_ev(text), self_id=SELF, mode=_gwk.MODE_GATED,
                                   gate_state=conv, now=self.now)
                self.assertEqual(r, "none", f"{text!r} 即使在对话态也不该唤醒")

    def test_note_conversation_only_extends(self):
        st = _gwk.GateState()
        st.note_conversation(now=self.now, window_s=60)
        first = st.conv_until
        st.note_conversation(now=self.now, window_s=10)     # 更短的窗口不许把截止时间提前
        self.assertEqual(st.conv_until, first)
        self.assertTrue(st.in_conversation(now=self.now))
        self.assertFalse(st.in_conversation(now=first + 1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
