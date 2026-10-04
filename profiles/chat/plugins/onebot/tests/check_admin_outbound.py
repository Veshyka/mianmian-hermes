"""她能不能自己动手管群：出站群管理标记（2026-10-03）。

背景：主人在群里问「理论上你应该可以用禁言这种群管理工具啊」，她答「出站只接了发消息，
禁言那条线没接过来」——**这次把那条线接上了**：

* 她在正文里写 `[禁言:QQ号,秒数]` / `[解禁:QQ号]` / `[踢出:QQ号]`
* 适配器在 `send()` 里**先剥标记**（主人永远看不到），再发真 OneBot 动作帧
* 硬护栏（提示词拦不住的那层）：不许动主人、不许动自己、时长钳制、只在群里、一条最多 3 个

⚠️ 动作**不是消息段**：混进 `message` 数组会被协议端当未知段拒，整条发不出去
（同一天 `face id=500` / 未知 CQ 码那两起事故）——所以走的是独立动作帧。
⚠️ 文件名 `check_` 前缀（disk-cleanup 会删 test_*）；只连进程内假协议端，不碰真群。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PLUGIN_PARENT = os.path.dirname(_PLUGIN_DIR)
_PKG = os.path.basename(_PLUGIN_DIR)
sys.path.insert(0, _PLUGIN_PARENT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from check_adapter_e2e import AdapterHarness  # noqa: E402
from mock_onebot import MockNapCat  # noqa: E402

_proto = __import__(f"{_PKG}.onebot_proto", fromlist=["*"])
_gadmin = __import__(f"{_PKG}.group_admin", fromlist=["*"])

OWNER = "<OWNER_QQ>"
VICTIM = "10001"
SELF = "<BOT_QQ>"


class TestMarkerParsing(unittest.TestCase):
    def test_ban_with_seconds(self):
        clean, acts, unknown = _proto.extract_admin_markers(f"[禁言:{VICTIM},600] 别刷了")
        self.assertEqual(acts, [{"kind": "ban", "qq": VICTIM, "seconds": "600"}])
        self.assertEqual(unknown, [])
        self.assertNotIn("禁言", clean, "标记必须从正文里剥掉")
        self.assertIn("别刷了", clean)

    def test_ban_without_seconds_and_fullwidth_colon(self):
        _, acts, _ = _proto.extract_admin_markers(f"[禁言：{VICTIM}]")
        self.assertEqual(acts, [{"kind": "ban", "qq": VICTIM, "seconds": ""}])

    def test_unban_and_kick(self):
        _, acts, _ = _proto.extract_admin_markers(f"[解禁:{VICTIM}] [踢出:10002]")
        self.assertEqual([a["kind"] for a in acts], ["unban", "kick"])

    def test_bad_target_is_stripped_and_reported(self):
        clean, acts, unknown = _proto.extract_admin_markers("[禁言:主人] 别刷了")
        self.assertEqual(acts, [])
        self.assertTrue(unknown)
        self.assertNotIn("禁言", clean, "写坏了也不能漏给主人")

    def test_plain_text_untouched(self):
        text = "就是普通一句话，没有任何标记"
        clean, acts, unknown = _proto.extract_admin_markers(text)
        self.assertEqual((clean, acts, unknown), (text, [], []))


class AdminHarness(AdapterHarness):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.adapter.self_id = SELF
        self.adapter.group_ids.add("55555")
        self.adapter.trust_tier_owner_ids = (OWNER,)
        # 群管理开着（线上 config 里 group_admin_enabled: true）
        self.adapter.group_admin = _gadmin.GroupAdmin({}, owner_ids=(OWNER,))

    def intents(self, parsed, gid="55555"):
        return self.adapter._admin_intents(parsed, gid)


class TestGuards(unittest.TestCase):
    """护栏是纯逻辑，不必起适配器；用一个最小替身读 `_admin_intents`。"""

    def setUp(self):
        self.adapter = _StubAdapter()

    def test_owner_is_never_touchable(self):
        out = self.adapter._admin_intents([{"kind": "ban", "qq": OWNER, "seconds": "300"}], "55555")
        self.assertEqual(out, [], "对主人的动作必须被代码级拦掉")

    def test_self_is_never_touchable(self):
        out = self.adapter._admin_intents([{"kind": "ban", "qq": SELF, "seconds": "300"}], "55555")
        self.assertEqual(out, [])

    def test_duration_is_clamped(self):
        lo = self.adapter._admin_intents([{"kind": "ban", "qq": VICTIM, "seconds": "5"}], "55555")
        self.assertEqual(lo[0].params["duration"], 60, "太短抬到 60 秒")
        hi = self.adapter._admin_intents([{"kind": "ban", "qq": VICTIM, "seconds": "99999999"}], "55555")
        self.assertEqual(hi[0].params["duration"], 2592000, "封顶 30 天")

    def test_default_seconds_when_omitted(self):
        out = self.adapter._admin_intents([{"kind": "ban", "qq": VICTIM, "seconds": ""}], "55555")
        self.assertEqual(out[0].params["duration"], 300)

    def test_unban_is_zero_duration(self):
        out = self.adapter._admin_intents([{"kind": "unban", "qq": VICTIM}], "55555")
        self.assertEqual(out[0].action, "set_group_ban")
        self.assertEqual(out[0].params["duration"], 0)

    def test_kick_action_name(self):
        out = self.adapter._admin_intents([{"kind": "kick", "qq": VICTIM}], "55555")
        self.assertEqual(out[0].action, "set_group_kick")

    def test_at_most_three_per_message(self):
        many = [{"kind": "ban", "qq": str(20000 + i), "seconds": "300"} for i in range(6)]
        out = self.adapter._admin_intents(many, "55555")
        self.assertEqual(len(out), 3, "一条消息最多 3 个动作（防注入刷屏）")


class _StubAdapter:
    """只带 `_admin_intents` 需要的属性，其余方法从真适配器借。

    ⚠️ 每个实例一份 `GroupAdmin`（含它自己的消息缓存）—— 类属性会让上一个测试的
    缓存漏进下一个测试（曾因此让「没有缓存就不撤回」这条假失败）。
    """

    self_id = SELF
    trust_tier_owner_ids = (OWNER,)
    ADMIN_MAX_PER_MESSAGE = 3
    ADMIN_TEXT_LIMITS = {"special_title": 6, "card": 30, "group_name": 30, "notice": 500}
    PENDING_JOIN_MAX = 20
    PENDING_JOIN_TTL = 24 * 3600.0
    _admin_intents = __import__(f"{_PKG}.adapter", fromlist=["*"]).OneBotAdapter._admin_intents

    def __init__(self):
        self.group_admin = _gadmin.GroupAdmin({}, owner_ids=(OWNER,))
        self._pending_joins = OrderedDict()


class TestE2E(AdminHarness):
    @staticmethod
    def _frames(client: MockNapCat, action: str) -> List[Dict[str, Any]]:
        return [a for a in client.actions if a.get("action") == action]

    async def test_ban_marker_becomes_real_action_and_text_stays_clean(self):
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            res = await self.adapter.send("55555", f"[禁言:{VICTIM},300] 别刷了 @所有人 求你")
            await asyncio.sleep(0.15)
            bans = self._frames(client, "set_group_ban")
            msgs = self._frames(client, "send_group_msg")
        self.assertTrue(res.success)
        self.assertEqual(len(bans), 1, "没发出真的禁言动作")
        p = bans[0]["params"]
        self.assertEqual(p["user_id"], int(VICTIM))
        self.assertEqual(p["group_id"], 55555)
        self.assertEqual(p["duration"], 300)
        self.assertTrue(msgs, "正文也该照常发出去")
        said = "".join(s["data"].get("text", "") for s in msgs[0]["params"]["message"]
                       if s["type"] == "text")
        self.assertNotIn("禁言", said, "标记漏给主人了")
        self.assertIn("别刷了", said)

    async def test_marker_only_message_sends_no_text(self):
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            res = await self.adapter.send("55555", f"[解禁:{VICTIM}]")
            await asyncio.sleep(0.15)
            bans = self._frames(client, "set_group_ban")
            msgs = self._frames(client, "send_group_msg")
        self.assertTrue(res.success)
        self.assertEqual(len(bans), 1)
        self.assertEqual(bans[0]["params"]["duration"], 0)
        self.assertEqual(msgs, [], "正文被剥空 → 不该发空消息")

    async def test_marker_in_private_chat_is_dropped(self):
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await self.adapter.send("99999", f"[禁言:{VICTIM},300] 试试")
            await asyncio.sleep(0.15)
            bans = self._frames(client, "set_group_ban")
            priv = self._frames(client, "send_private_msg")
        self.assertEqual(bans, [], "私聊里的群管理标记不许执行")
        said = "".join(s["data"].get("text", "") for s in priv[0]["params"]["message"]
                       if s["type"] == "text")
        self.assertNotIn("禁言", said)

    async def test_owner_target_blocked_end_to_end(self):
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await self.adapter.send("55555", f"[禁言:{OWNER},300] 试着动主人")
            await asyncio.sleep(0.15)
            bans = self._frames(client, "set_group_ban")
        self.assertEqual(bans, [], "对主人的禁言必须被拦")

    async def test_special_title_reaches_the_wire(self):
        """头衔：她写 [头衔:QQ,文字] → 线上必须出现 set_group_special_title（带 special_title）。"""
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await self.adapter.send("55555", f"[头衔:{VICTIM},卷王] 挂上了")
            await asyncio.sleep(0.15)
            titles = self._frames(client, "set_group_special_title")
            msgs = self._frames(client, "send_group_msg")
        self.assertEqual(len(titles), 1, "头衔动作没发出去")
        self.assertEqual(titles[0]["params"]["special_title"], "卷王")
        self.assertEqual(titles[0]["params"]["group_id"], 55555)
        said = "".join(s["data"].get("text", "") for s in msgs[0]["params"]["message"]
                       if s["type"] == "text")
        self.assertNotIn("头衔", said, "标记漏给主人了")

    async def test_approve_join_reaches_the_wire(self):
        """审批：她写 [同意入群:QQ号] → 线上必须出现 set_group_add_request（带真 flag）。"""
        self.adapter._pending_joins = OrderedDict()
        self.adapter._pending_joins[VICTIM] = {"flag": "FLAG-XYZ", "gid": "55555",
                                               "comment": "放我进", "ts": time.time()}
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            res = await self.adapter.send("55555", f"[同意入群:{VICTIM}] 让他进来吧")
            await asyncio.sleep(0.15)
            reqs = self._frames(client, "set_group_add_request")
            msgs = self._frames(client, "send_group_msg")
        self.assertTrue(res.success)
        self.assertEqual(len(reqs), 1, "审批动作没发出去")
        self.assertEqual(reqs[0]["params"]["flag"], "FLAG-XYZ")
        self.assertTrue(reqs[0]["params"]["approve"])
        said = "".join(s["data"].get("text", "") for s in msgs[0]["params"]["message"]
                       if s["type"] == "text")
        self.assertNotIn("同意入群", said)


class TestNewActions(unittest.TestCase):
    """2026-10-03 第二批：入群审批 / 撤回 / 全体禁言（主人在群里问「有人进群能不能审批」后加的）。"""

    def setUp(self):
        self.adapter = _StubAdapter()
        self.adapter._pending_joins = OrderedDict()

    # ── 入群审批 ─────────────────────────────────────────────────────────
    def test_approve_uses_the_real_flag_and_is_one_shot(self):
        self.adapter._pending_joins["10001"] = {"flag": "FLAG-ABC", "gid": "55555",
                                                "comment": "放我进", "ts": time.time()}
        out = self.adapter._admin_intents([{"kind": "approve_join", "qq": "10001"}], "55555")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].action, "set_group_add_request")
        self.assertEqual(out[0].params["flag"], "FLAG-ABC")
        self.assertTrue(out[0].params["approve"])
        self.assertEqual(out[0].params["sub_type"], "add")
        again = self.adapter._admin_intents([{"kind": "approve_join", "qq": "10001"}], "55555")
        self.assertEqual(again, [], "同一份申请不许批第二次")

    def test_approve_without_pending_request_does_nothing(self):
        out = self.adapter._admin_intents([{"kind": "approve_join", "qq": "99999"}], "55555")
        self.assertEqual(out, [], "没有待审批申请时不许凭号码乱批")

    def test_reject_carries_reason(self):
        self.adapter._pending_joins["10002"] = {"flag": "F2", "gid": "55555",
                                                "comment": "", "ts": time.time()}
        out = self.adapter._admin_intents(
            [{"kind": "reject_join", "qq": "10002", "reason": "广告号"}], "55555")
        self.assertEqual(out[0].params["approve"], False)
        self.assertEqual(out[0].params["reason"], "广告号")

    def test_expired_request_is_dropped(self):
        self.adapter._pending_joins["10003"] = {"flag": "F3", "gid": "55555", "comment": "",
                                                "ts": time.time() - 48 * 3600}
        out = self.adapter._admin_intents([{"kind": "approve_join", "qq": "10003"}], "55555")
        self.assertEqual(out, [], "过期的申请自动作废")

    # ── 撤回 ─────────────────────────────────────────────────────────────
    def test_recall_uses_last_cached_message_of_that_user(self):
        self.adapter.group_admin.cache.put("55555", "777", {"message_id": "777",
                                                            "user_id": "10001",
                                                            "text": "刷屏内容"})
        out = self.adapter._admin_intents([{"kind": "recall", "qq": "10001"}], "55555")
        self.assertEqual(out[0].action, "delete_msg")
        self.assertEqual(out[0].params["message_id"], 777)

    def test_recall_without_cached_message_does_nothing(self):
        out = self.adapter._admin_intents([{"kind": "recall", "qq": "10001"}], "55555")
        self.assertEqual(out, [])

    # ── 全体禁言 ─────────────────────────────────────────────────────────
    def test_whole_ban_on_and_off(self):
        on = self.adapter._admin_intents([{"kind": "whole_ban", "qq": "", "on": "1"}], "55555")
        self.assertEqual(on[0].action, "set_group_whole_ban")
        self.assertTrue(on[0].params["enable"])
        off = self.adapter._admin_intents([{"kind": "whole_ban", "qq": "", "on": "0"}], "55555")
        self.assertFalse(off[0].params["enable"])


class TestOwnerStillSafeWithNewActions(unittest.TestCase):
    def setUp(self):
        self.adapter = _StubAdapter()
        self.adapter._pending_joins = OrderedDict()

    def test_owner_cannot_be_recalled_or_rejected(self):
        self.adapter.group_admin.cache.put("55555", "888", {"message_id": "888",
                                                             "user_id": OWNER, "text": "主人说话"})
        for kind in ("recall", "kick", "ban"):
            out = self.adapter._admin_intents([{"kind": kind, "qq": OWNER}], "55555")
            self.assertEqual(out, [], f"{kind} 打到了主人身上")
        self.adapter._pending_joins[OWNER] = {"flag": "F", "gid": "55555", "comment": "",
                                              "ts": time.time()}
        out = self.adapter._admin_intents([{"kind": "reject_join", "qq": OWNER}], "55555")
        self.assertEqual(out, [], "拒绝主人的入群申请（理论上不该出现）也被拦")


class TestRequestPolicyAgent(unittest.TestCase):
    """`request.policy: agent`（2026-10-03）：群管理不动手、也不给主人发私聊 —— 交给她自己审。"""

    def _handle(self, policy: str):
        admin = _gadmin.GroupAdmin({"enabled": True,
                                    "request": {"policy": policy, "notify_owner": True}},
                                   owner_ids=(OWNER,))
        return admin.handle({"post_type": "request", "request_type": "group", "sub_type": "add",
                            "group_id": 55555, "user_id": 10001, "comment": "放我进",
                            "flag": "FLAG-10001"})

    def test_agent_policy_produces_no_owner_notice(self):
        out = self._handle("agent")
        acts = [i for i in out if getattr(i, "action", "") != "internal_noop"]
        self.assertEqual(acts, [], "policy=agent 下群管理不该产出任何动作（也不该通知主人）")

    def test_manual_policy_still_notifies_owner(self):
        """对照：老策略 manual 确实会给主人发私聊 —— 主人看到的那条就是这么来的。"""
        out = self._handle("manual")
        acts = [i for i in out if getattr(i, "action", "") != "internal_noop"]
        self.assertTrue(acts, "manual 策略没有产出通知（对照失效）")


class TestNewMarkersParsing(unittest.TestCase):
    def test_reject_reason_keeps_free_text(self):
        _, acts, _ = _proto.extract_admin_markers("[拒绝入群:10002,广告号 别再来了]")
        self.assertEqual(acts[0]["kind"], "reject_join")
        self.assertEqual(acts[0]["qq"], "10002")
        self.assertTrue(acts[0]["reason"])

    def test_whole_ban_reading(self):
        _, acts, _ = _proto.extract_admin_markers("[全体禁言:开]")
        self.assertEqual(acts[0]["on"], "1")
        _, acts2, _ = _proto.extract_admin_markers("[全体禁言:关]")
        self.assertEqual(acts2[0]["on"], "0")

    def test_all_new_markers_never_leak_to_the_text(self):
        clean, acts, _ = _proto.extract_admin_markers(
            "[同意入群:1] [拒绝入群:2,理由] [撤回:3] [全体禁言:开] 说完了")
        self.assertEqual(len(acts), 4)
        for word in ("同意入群", "拒绝入群", "撤回", "全体禁言"):
            self.assertNotIn(word, clean, f"{word} 漏给主人了")
        self.assertIn("说完了", clean)


class TestJoinReview(AdminHarness):
    """入群申请：她自己审（2026-10-03 主人「加上吧，这个让她自己审不要让别人提醒」）。

    钉子：申请进来 → **自动起一轮她的回合并把申请人 QQ/附言摆在她眼前**，
    她那一轮的标记再经 `send()` 变成真审批帧。同时钉住节流与 collect-only 降级。
    """

    def _req(self, qq: str = "10001", comment: str = "放我进", gid: str = "55555",
             request_type: str = "group") -> Dict[str, Any]:
        # ⚠️ NapCat 的加群申请是 **request_type="group"**（子类型在 sub_type）。
        #    2026-10-03 之前这里造的是 "add"，适配器也只认 "add" —— 两边一起错，
        #    结果线上「她自己审」的回合从来没起来过，测试却全绿。别改回去。
        ev: Dict[str, Any] = {"post_type": "request", "request_type": request_type,
                              "sub_type": "add",
                              "group_id": _proto._num(gid), "user_id": _proto._num(qq),
                              "comment": comment, "flag": "FLAG-" + qq}
        return ev

    async def asyncSetUp(self):
        # 群功能在测试环境默认关（生产靠 profile 配置）——本类要的是「开了之后」的行为
        await super().asyncSetUp()
        self.adapter.group_enabled = True
        self.adapter.group_wake_mode = "gated"

    @staticmethod
    def _frames(client: MockNapCat, action: str) -> List[Dict[str, Any]]:
        return [a for a in client.actions if a.get("action") == action]

    async def test_request_gets_one_review_turn_with_applicant_in_sight(self):
        self.adapter.group_wake_mode = "gated"
        await self.adapter._route_group_admin(self._req())
        self.assertEqual(len(self.dispatched), 1, "申请没起回合 = 她看不见申请")
        text = self.dispatched[0].text
        self.assertIn("10001", text, "申请人号码没摆到她眼前")
        self.assertIn("放我进", text)
        self.assertIn("同意入群", text, "没教她怎么批")
        self.assertIn("10001", self.adapter._pending_joins, "flag 没进队列 → 批了也发不出去")

    async def test_review_turn_text_windows_the_group(self):
        self.adapter.group_wake_mode = "gated"
        await self.adapter._route_group_admin(self._req())
        win = self.adapter.group_window.render_context("55555")
        self.assertIn("有人申请加入本群", win, "系统记录没落进群窗口")
        self.assertIn("10001", win)

    async def test_cooldown_blocks_a_second_turn(self):
        self.adapter.group_wake_mode = "gated"
        await self.adapter._route_group_admin(self._req(qq="10001"))
        await self.adapter._route_group_admin(self._req(qq="10002"))
        self.assertEqual(len(self.dispatched), 1, "冷却没生效（申请洪水会烧钱）")
        self.assertIn("10002", self.adapter._pending_joins, "冷却期内的申请也要进队列")

    async def test_hourly_cap_blocks_after_limit(self):
        self.adapter.group_wake_mode = "gated"
        self.adapter.JOIN_REVIEW_COOLDOWN_S = 0.0
        self.adapter.join_review_max_per_hour = 2
        for qq in ("10001", "10002", "10003"):
            await self.adapter._route_group_admin(self._req(qq=qq))
        self.assertEqual(len(self.dispatched), 2, "每小时上限没兜住")
        # 超限那份不叫醒她，但**直接放行**（宽松审核）——放行后 flag 就消耗掉了
        self.assertNotIn("10003", self.adapter._pending_joins,
                         "超限申请既没被放行、flag 还留着 = 申请人干等")

    async def test_napcat_group_request_type_wakes_her(self):
        """真字段是 `request_type="group"`（旧代码只认 "add"，所以她永远不知道有人申请）。"""
        self.adapter.group_wake_mode = "gated"
        self.adapter._join_review_ts.clear()
        self.adapter._join_review_last_ts.clear()
        await self.adapter._route_group_admin(self._req(request_type="group"))
        self.assertEqual(len(self.dispatched), 1, "NapCat 真字段没起回合 = 她看不见申请")

    async def test_legacy_add_request_type_still_tolerated(self):
        """兼容旧写法（别的实现可能发 "add"）——两路都得认。"""
        self.adapter.group_wake_mode = "gated"
        self.adapter._join_review_ts.clear()
        self.adapter._join_review_last_ts.clear()
        await self.adapter._route_group_admin(self._req(request_type="add"))
        self.assertEqual(len(self.dispatched), 1)

    async def test_overflow_is_approved_silently_without_waking_her(self):
        """超频（主人：「申请一小时不要给她发太多」）→ 不唤醒、直接放行、不发群消息。"""
        self.adapter.group_wake_mode = "gated"
        self.adapter.JOIN_REVIEW_COOLDOWN_S = 0.0
        self.adapter.join_review_max_per_hour = 1
        self.adapter._join_review_ts.clear()
        self.adapter._join_review_last_ts.clear()
        await self.adapter._route_group_admin(self._req(qq="10001"))
        self.assertEqual(len(self.dispatched), 1)
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await self.adapter._route_group_admin(self._req(qq="10002"))
            await asyncio.sleep(0.15)
            reqs = self._frames(client, "set_group_add_request")
            msgs = self._frames(client, "send_group_msg")
        self.assertEqual(len(self.dispatched), 1, "超限申请不该再唤醒她（省 AI 的钱）")
        self.assertEqual(len(reqs), 1, "超限申请没被放行 → 申请人干等")
        self.assertTrue(reqs[0]["params"]["approve"])
        self.assertEqual(msgs, [], "放行是静默的，不该在群里说话")
        self.assertNotIn("10002", self.adapter._pending_joins, "放行后队列没清")

    async def test_review_turn_stays_out_of_the_group(self):
        """主人：「不要反馈到群里」——她审申请时写的话不进群，但标记照样生效。"""
        self.adapter.group_wake_mode = "gated"
        await self.adapter._route_group_admin(self._req())
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await self.adapter.send("55555", "[同意入群:10001] 我看看…嗯，让他进来吧")
            await asyncio.sleep(0.15)
            reqs = self._frames(client, "set_group_add_request")
            msgs = self._frames(client, "send_group_msg")
        self.assertEqual(len(reqs), 1, "标记没生效")
        self.assertTrue(reqs[0]["params"]["approve"])
        self.assertEqual(msgs, [], "审申请那一轮的正文漏进群里了")

    async def test_normal_group_turn_is_not_silenced(self):
        """静默只针对审申请那一轮：普通群回合照旧说话。"""
        self.adapter.group_enabled = True
        self.adapter.group_wake_mode = "gated"
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await self.adapter.send("55555", "我在呢")
            await asyncio.sleep(0.15)
            msgs = self._frames(client, "send_group_msg")
        self.assertEqual(len(msgs), 1, "普通回合被误静默了")

    async def test_collect_only_records_but_does_not_review(self):
        self.adapter.group_wake_mode = "collect-only"
        await self.adapter._route_group_admin(self._req())
        self.assertEqual(self.dispatched, [], "collect-only 档下不许起回合")
        self.assertIn("10001", self.adapter._pending_joins, "照旧要进队列（主人可叫她手动批）")

    async def test_approved_from_the_review_turn_reaches_the_wire(self):
        """最要紧的一条：她审完写的标记，必须真的变成审批帧。"""
        self.adapter.group_wake_mode = "gated"
        await self.adapter._route_group_admin(self._req())
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await self.adapter.send("55555", "[同意入群:10001] 进来吧")
            await asyncio.sleep(0.15)
            reqs = self._frames(client, "set_group_add_request")
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0]["params"]["flag"], "FLAG-10001")
        self.assertTrue(reqs[0]["params"]["approve"])


class TestMoreGroupActions(unittest.TestCase):
    """群管理第二波（2026-10-03 主人：「能不能让她可以授予头衔啥的」）：

    头衔 / 管理员 / 名片 / 公告 / 精华 / 改群名 / 戳一戳 —— 全部按本机 NapCat 4.18.28
    实测确认存在的 action 接（探针见 `tmp/napcat_probe.sh`，空参回 "Schema compilation
    error" = 动作存在，正确参数回业务错误 = 参数形态对）。
    """

    def setUp(self):
        self.adapter = _StubAdapter()

    # ── 头衔 ─────────────────────────────────────────────────────────────
    def test_special_title_is_capped_to_six_chars(self):
        out = self.adapter._admin_intents(
            [{"kind": "special_title", "qq": "10001", "text": "卷王之王卷王之王卷王"}], "55555")
        self.assertEqual(out[0].action, "set_group_special_title")
        self.assertEqual(out[0].params["special_title"], "卷王之王卷王", "QQ 侧最多 6 个字")
        self.assertEqual(out[0].params["user_id"], 10001)

    def test_empty_title_does_nothing(self):
        self.assertEqual(
            self.adapter._admin_intents([{"kind": "special_title", "qq": "10001", "text": ""}],
                                        "55555"), [])

    # ── 管理员 ───────────────────────────────────────────────────────────
    def test_admin_grant_and_revoke(self):
        on = self.adapter._admin_intents([{"kind": "set_admin", "qq": "10001"}], "55555")
        self.assertEqual(on[0].action, "set_group_admin")
        self.assertTrue(on[0].params["enable"])
        off = self.adapter._admin_intents([{"kind": "unset_admin", "qq": "10001"}], "55555")
        self.assertFalse(off[0].params["enable"])

    # ── 名片 / 公告 / 群名 ───────────────────────────────────────────────
    def test_card_and_notice_and_group_name(self):
        card = self.adapter._admin_intents([{"kind": "card", "qq": "10001", "text": "新人甲"}], "55555")
        self.assertEqual(card[0].params["card"], "新人甲")
        notice = self.adapter._admin_intents([{"kind": "notice", "text": "今晚维护"}], "55555")
        self.assertEqual(notice[0].action, "_send_group_notice")
        self.assertEqual(notice[0].params["content"], "今晚维护")
        self.assertEqual(notice[0].params["group_id"], 55555)
        name = self.adapter._admin_intents([{"kind": "group_name", "text": "棉花糖"}], "55555")
        self.assertEqual(name[0].action, "set_group_name")
        self.assertEqual(name[0].params["group_name"], "棉花糖")

    def test_notice_is_length_capped(self):
        out = self.adapter._admin_intents([{"kind": "notice", "text": "长" * 900}], "55555")
        self.assertEqual(len(out[0].params["content"]), 500)

    # ── 精华 ─────────────────────────────────────────────────────────────
    def test_essence_uses_cached_message_of_that_user(self):
        self.adapter.group_admin.cache.put("55555", "666", {"message_id": "666",
                                                            "user_id": "10001", "text": "名场面"})
        out = self.adapter._admin_intents([{"kind": "essence", "qq": "10001"}], "55555")
        self.assertEqual(out[0].action, "set_essence_msg")
        self.assertEqual(out[0].params["message_id"], 666)
        off = self.adapter._admin_intents([{"kind": "unessence", "qq": "10001"}], "55555")
        self.assertEqual(off[0].action, "delete_essence_msg")

    def test_essence_without_cache_does_nothing(self):
        self.assertEqual(self.adapter._admin_intents([{"kind": "essence", "qq": "10001"}], "55555"), [])

    # ── 戳一戳 ───────────────────────────────────────────────────────────
    def test_poke(self):
        out = self.adapter._admin_intents([{"kind": "poke", "qq": "10001"}], "55555")
        self.assertEqual(out[0].action, "group_poke")
        self.assertEqual(out[0].params["user_id"], 10001)

    # ── 护栏仍然覆盖新动作 ───────────────────────────────────────────────
    def test_owner_is_protected_on_every_new_action(self):
        self.adapter.group_admin.cache.put("55555", "999", {"message_id": "999",
                                                            "user_id": OWNER, "text": "主人说话"})
        for kind in ("special_title", "set_admin", "unset_admin", "card", "essence",
                     "unessence", "poke", "kick", "ban", "recall"):
            item = {"kind": kind, "qq": OWNER, "text": "老板"}
            self.assertEqual(self.adapter._admin_intents([item], "55555"), [],
                             f"{kind} 打到了主人身上")


class TestMoreMarkersParsing(unittest.TestCase):
    def test_special_title_keeps_free_text(self):
        _, acts, _ = _proto.extract_admin_markers("[头衔:10001,卷王之王] 挂了")
        self.assertEqual(acts[0]["kind"], "special_title")
        self.assertEqual(acts[0]["qq"], "10001")
        self.assertEqual(acts[0]["text"], "卷王之王")

    def test_long_words_win_over_short_ones(self):
        """`取消管理员` 不能被 `管理员` 抢先命中（否则只剩个`取消`尾巴当文本）。"""
        _, acts, _ = _proto.extract_admin_markers("[取消管理员:10001]")
        self.assertEqual(acts[0]["kind"], "unset_admin")
        _, acts2, _ = _proto.extract_admin_markers("[取消精华:10001]")
        self.assertEqual(acts2[0]["kind"], "unessence")

    def test_notice_and_group_name_take_text_without_qq(self):
        _, acts, _ = _proto.extract_admin_markers("[公告:今晚十点维护] [改群名:棉花糖]")
        self.assertEqual([a["kind"] for a in acts], ["notice", "group_name"])
        self.assertEqual(acts[0]["text"], "今晚十点维护")
        self.assertEqual(acts[1]["text"], "棉花糖")

    def test_empty_notice_is_stripped_but_recorded(self):
        clean, acts, unknown = _proto.extract_admin_markers("[公告:] 就这样")
        self.assertEqual(acts, [])
        self.assertTrue(unknown)
        self.assertNotIn("公告", clean)

    def test_new_markers_never_leak_to_the_text(self):
        clean, acts, _ = _proto.extract_admin_markers(
            "[头衔:1,甲] [管理员:2] [取消管理员:3] [名片:4,乙] [公告:丙] [改群名:丁] "
            "[精华:5] [取消精华:6] [戳:7] 说完了")
        self.assertEqual(len(acts), 9)
        for word in ("头衔", "管理员", "名片", "公告", "改群名", "精华", "戳"):
            self.assertNotIn(word, clean, f"{word} 漏给主人了")
        self.assertIn("说完了", clean)


class TestInjectionAndSkill(AdminHarness):
    """注入瘦身 + 手册接线（2026-10-03 主人拍板）。"""

    async def test_only_markers_means_no_empty_message(self):
        """她只写一个标记、没说话 → 动作照发，但不往群里丢空消息。"""
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            res = await self.adapter.send("55555", f"[禁言:{VICTIM},600]")
        self.assertTrue(res.success)
        self.assertTrue(self._frames(client, "set_group_ban"), "动作没发出去")
        self.assertFalse(self._frames(client, "send_group_msg"), "发了条空消息")

    async def test_markers_with_words_still_send_the_words(self):
        """标记 + 说话 → 正文照旧发，标记不出现。"""
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await self.adapter.send("55555", f"停一下 [禁言:{VICTIM},600]")
        said = (self._frames(client, "send_group_msg") or [{}])[0]
        self.assertIn("停一下", str(said))
        self.assertNotIn("禁言", str(said))

    @staticmethod
    def _frames(client: MockNapCat, action: str) -> List[Dict[str, Any]]:
        return [a for a in client.actions if a.get("action") == action]


class TestSkillWiring(unittest.TestCase):
    """手册本身：她技能库里的 group-admin 必须真存在、且被注入指到。"""

    SKILL = Path("/opt/data/profiles/chat/skills/communication/group-admin/SKILL.md")

    def test_skill_file_exists_with_frontmatter(self):
        self.assertTrue(self.SKILL.exists(), f"手册不在 {self.SKILL}")
        head = self.SKILL.read_text(encoding="utf-8")[:400]
        self.assertTrue(head.startswith("---"), "没有 frontmatter")
        self.assertIn("name: group-admin", head)
        self.assertIn("description: Use when", head)

    def test_skill_covers_every_marker_the_adapter_understands(self):
        """代码认的每一种动作，手册里都得有写法 —— 少一个她就会"不知道自己能做"。"""
        from onebot import onebot_proto as proto
        body = self.SKILL.read_text(encoding="utf-8")
        for word in proto.ADMIN_SPEC:
            self.assertIn(f"[{word}", body, f"手册里没有 [{word}:…] 的写法")

    def test_injection_is_a_pointer_not_a_menu(self):
        """正文只说"看 skill `group-admin`"，标记表不再每轮糊一遍；约束句也拿掉了。"""
        from onebot import group_wake as gw
        text = gw.build_turn_text(gid="55555", trigger="at", window_text="x")
        self.assertIn("group-admin", text, "没告诉她手册在哪")
        for leak in ("[禁言:", "[头衔:", "[撤回:", "[公告:", "[改群名:"):
            self.assertNotIn(leak, text, f"注入里还留着 {leak} 的写法 = 没瘦下来")
        self.assertNotIn("只有主人明确要求", text, "主人要她在群里自由点，这类约束该拿掉")


if __name__ == "__main__":
    unittest.main(verbosity=2)
