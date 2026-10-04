"""主动私聊斜坡的钉子（2026-10-03 主人定稿的机制）。

口径（改行为前先读 `proactive.py` 头注 + 本文件）：
  * 每 10 分钟一格，p = 格数 × 3%；醒着才涨。
  * 静默窗 00:30–07:00：不涨、不触发，但**概率不清空**。
  * 任何对话（他说话 / 她说话）→ 归零；触发 → 也归零；**没有冷却**。
  * 纯脚本：没掷中不叫模型。
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import random
import sys
import time
import types
import unittest
from pathlib import Path
from typing import Any, Dict, List

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(_PLUGIN_DIR))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/opt/hermes")

from gateway.config import PlatformConfig  # noqa: E402
from gateway.platform_registry import PlatformEntry, platform_registry  # noqa: E402

_pro = importlib.import_module("onebot.proactive")
_clock = importlib.import_module("onebot.onebot_time")
_gw = importlib.import_module("onebot.group_window")
_adapter_mod = importlib.import_module("onebot.adapter")
OneBotAdapter = _adapter_mod.OneBotAdapter

if not platform_registry.is_registered("onebot"):
    platform_registry.register(PlatformEntry(
        name="onebot", label="OneBot v11", adapter_factory=OneBotAdapter,
        check_fn=lambda: True, source="plugin", plugin_name="hermes_onebot"))

QUIET = _pro.parse_quiet("00:30-07:00")


def _ts(y: int, mo: int, d: int, h: int, mi: int) -> float:
    """**北京时刻** → epoch。容器跑在 UTC，所以绝不能用 time.mktime（那是 UTC 时刻）。"""
    return _clock.epoch(y, mo, d, h, mi)


class TestQuietWindow(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(_pro.parse_quiet("00:30-07:00"), ((0, 30), (7, 0)))
        self.assertEqual(_pro.parse_quiet("垃圾"), _pro.DEFAULT_QUIET, "写错要回默认，不许崩")

    def test_wrap_around_midnight(self):
        q = _pro.parse_quiet("23:00-07:00")
        r = _pro.ProactiveRamp(quiet=q)
        self.assertTrue(r.in_quiet(_ts(2026, 10, 3, 23, 30)))
        self.assertTrue(r.in_quiet(_ts(2026, 10, 4, 6, 0)))
        self.assertFalse(r.in_quiet(_ts(2026, 10, 4, 12, 0)))

    def test_default_window_edges(self):
        r = _pro.ProactiveRamp(quiet=QUIET)
        self.assertFalse(r.in_quiet(_ts(2026, 10, 3, 0, 29)))
        self.assertTrue(r.in_quiet(_ts(2026, 10, 3, 0, 30)))
        self.assertTrue(r.in_quiet(_ts(2026, 10, 4, 6, 59)))
        self.assertFalse(r.in_quiet(_ts(2026, 10, 4, 7, 0)))


class TestBeijingClock(unittest.TestCase):
    """2026-10-04 事故：容器是 UTC，静默窗用 time.localtime() → 实际落在北京 08:30–15:00，
    她凌晨 4:34 / 5:44 / 6:05 连发了 3 条（主人：「晚上好像没暂停」）。"""

    def setUp(self):
        self.r = _pro.ProactiveRamp(quiet=_pro.parse_quiet("00:30-07:00"))

    def test_verdict_does_not_depend_on_container_tz(self):
        """静默窗判定必须与容器 TZ 无关。

        2026-10-04 事故时容器是 UTC（`time.localtime` 差 8 小时）；当天晚些时候容器本身
        也改成了 Asia/Shanghai。不管哪种，`in_quiet()` 的答案都得一样 —— 所以这里
        只断言北京语义，不再断言"环境是 UTC"。
        """
        e = _clock.epoch(2026, 10, 4, 4, 34)
        self.assertEqual(_clock.at(e).hour, 4, "epoch ↔ 北京时刻换算坏了")
        self.assertTrue(self.r.in_quiet(e), "北京 04:34 必须是静默窗")
        self.assertFalse(self.r.in_quiet(_clock.epoch(2026, 10, 4, 12, 0)))

    def test_the_incident_hours_are_quiet_in_beijing(self):
        for hh, mm in ((4, 34), (5, 44), (6, 5)):
            self.assertTrue(self.r.in_quiet(_clock.epoch(2026, 10, 4, hh, mm)),
                            f"北京 {hh:02d}:{mm:02d} 必须算静默窗（她那次就是这时候发的）")

    def test_boundaries_are_beijing(self):
        self.assertFalse(self.r.in_quiet(_clock.epoch(2026, 10, 4, 0, 29)))
        self.assertTrue(self.r.in_quiet(_clock.epoch(2026, 10, 4, 0, 30)))
        self.assertTrue(self.r.in_quiet(_clock.epoch(2026, 10, 4, 6, 59)))
        self.assertFalse(self.r.in_quiet(_clock.epoch(2026, 10, 4, 7, 0)))
        self.assertFalse(self.r.in_quiet(_clock.epoch(2026, 10, 4, 15, 0)),
                         "下午 3 点要是算静默窗，说明又回到 UTC 了")

    def test_text_and_day_use_beijing(self):
        e = _clock.epoch(2026, 10, 4, 5, 30)
        st = _pro.RampState(ticks=3, last_activity_ts=e - 3600, last_tick_ts=e - 600)
        txt = self.r.build_text(st, e)
        self.assertIn('05:30', txt, '注入里的现在几点要是北京 05:30')
        self.r.note_fire(st, e)
        self.assertEqual(st.day, "2026-10-04", "跨日计数按北京算")

    def test_group_window_stamp_is_beijing(self):
        import tempfile
        w = _gw.GroupWindow(Path(tempfile.mkdtemp()) / "g.jsonl")
        w.append("<GROUP_ID2>", uid="<OWNER_QQ>", name="<OWNER_NICK>", text="哈气来")
        txt = w.render_context("<GROUP_ID2>")
        self.assertIn(_clock.hhmm(), txt, "窗口时间戳得是北京时间（原来差 8 小时）")
        if time.localtime().tm_hour != _clock.now().hour:      # 容器是 UTC，这条必定不同
            self.assertNotIn(time.strftime("%H:%M"), txt,
                             "窗口里出现了 UTC 的钟点 → 又用回 time.localtime 了")


class TestRamp(unittest.TestCase):
    """主人的原话逐条钉住。"""

    def setUp(self):
        self.r = _pro.ProactiveRamp(step_pct=3.0, tick_min=10.0, quiet=QUIET,
                                    rnd=random.Random(1))

    def _st(self, t0: float) -> _pro.RampState:
        return _pro.RampState(last_activity_ts=t0, last_tick_ts=t0)

    def test_ten_minutes_one_grid_default_shape(self):
        """现役默认形状 = 二次（主人 2026-10-04「预期 2.5h 一条」）。"""
        t0 = _ts(2026, 10, 3, 12, 0)          # 白天，不在静默窗
        st = self._st(t0)
        self.r.advance(st, t0 + 600)
        self.assertEqual(st.ticks, 1)
        self.assertAlmostEqual(self.r.prob(st), (1 / 30) ** 2)     # 第一格 0.1%
        self.r.advance(st, t0 + 3000)          # 再过 40 分钟 = 第 5 格
        self.assertEqual(st.ticks, 5)
        self.assertAlmostEqual(self.r.prob(st), (5 / 30) ** 2)     # 2.8%

    def test_expected_wait_is_about_two_and_a_half_hours(self):
        """2.5h 一条：前一小时基本不开口，2.5h 时 25%，5h 保底。"""
        r = _pro.ProactiveRamp(tick_min=10.0, quiet=QUIET)         # 默认二次 + 5h
        # 格数 → 分钟：6 格=1h、15 格=2h30m、19 格=3h10m（别用 hours*6 取整，会差一格）
        for ticks, expect in ((6, (6 / 30) ** 2), (15, 0.25), (19, (19 / 30) ** 2)):
            st = _pro.RampState(ticks=ticks)
            self.assertAlmostEqual(r.prob(st), expect, places=4,
                                   msg=f"{ticks * 10} 分钟的概率不对")
        st = _pro.RampState(ticks=30)                              # 5h → 100%
        self.assertAlmostEqual(r.prob(st), 1.0)
        h6 = _pro.RampState(ticks=6)
        self.assertLess(r.prob(h6), 0.05, "一小时内开口的概率要很小（不然又变成一小时一条）")

    def test_linear_shape_still_available(self):
        """2026-10-03 的原口径留着：一句话改回 linear。"""
        r = _pro.ProactiveRamp(step_pct=3.0, tick_min=10.0, quiet=QUIET,
                               ramp_shape="linear")
        st = _pro.RampState(ticks=1)
        self.assertAlmostEqual(r.prob(st), 0.03)
        st = _pro.RampState(ticks=5)
        self.assertAlmostEqual(r.prob(st), 0.15)
        st = _pro.RampState(ticks=34)
        self.assertAlmostEqual(r.prob(st), 1.0)

    def test_ramp_hours_knob(self):
        """保底时刻跟 ramp_hours 走：4h → 24 格满。"""
        r = _pro.ProactiveRamp(tick_min=10.0, quiet=QUIET, ramp_hours=4.0)
        self.assertAlmostEqual(r.prob(_pro.RampState(ticks=12)), 0.25)
        self.assertAlmostEqual(r.prob(_pro.RampState(ticks=24)), 1.0)

    def test_unknown_shape_falls_back_to_quadratic(self):
        r = _pro.ProactiveRamp(tick_min=10.0, quiet=QUIET, ramp_shape="三次方")
        self.assertEqual(r.ramp_shape, "quadratic")

    def test_probability_is_capped_at_100(self):
        t0 = _ts(2026, 10, 3, 12, 0)
        st = self._st(t0)
        self.r.advance(st, t0 + 4000 * 60)     # 涨到远超 100%
        self.assertAlmostEqual(self.r.prob(st), 1.0)
        self.assertTrue(self.r.roll(st, t0 + 4000 * 60), "到 100% 必须触发（保底）")

    def test_quiet_hours_pause_but_do_not_clear(self):
        """主人原话：昨天 00:30 到 40%，第二天 7:00 从 40% 开始。"""
        r = _pro.ProactiveRamp(step_pct=3.0, tick_min=10.0, quiet=QUIET,
                               ramp_shape="linear")   # 用线性口径算「40%」这个例子更直观
        t0 = _ts(2026, 10, 3, 19, 0)
        st = self._st(t0)
        r.advance(st, _ts(2026, 10, 3, 21, 5))          # 醒着 2h05 → 12 格
        before = st.ticks
        r.advance(st, _ts(2026, 10, 4, 0, 30))          # 到静默窗起点（00:30 之前都算醒着）
        at_quiet = st.ticks
        self.assertGreater(at_quiet, before)
        r.advance(st, _ts(2026, 10, 4, 7, 0))           # 静默窗里一格都不涨
        self.assertEqual(st.ticks, at_quiet, "静默窗不该涨概率")
        self.assertAlmostEqual(r.prob(st), min(1.0, at_quiet * 3 / 100.0),
                               msg="静默窗前后概率必须一样（不清空）")
        r.advance(st, _ts(2026, 10, 4, 7, 20))          # 07:00 之后接着涨
        self.assertEqual(st.ticks, at_quiet + 2)

    def test_exact_owner_example_40_percent_survives_the_night(self):
        r = _pro.ProactiveRamp(step_pct=5.0, tick_min=10.0, quiet=QUIET,
                               ramp_shape="linear")   # 用 5% 凑出 40%（主人的原例子是线性口径）
        st = _pro.RampState(ticks=8,                      # 8 格 × 5% = 40%
                            last_activity_ts=_ts(2026, 10, 3, 23, 10),
                            last_tick_ts=_ts(2026, 10, 4, 0, 30))   # 00:30 停在 40%
        r.advance(st, _ts(2026, 10, 4, 7, 0))
        self.assertEqual(st.ticks * 5, 40, "醒着前停在 40% → 第二天 7:00 还是 40%")
        r.advance(st, _ts(2026, 10, 4, 7, 20))            # 7 点后接着从 40% 往上爬
        self.assertEqual(st.ticks * 5, 50)

    def test_activity_resets_to_zero(self):
        """「期间发生对话就重置到 0%」。"""
        t0 = _ts(2026, 10, 3, 12, 0)
        st = self._st(t0)
        self.r.advance(st, t0 + 3600)
        self.assertGreater(st.ticks, 0)
        self.r.on_activity(st, t0 + 3700)
        self.assertEqual(st.ticks, 0)
        self.assertAlmostEqual(self.r.prob(st), 0.0)
        self.assertFalse(self.r.roll(st, t0 + 3700), "刚聊过不该立刻又开口")

    def test_fire_resets_and_counts_the_day(self):
        t0 = _ts(2026, 10, 3, 12, 0)
        st = self._st(t0)
        self.r.advance(st, t0 + 6000)
        self.r.note_fire(st, t0 + 6000)
        self.assertEqual(st.ticks, 0, "触发要归零（主人：触发后就归零）")
        self.assertEqual(st.fires_today, 1)
        self.assertEqual(st.last_self_ts, t0 + 6000)

    def test_no_cooldown_is_deliberate(self):
        """主人明确不要冷却 —— 归零之后就该按 3% 重新爬，不许偷偷加地板。"""
        r = _pro.ProactiveRamp(step_pct=3.0, tick_min=10.0, quiet=QUIET,
                               rnd=random.Random(0))
        st = self._st(_ts(2026, 10, 3, 12, 0))
        r.note_fire(st, _ts(2026, 10, 3, 12, 0) + 3600)
        st.ticks = 34                          # 二次形状下 30 格就 100%；34 格必掷中
        self.assertTrue(r.roll(st, time.time()))

    def test_awake_minutes_excludes_quiet(self):
        r = _pro.ProactiveRamp(quiet=QUIET)
        got = r.awake_minutes(_ts(2026, 10, 3, 23, 0), _ts(2026, 10, 4, 8, 0))
        self.assertAlmostEqual(got, 90 + 60, delta=2)   # 23:00–00:30 是 90 分，07:00–08:00 是 60 分

    def test_text_carries_the_numbers(self):
        st = self._st(_ts(2026, 10, 3, 20, 0))
        r = _pro.ProactiveRamp(step_pct=3.0, tick_min=10.0, quiet=QUIET)
        r.advance(st, _ts(2026, 10, 3, 21, 0))
        txt = r.build_text(st, _ts(2026, 10, 3, 21, 0))
        self.assertIn("source=qq-dm-proactive", txt)
        self.assertIn("是你自己想起他了", txt)
        self.assertIn("p=", txt, "主人要求她能看见自己在多少概率被掷中")
        self.assertIn("小时", txt, "要告诉她沉默多久")
        self.assertIn("[SILENT]", txt, "得给她不说话的出口")
        self.assertIn("你第 1 次主动开口", txt)


class TestAdapterWiring(unittest.TestCase):
    """接进适配器之后：谁让它归零、谁把它叫醒、她能不能闭嘴。"""

    class _Fake:
        def __init__(self):
            self.sent: List[Dict[str, Any]] = []

        async def call(self, payload, **kw):
            self.sent.append(payload)
            return {"status": "ok", "retcode": 0}

    def _adapter(self):
        extra = {"proactive_dm_enabled": True, "proactive_dm_step_pct": 3,
                 "proactive_dm_tick_min": 10, "proactive_dm_quiet": "00:30-07:00",
                 "state_path": "/tmp/onebot-proactive-test-state.json"}
        a = OneBotAdapter(PlatformConfig(enabled=True, extra=extra))
        a.proactive_path = Path("/tmp/onebot-proactive-test.json")
        a._proactive.clear()
        a.read_only = False
        a._client_connected = True          # 不然 send() 直接以「没连上」拒发，测不到真实路径
        a._ws = types.SimpleNamespace(closed=False)   # send() 还要看 ws 在不在
        self.dispatched: List[Any] = []
        self.frames: List[Any] = []

        async def _fake_dispatch(text, event, *, is_group, gid, uid, trigger=""):
            self.dispatched.append((text, is_group, trigger, uid))

        async def _fake_action(payload, **kw):
            self.frames.append(payload)
            return {"status": "ok", "retcode": 0}

        a._dispatch = _fake_dispatch        # type: ignore[assignment]
        a._call_action = _fake_action       # type: ignore[assignment]
        return a

    def test_his_message_resets_the_ramp(self):
        a = self._adapter()
        a._proactive["<OWNER_QQ>"] = _pro.RampState(ticks=20, last_activity_ts=time.time() - 3600,
                                                    last_tick_ts=time.time() - 3600)
        a._proactive_activity("<OWNER_QQ>")
        self.assertEqual(a._proactive["<OWNER_QQ>"].ticks, 0)

    def test_trigger_wakes_her_once_with_the_marker(self):
        a = self._adapter()
        now = time.time()
        a._proactive["<OWNER_QQ>"] = _pro.RampState(ticks=40, last_activity_ts=now - 7200,
                                                    last_tick_ts=now - 700)
        a.proactive.in_quiet = lambda now: False        # 跟测试跑的钟点无关
        asyncio.run(a._proactive_check())
        self.assertEqual(len(self.dispatched), 1, "掷中就该叫醒她一次")
        text, is_group, trigger, uid = self.dispatched[0]
        self.assertFalse(is_group, "主动私聊不能走群链路")
        self.assertEqual(trigger, "proactive")
        self.assertEqual(uid, "<OWNER_QQ>")
        self.assertIn("source=qq-dm-proactive", text)
        self.assertEqual(a._proactive["<OWNER_QQ>"].ticks, 0, "触发要归零")
        self.assertEqual(a._proactive_turn[0], "<OWNER_QQ>")

    def test_no_trigger_during_quiet_hours(self):
        a = self._adapter()
        now = time.time()
        a._proactive["<OWNER_QQ>"] = _pro.RampState(ticks=40, last_activity_ts=now - 7200,
                                                    last_tick_ts=now - 700)
        a.proactive.in_quiet = lambda now: True          # 假装现在是静默窗
        asyncio.run(a._proactive_check())
        self.assertEqual(self.dispatched, [], "静默窗里不许触发")
        self.assertEqual(a._proactive["<OWNER_QQ>"].ticks, 40, "但概率要留着")

    def test_never_fires_into_a_group(self):
        """主人 2026-10-03：「群的保持原样就好，这个主动起头不往群搞」——群号必须被挡。"""
        a = self._adapter()
        now = time.time()
        a.group_ids.add("<GROUP_ID>")
        a._proactive["<GROUP_ID>"] = _pro.RampState(ticks=40, last_activity_ts=now - 7200,
                                                    last_tick_ts=now - 700)
        a.proactive.in_quiet = lambda now: False
        asyncio.run(a._proactive_check())
        self.assertEqual(self.dispatched, [], "主动起头疑似往群里派发了（主人明确禁止）")
        self.assertEqual(a._proactive["<GROUP_ID>"].ticks, 40, "群号那格不该被动过")

    def test_group_list_learned_and_bad_proactive_entry_pruned(self):
        """2026-10-04 事故：群「<GROUP_NAME>」被当成私聊记进了主动名单，她朝它发「主动私聊」→ 1200 连发失败。

        修法：连上就向协议端要一次群号名单（权威），比「收到群消息才学」早；顺手把名单里混进的群号清掉。
        """
        a = self._adapter()

        async def _fake(payload, **kw):
            if payload.get("action") == "get_group_list":
                return {"status": "ok", "retcode": 0,
                        "data": [{"group_id": <GROUP_ID>}, {"group_id": <GROUP_ID2>}]}
            if payload.get("action") == "get_friend_list":
                return {"status": "ok", "retcode": 0, "data": [{"user_id": <OWNER_QQ>}]}
            return {"status": "ok", "retcode": 0}

        a._call_action = _fake
        now = time.time()
        a._proactive["<GROUP_ID2>"] = _pro.RampState(ticks=4, last_activity_ts=now, last_tick_ts=now)
        a._proactive["<OWNER_QQ>"] = _pro.RampState(ticks=1, last_activity_ts=now, last_tick_ts=now)
        # 2026-10-04 实测：单测的假会话 123456 曾混进真状态文件，她朝它开口白烧一个回合
        a._proactive["123456"] = _pro.RampState(ticks=0, last_activity_ts=now, last_tick_ts=now)
        asyncio.run(a._load_group_ids())
        self.assertIn("<GROUP_ID2>", a.group_ids, "群号名单没灌进去")
        self.assertNotIn("<GROUP_ID2>", a._proactive, "群号没从主动私聊名单里清掉")
        self.assertNotIn("123456", a._proactive, "不是好友的假会话没清掉（会白烧一个模型回合）")
        self.assertIn("<OWNER_QQ>", a._proactive, "真好友那格被误删了")
        self.assertTrue(a._is_group_chat("<GROUP_ID2>", None), "学了群号还是认成私聊")

    def test_group_list_failure_is_not_fatal(self):
        """协议端没起好时，名单拿不到不能炸，退回「靠收群消息学」的老路。"""
        from unittest import mock

        a = self._adapter()
        a.group_ids.clear()

        async def _boom(payload, **kw):
            raise RuntimeError("协议端还没起来")

        async def _noop(*a_, **k_):
            return None

        a._call_action = _boom
        with mock.patch("asyncio.sleep", new=_noop):
            asyncio.run(a._load_group_ids())
        self.assertEqual(set(), set(a.group_ids), "拿不到名单不该塞东西")

    def test_zero_probability_never_fires(self):
        a = self._adapter()
        a._proactive["<OWNER_QQ>"] = _pro.RampState(ticks=0, last_activity_ts=time.time(),
                                                    last_tick_ts=time.time() - 700)
        a.proactive.in_quiet = lambda now: False
        asyncio.run(a._proactive_check())
        self.assertEqual(self.dispatched, [], "0% 不该开口")

    def test_she_can_stay_silent_on_a_proactive_turn(self):
        a = self._adapter()
        a._proactive_turn = ("<OWNER_QQ>", time.time())
        res = asyncio.run(a.send("<OWNER_QQ>", "[SILENT]"))
        self.assertTrue(res.success)
        self.assertEqual(self.frames, [], "她自己选不说，就不该发出去")

    def test_silence_is_not_honored_on_a_normal_dm_turn(self):
        """他问了就得答：普通私聊回合不允许装死。"""
        a = self._adapter()
        a._proactive_turn = None
        asyncio.run(a.send("<OWNER_QQ>", "在的呀"))
        self.assertEqual(len(self.frames), 1, "普通回复必须发出去")

    def test_cold_start_seeds_the_owners_dm(self):
        """重启后不能变成「静默到下一次对话」——启动就给主人的私聊一个起点。"""
        a = self._adapter()
        a._proactive.clear()                     # 夹具清过表，这里显式重播一次（等价启动那一刻）
        a._proactive_seed_owners()
        self.assertTrue(a.trust_tier_owner_ids, "测试环境得能拿到主人号，否则这条没意义")
        self.assertIn(str(a.trust_tier_owner_ids[0]), a._proactive, "冷启动没播种")
        st = a._proactive[str(a.trust_tier_owner_ids[0])]
        self.assertEqual(st.ticks, 0, "播种不该是攒了格子的状态")
        self.assertGreater(st.last_activity_ts, 0)

    def test_state_survives_a_restart(self):
        a = self._adapter()
        now = time.time()
        a._proactive["<OWNER_QQ>"] = _pro.RampState(ticks=17, last_activity_ts=now - 3600,
                                                    last_tick_ts=now - 600, fires_today=2,
                                                    day=time.strftime("%Y-%m-%d"))
        a._proactive_fires = 5
        a._save_proactive()
        b = self._adapter()
        b.proactive_path = a.proactive_path
        b._proactive.clear()
        b._load_proactive()
        st = b._proactive["<OWNER_QQ>"]
        self.assertEqual(st.ticks, 17, "重启后格数不能丢")
        self.assertEqual(st.fires_today, 2)
        self.assertEqual(b._proactive_fires, 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
