"""`group_admin.py` 的离线单测（纯逻辑，不连网、不起 agent、不碰配置）。

守六条线：
  1. **默认保守**：空 cfg 下入群/加群申请/违禁词/防撤回**一律不动手**
  2. **权限自己写**：非主人的管理命令**不产出任何执行类 Intent**（最多留一条内部拒绝提示）
  3. **通知不退化**：待处理申请/越权命令等通知**必须留下 Intent**（不许被吞成空表）
  4. **缓存有界**：消息缓存按 message_id 去重 + 容量上限，不会无限长
  5. **防撤回两态**：缓存里有原文→补发内容；没有→**仍留一条痕迹**（不静默丢）
  6. **零 I/O**：只用标准库，靠「合成包」加载模块，连 `onebot/__init__.py`（会 import 适配器）都不执行

跑法：`/opt/hermes/.venv/bin/python3 tests/check_group_admin.py`
"""

from __future__ import annotations

import importlib
import os
import sys
import types
import unittest

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PKG = os.path.basename(_PLUGIN_DIR)

# 合成一个只含 __path__ 的包 → `from . import onebot_proto` 能用，
# 但**不会**执行 onebot/__init__.py（那会 import 适配器/gateway，属于本测试不该拉的依赖）。
if _PKG not in sys.modules:
    _pkg = types.ModuleType(_PKG)
    _pkg.__path__ = [_PLUGIN_DIR]              # type: ignore[attr-defined]
    sys.modules[_PKG] = _pkg

_ga = importlib.import_module(f"{_PKG}.group_admin")

SELF = "90001"
OWNER = "10001"
STRANGER = "20002"
GID = "55555"
OTHER_GID = "66666"
T0 = 1_700_000_000.0


# ── 造事件的小工具 ─────────────────────────────────────────────────────────


def _msg(text="", *, mid=7, uid=STRANGER, gid=GID, segs=None, t=T0, name="甲") -> dict:
    if segs is None:
        segs = [{"type": "text", "data": {"text": text}}]
    return {"post_type": "message", "message_type": "group", "group_id": gid,
            "user_id": uid, "message_id": mid, "message": segs, "time": t,
            "sender": {"user_id": uid, "nickname": name}}


def _notice(ntype, *, uid=STRANGER, gid=GID, mid=7, t=T0) -> dict:
    ev = {"post_type": "notice", "notice_type": ntype, "group_id": gid,
          "user_id": uid, "time": t}
    if mid is not None:
        ev["message_id"] = mid
    return ev


def _request(*, uid=STRANGER, gid=GID, comment="", flag="flag-1", rt="group", t=T0) -> dict:
    return {"post_type": "request", "request_type": rt, "group_id": gid, "user_id": uid,
            "comment": comment, "flag": flag, "time": t}


def _ga_new(cfg=None, *, owners=(OWNER,), self_id=SELF):
    return _ga.GroupAdmin(cfg, owner_ids=owners, self_id=self_id)


def _actions(intents):
    return [i.action for i in intents]


def _exec_actions(intents):
    """执行类动作（排除只为留痕/通知准备的内部动作）。"""
    return [i.action for i in intents if i.action != _ga.ACTION_INTERNAL]


# ── 契约与回归 ─────────────────────────────────────────────────────────────


class TestContract(unittest.TestCase):
    def test_intent_is_frozen_with_note_default(self):
        i = _ga.Intent("set_group_ban", {"a": 1}, "cmd_ban")
        self.assertEqual(i.note, "")
        self.assertEqual(i.action, "set_group_ban")
        with self.assertRaises(Exception):
            i.action = "x"                                  # frozen

    def test_default_config_has_every_section(self):
        for key in ("enabled", "cache_max", "welcome", "leave", "request", "keywords",
                    "banned_words", "anti_recall", "commands", "stats",
                    "keyword_max_per_min"):
            self.assertIn(key, _ga.DEFAULT_CONFIG)

    def test_empty_event_and_bad_input(self):
        ga = _ga_new()
        self.assertEqual(ga.handle({}), [])
        self.assertEqual(ga.handle(None), [])            # type: ignore[arg-type]
        self.assertEqual(ga.handle("nope"), [])          # type: ignore[arg-type]

    def test_private_message_not_handled(self):
        ga = _ga_new({"keywords": [{"pattern": "你好", "reply": "在"}]})
        ev = _msg("你好")
        ev["message_type"] = "private"
        self.assertEqual(ga.handle(ev), [])

    def test_master_switch_off_kills_everything(self):
        ga = _ga_new({"enabled": False, "welcome": {"enabled": True}})
        self.assertEqual(ga.handle(_notice("group_increase")), [])
        self.assertEqual(ga.handle(_msg("/ban 1")), [])

    def test_unknown_notice_type_yields_nothing_but_keeps_reason(self):
        ga = _ga_new({"welcome": {"enabled": True}})
        self.assertEqual(ga.handle(_notice("group_ban")), [])
        self.assertTrue(ga.last_skip)                    # 有原因留痕，不静默

    def test_internal_intent_carries_no_params(self):
        ga = _ga_new({"request": {"policy": "manual"}}, owners=())   # 没有主人号
        out = ga.handle(_request(comment="想进群"))
        self.assertEqual(_actions(out), [_ga.ACTION_INTERNAL])
        self.assertEqual(out[0].params, {})
        self.assertTrue(out[0].note)

    def test_notification_never_degrades_to_empty(self):
        """回归：曾经「通知类」整条被丢 → handle 返回空表，主人什么都看不到。"""
        for cfg, ev in (
            ({"request": {"policy": "manual"}}, _request()),
            ({"commands": {"prefix": "/"}}, _msg("/ban 1", uid=STRANGER)),
        ):
            with self.subTest(cfg=cfg):
                out = _ga_new(cfg).handle(ev)
                self.assertTrue(out)
                self.assertTrue(out[0].note)


class TestWelcome(unittest.TestCase):
    def test_off_by_default(self):
        ga = _ga_new()
        self.assertEqual(ga.handle(_notice("group_increase", uid="30003")), [])

    def test_on_sends_with_placeholders(self):
        ga = _ga_new({"welcome": {"enabled": True, "text": "{at} 你好 {name} 欢迎来到 {group}"}})
        out = ga.handle(_notice("group_increase", uid="30003"))
        self.assertEqual(_actions(out), ["send_group_msg"])
        self.assertEqual(out[0].kind, "welcome")
        # ⚠️ 2026-10-03 行为变更（PLAN-v6 表情包批次）：`build_action()` 现在会把正文里的
        # CQ 码解析成**真消息段**（她「正文发图 / 发表情包」走的就是这条路），所以 `{at}`
        # 在这里就已经是 at 段、不再是一串字面文本。断言按新形状写（比旧断言更强）：
        segs = out[0].params["message"]
        self.assertEqual(segs[0]["type"], "at")
        self.assertEqual(str(segs[0]["data"]["qq"]), "30003")
        joined = "".join(s["data"].get("text", "") for s in segs if s["type"] == "text")
        self.assertIn(GID, joined)
        self.assertIn("30003", joined)                              # name 回落到 QQ 号
        self.assertNotIn("[CQ:", str(segs), "CQ 码不许以字面文本发出去")
        self.assertEqual(str(out[0].params["group_id"]), GID)
        self.assertNotIn("self_id", out[0].params)                  # 适配器自己补

    def test_group_filter(self):
        ga = _ga_new({"welcome": {"enabled": True, "groups": [OTHER_GID]}})
        self.assertEqual(ga.handle(_notice("group_increase")), [])
        self.assertEqual(len(ga.handle(_notice("group_increase", gid=OTHER_GID))), 1)

    def test_cooldown_suppresses_second_welcome(self):
        ga = _ga_new({"welcome": {"enabled": True, "cooldown_s": 10}})
        self.assertEqual(len(ga.handle(_notice("group_increase", t=T0))), 1)
        self.assertEqual(ga.handle(_notice("group_increase", uid="30004", t=T0 + 5)), [])
        self.assertEqual(len(ga.handle(_notice("group_increase", uid="30005", t=T0 + 30))), 1)

    def test_decrease_does_not_trigger_welcome(self):
        ga = _ga_new({"welcome": {"enabled": True}})
        self.assertEqual(ga.handle(_notice("group_decrease")), [])


class TestLeave(unittest.TestCase):
    def test_off_by_default(self):
        self.assertEqual(_ga_new().handle(_notice("group_decrease")), [])

    def test_on_sends(self):
        ga = _ga_new({"leave": {"enabled": True, "text": "{name} 走了"}})
        out = ga.handle(_notice("group_decrease"))
        self.assertEqual(_actions(out), ["send_group_msg"])
        self.assertEqual(out[0].kind, "leave")

    def test_self_leaving_is_skipped(self):
        ga = _ga_new({"leave": {"enabled": True}})
        self.assertEqual(ga.handle(_notice("group_decrease", uid=SELF)), [])


class TestRequest(unittest.TestCase):
    def test_manual_default_never_approves(self):
        ga = _ga_new()                                     # 空 cfg = manual
        out = ga.handle(_request(comment="求进群"))
        self.assertNotIn("set_group_add_request", _exec_actions(out))
        self.assertEqual(out[0].kind, "request_manual")
        self.assertEqual(out[0].action, "send_private_msg")
        self.assertEqual(str(out[0].params["user_id"]), OWNER)

    def test_manual_without_owner_still_notifies(self):
        out = _ga_new({}, owners=()).handle(_request())
        self.assertEqual(_actions(out), [_ga.ACTION_INTERNAL])
        self.assertIn("加群申请", out[0].note)

    def test_invalid_policy_falls_back_to_manual(self):
        self.assertEqual(_ga.merge_config({"request": {"policy": "yolo"}})["request"]["policy"],
                         "manual")
        out = _ga_new({"request": {"policy": "yolo"}}).handle(_request())
        self.assertNotIn("set_group_add_request", _exec_actions(out))

    def test_auto_approve(self):
        ga = _ga_new({"request": {"policy": "auto_approve", "approve_comment": "欢迎"}})
        out = ga.handle(_request(flag="F9"))
        self.assertEqual(_actions(out), ["set_group_add_request"])
        self.assertTrue(out[0].params["approve"])
        self.assertEqual(out[0].params["flag"], "F9")
        self.assertEqual(out[0].params["comment"], "欢迎")

    def test_auto_reject_reason_is_configurable(self):
        ga = _ga_new({"request": {"policy": "auto_reject", "reject_reason": "人满"}})
        out = ga.handle(_request())
        self.assertEqual(_actions(out), ["set_group_add_request"])
        self.assertFalse(out[0].params["approve"])
        self.assertEqual(out[0].params["reason"], "人满")

    def test_blacklist_overrides_auto_approve(self):
        ga = _ga_new({"request": {"policy": "auto_approve",
                                  "blacklist_keywords": ["广告"],
                                  "reject_reason": "黑名单"}})
        out = ga.handle(_request(comment="来发广告啦"))
        self.assertEqual(_actions(out), ["set_group_add_request"])
        self.assertFalse(out[0].params["approve"])
        self.assertEqual(out[0].params["reason"], "黑名单")

    def test_whitelist_keyword_blocks_approve(self):
        ga = _ga_new({"request": {"policy": "auto_approve", "whitelist_keywords": ["暗号"]}})
        out = ga.handle(_request(comment="随手点的"))
        self.assertNotIn("set_group_add_request", _exec_actions(out))
        self.assertIn("白名单", out[0].note)

    def test_friend_request_ignored(self):
        self.assertEqual(_ga_new({"request": {"policy": "auto_approve"}})
                         .handle(_request(rt="friend")), [])


class TestKeywords(unittest.TestCase):
    def test_no_rules_means_no_reply(self):
        self.assertEqual(_ga_new().handle(_msg("你好")), [])

    def test_contains_match(self):
        ga = _ga_new({"keywords": [{"pattern": "你好", "reply": "好呀", "match": "contains"}]})
        out = ga.handle(_msg("你好啊棉棉"))
        self.assertEqual(_actions(out), ["send_group_msg"])
        self.assertEqual(out[0].kind, "keyword_reply")
        self.assertEqual(out[0].params["message"][0]["data"]["text"], "好呀")

    def test_exact_match_negative(self):
        ga = _ga_new({"keywords": [{"pattern": "菜单", "reply": "这是菜单", "match": "exact"}]})
        self.assertEqual(ga.handle(_msg("求菜单呀")), [])
        self.assertEqual(len(ga.handle(_msg("菜单"))), 1)

    def test_prefix_match(self):
        ga = _ga_new({"keywords": [{"pattern": "规则", "reply": "看群公告", "prefix": True}]})
        self.assertEqual(ga.handle(_msg("问规则会怎样")), [])
        self.assertEqual(len(ga.handle(_msg("规则是什么"))), 1)

    def test_regex_match_and_bad_regex_is_dropped(self):
        ga = _ga_new({"keywords": [
            {"pattern": "([(", "reply": "坏正则", "match": "regex"},
            {"pattern": r"^1[3-9]\d{9}$", "reply": "别发手机号", "match": "regex"},
        ]})
        out = ga.handle(_msg("13800138000", mid=9))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].kind, "keyword_reply")

    def test_group_filter(self):
        ga = _ga_new({"keywords": [{"pattern": "kp", "reply": "在", "groups": [OTHER_GID]}]})
        self.assertEqual(ga.handle(_msg("kp")), [])
        self.assertEqual(len(ga.handle(_msg("kp", gid=OTHER_GID))), 1)

    def test_rate_limit_per_minute(self):
        ga = _ga_new({"keyword_max_per_min": 1,
                      "keywords": [{"pattern": "kp", "reply": "在"}]})
        self.assertEqual(len(ga.handle(_msg("kp", mid=1, t=T0))), 1)
        self.assertEqual(ga.handle(_msg("kp", mid=2, t=T0 + 3)), [])
        self.assertEqual(len(ga.handle(_msg("kp", mid=3, t=T0 + 61))), 1)

    def test_whitespace_only_text_does_not_reply(self):
        ga = _ga_new({"keywords": [{"pattern": "图", "reply": "?"}]})
        self.assertEqual(ga.handle(_msg(segs=[{"type": "text", "data": {"text": "   "}}])), [])

    def test_media_placeholder_is_not_the_raw_word(self):
        # 图片渲染成占位符「[图片]」→ 规则写「照片」不该被占位符误命中
        ga = _ga_new({"keywords": [{"pattern": "照片", "reply": "?"}]})
        self.assertEqual(ga.handle(_msg(segs=[{"type": "image", "data": {}}])), [])


class TestBannedWords(unittest.TestCase):
    CFG = {"banned_words": {"enabled": True, "words": ["加群私聊"], "warn_text": "{at} 别发这个"}}

    def test_off_by_default(self):
        self.assertEqual(_ga_new().handle(_msg("加群私聊")), [])

    def test_off_by_default_for_regex_too(self):
        cfg = {"banned_words": {"regex": [r"广告"]}}
        self.assertEqual(_ga_new(cfg).handle(_msg("广告")), [])

    def test_delete_intent(self):
        out = _ga_new(self.CFG).handle(_msg("加群私聊我", mid=42))
        self.assertIn("delete_msg", _actions(out))
        dele = [i for i in out if i.action == "delete_msg"][0]
        self.assertEqual(dele.kind, "banned_delete")
        self.assertEqual(str(dele.params["message_id"]), "42")

    def test_warn_text_sent(self):
        out = _ga_new(self.CFG).handle(_msg("加群私聊", mid=42))
        warns = [i for i in out if i.kind == "banned_warn"]
        self.assertEqual(len(warns), 1)
        # 同上（2026-10-03 行为变更）：@ 现在是真 at 段，不是字面 CQ 文本
        segs = warns[0].params["message"]
        ats = [s for s in segs if s["type"] == "at"]
        self.assertTrue(ats, f"违禁词警告要 @ 到人，实际段：{segs}")
        self.assertEqual(str(ats[0]["data"]["qq"]), STRANGER)
        self.assertNotIn("[CQ:", str(segs), "CQ 码不许以字面文本发出去")

    def test_no_hit_no_action(self):
        self.assertEqual(_ga_new(self.CFG).handle(_msg("正常聊天", mid=43)), [])

    def test_owner_exempt(self):
        out = _ga_new(self.CFG).handle(_msg("加群私聊", uid=OWNER))
        self.assertEqual(_exec_actions(out), [])

    def test_exempt_owner_can_be_turned_off(self):
        cfg = {"banned_words": {"enabled": True, "words": ["加群私聊"], "exempt_owner": False}}
        self.assertIn("delete_msg", _actions(_ga_new(cfg).handle(_msg("加群私聊", uid=OWNER))))

    def test_delete_ban_adds_ban_intent(self):
        cfg = {"banned_words": {"enabled": True, "words": ["刷屏"], "action": "delete_ban",
                                "ban_seconds": 60}}
        out = _ga_new(cfg).handle(_msg("刷屏刷屏"))
        kinds = [i.action for i in out]
        self.assertIn("delete_msg", kinds)
        bans = [i for i in out if i.action == "set_group_ban"]
        self.assertEqual(len(bans), 1)
        self.assertEqual(bans[0].params["duration"], 60)
        self.assertEqual(str(bans[0].params["user_id"]), STRANGER)

    def test_regex_hit(self):
        cfg = {"banned_words": {"enabled": True, "regex": [r"1[3-9]\d{9}"]}}
        out = _ga_new(cfg).handle(_msg("我的号 13800138000 加我"))
        self.assertIn("delete_msg", _actions(out))

    def test_missing_message_id_still_leaves_trace(self):
        cfg = {"banned_words": {"enabled": True, "words": ["刷屏"]}}
        ev = _msg("刷屏")
        del ev["message_id"]
        out = _ga_new(cfg).handle(ev)
        self.assertEqual(_actions(out), [_ga.ACTION_INTERNAL])
        self.assertIn("违禁词", out[0].note)

    def test_group_filter(self):
        cfg = {"banned_words": {"enabled": True, "words": ["刷屏"], "groups": [OTHER_GID]}}
        self.assertEqual(_ga_new(cfg).handle(_msg("刷屏")), [])
        self.assertTrue(_ga_new(cfg).handle(_msg("刷屏", gid=OTHER_GID)))

    def test_banned_hit_suppresses_keyword_reply(self):
        cfg = dict(self.CFG)
        cfg["keywords"] = [{"pattern": "加群", "reply": "不许发"}]
        out = _ga_new(cfg).handle(_msg("加群私聊我"))
        self.assertNotIn("keyword_reply", [i.kind for i in out])
        self.assertIn("delete_msg", _actions(out))


class TestAntiRecall(unittest.TestCase):
    CFG = {"anti_recall": {"enabled": True, "text": "{name} 撤回了：{content}"}}

    def test_off_by_default(self):
        ga = _ga_new()
        ga.note_message(_msg("悄悄话", mid=7))
        self.assertEqual(ga.handle(_notice("group_recall", uid=STRANGER, mid=7)), [])

    def test_with_cached_original(self):
        ga = _ga_new(self.CFG)
        ga.note_message(_msg("原始内容", mid=7, uid=STRANGER, name="甲"))
        out = ga.handle(_notice("group_recall", uid=STRANGER, mid=7))
        self.assertEqual(_actions(out), ["send_group_msg"])
        self.assertEqual(out[0].kind, "anti_recall")
        text = out[0].params["message"][0]["data"]["text"]
        self.assertIn("原始内容", text)
        self.assertIn("甲", text)

    def test_without_cached_original_still_traces(self):
        ga = _ga_new(self.CFG)                       # 没喂过 note_message
        out = ga.handle(_notice("group_recall", uid=STRANGER, mid=7))
        self.assertEqual(_actions(out), [_ga.ACTION_INTERNAL])
        self.assertIn("缓存", out[0].note)

    def test_evicted_original_degrades_gracefully(self):
        ga = _ga_new({"cache_max": 2, "anti_recall": {"enabled": True}})
        for mid in (1, 2, 3):
            ga.note_message(_msg("第%d条" % mid, mid=mid))
        out = ga.handle(_notice("group_recall", mid=1))
        self.assertEqual(_actions(out), [_ga.ACTION_INTERNAL])   # 原文被挤掉了

    def test_self_recall_skipped(self):
        ga = _ga_new(self.CFG)
        ga.note_message(_msg("她说的", mid=7, uid=SELF))
        self.assertEqual(ga.handle(_notice("group_recall", uid=SELF, mid=7)), [])

    def test_own_message_not_cached(self):
        ga = _ga_new(self.CFG)
        ga.note_message(_msg("她说的", mid=7, uid=SELF))
        self.assertIsNone(ga.cache.get(GID, "7"))

    def test_other_notice_types_ignored(self):
        self.assertEqual(_ga_new(self.CFG).handle(_notice("group_ban")), [])

    def test_group_filter(self):
        cfg = {"anti_recall": {"enabled": True, "groups": [OTHER_GID]}}
        ga = _ga_new(cfg)
        ga.note_message(_msg("原文", mid=7))
        self.assertEqual(ga.handle(_notice("group_recall", mid=7)), [])


class TestCommands(unittest.TestCase):
    def test_non_owner_gets_no_exec_intent(self):
        ga = _ga_new()
        for text in ("/ban 12345 60", "/kick 12345", "/recall 9", "/essence 9",
                     "/card 12345 坏话", "/unban 12345"):
            with self.subTest(text=text):
                out = ga.handle(_msg(text, uid=STRANGER))
                self.assertEqual(_exec_actions(out), [])
                self.assertEqual([i.action for i in out], [_ga.ACTION_INTERNAL])
                self.assertIn("拒绝", out[0].note)

    def test_non_owner_deny_note_can_be_silenced(self):
        ga = _ga_new({"commands": {"deny_note": False}})
        self.assertEqual(ga.handle(_msg("/ban 12345", uid=STRANGER)), [])

    def test_no_owner_configured_means_nobody(self):
        ga = _ga_new({}, owners=())
        self.assertEqual(_exec_actions(ga.handle(_msg("/ban 12345", uid=OWNER))), [])

    def test_owner_ban(self):
        out = _ga_new().handle(_msg("/ban 12345 60", uid=OWNER))
        self.assertEqual(_actions(out), ["set_group_ban"])
        self.assertEqual(out[0].params["duration"], 60)
        self.assertEqual(str(out[0].params["user_id"]), "12345")
        self.assertEqual(str(out[0].params["group_id"]), GID)

    def test_owner_ban_with_at_segment(self):
        segs = [{"type": "text", "data": {"text": "/ban "}},
                {"type": "at", "data": {"qq": "12345"}},
                {"type": "text", "data": {"text": " 120"}}]
        out = _ga_new().handle(_msg(uid=OWNER, segs=segs))
        self.assertEqual(out[0].params["duration"], 120)
        self.assertEqual(str(out[0].params["user_id"]), "12345")

    def test_ban_default_seconds_and_arg_errors(self):
        ga = _ga_new({"commands": {"default_ban_seconds": 600}})
        out = ga.handle(_msg("/ban 12345", uid=OWNER))
        self.assertEqual(out[0].params["duration"], 600)
        for bad in ("/ban", "/ban 不是号码"):
            with self.subTest(bad=bad):
                out = ga.handle(_msg(bad, uid=OWNER))
                self.assertEqual(_exec_actions(out), [])
                self.assertTrue(out and out[0].note)

    def test_ban_duration_is_capped(self):
        ga = _ga_new({"commands": {"max_ban_seconds": 3600}})
        out = ga.handle(_msg("/ban 12345 99999999", uid=OWNER))
        self.assertEqual(out[0].params["duration"], 3600)

    def test_unban(self):
        out = _ga_new().handle(_msg("/解禁 12345", uid=OWNER))
        self.assertEqual(_actions(out), ["set_group_ban"])
        self.assertEqual(out[0].params["duration"], 0)

    def test_kick(self):
        out = _ga_new().handle(_msg("/kick 12345 1", uid=OWNER))
        self.assertEqual(_actions(out), ["set_group_kick"])
        self.assertTrue(out[0].params["reject_add_request"])

    def test_card(self):
        out = _ga_new().handle(_msg("/card 12345 新名字 真好看", uid=OWNER))
        self.assertEqual(_actions(out), ["set_group_card"])
        self.assertEqual(out[0].params["card"], "新名字 真好看")

    def test_recall_with_message_id(self):
        out = _ga_new().handle(_msg("/recall 999", uid=OWNER))
        self.assertEqual(_actions(out), ["delete_msg"])
        self.assertEqual(str(out[0].params["message_id"]), "999")

    def test_recall_uses_replied_message_when_no_arg(self):
        segs = [{"type": "reply", "data": {"id": "8888"}},
                {"type": "text", "data": {"text": "/recall"}}]
        out = _ga_new().handle(_msg(uid=OWNER, segs=segs))
        self.assertEqual(_actions(out), ["delete_msg"])
        self.assertEqual(str(out[0].params["message_id"]), "8888")

    def test_recall_without_arg_and_without_reply(self):
        out = _ga_new().handle(_msg("/recall", uid=OWNER))
        self.assertEqual(_exec_actions(out), [])
        self.assertTrue(out[0].note)

    def test_essence_and_unessence(self):
        ga = _ga_new()
        self.assertEqual(_actions(ga.handle(_msg("/essence 77", uid=OWNER))), ["set_essence"])
        self.assertEqual(_actions(ga.handle(_msg("/取消精华 77", uid=OWNER))), ["delete_essence"])

    def test_command_group_whitelist(self):
        ga = _ga_new({"commands": {"groups": [OTHER_GID]}})
        self.assertEqual(_exec_actions(ga.handle(_msg("/ban 12345", uid=OWNER))), [])
        self.assertEqual(_exec_actions(ga.handle(_msg("/ban 12345", uid=OWNER, gid=OTHER_GID))),
                         ["set_group_ban"])

    def test_unknown_command_falls_through_to_keywords(self):
        ga = _ga_new({"keywords": [{"pattern": "help", "reply": "这是帮助"}]})
        out = ga.handle(_msg("/help", uid=STRANGER))
        self.assertEqual(_actions(out), ["send_group_msg"])
        self.assertEqual(out[0].kind, "keyword_reply")

    def test_prefix_is_configurable(self):
        ga = _ga_new({"commands": {"prefix": "!"}})
        self.assertEqual(_exec_actions(ga.handle(_msg("/ban 12345", uid=OWNER))), [])
        self.assertEqual(_exec_actions(ga.handle(_msg("!ban 12345", uid=OWNER))), ["set_group_ban"])

    def test_commands_can_be_disabled(self):
        ga = _ga_new({"commands": {"enabled": False}})
        self.assertEqual(_exec_actions(ga.handle(_msg("/ban 12345", uid=OWNER))), [])

    def test_stats_command_returns_internal_note(self):
        ga = _ga_new()
        ga.note_message(_msg("说了话"))
        out = ga.handle(_msg("/统计", uid=OWNER))
        self.assertEqual(_actions(out), [_ga.ACTION_INTERNAL])
        self.assertIn("统计", out[0].note)


class TestCacheAndStats(unittest.TestCase):
    def test_cache_capacity_is_bounded(self):
        ga = _ga_new({"cache_max": 3})
        for mid in range(1, 11):
            ga.note_message(_msg("第%d条" % mid, mid=mid))
        self.assertEqual(ga.cache.size(GID), 3)
        self.assertIsNone(ga.cache.get(GID, "1"))
        self.assertIsNotNone(ga.cache.get(GID, "10"))

    def test_cache_dedupes_same_message_id(self):
        ga = _ga_new({"cache_max": 5})
        for _ in range(5):
            ga.note_message(_msg("同一句", mid=7))
        self.assertEqual(ga.cache.size(GID), 1)

    def test_cache_keeps_groups_separate(self):
        ga = _ga_new()
        ga.note_message(_msg("A", mid=1, gid=GID))
        ga.note_message(_msg("B", mid=2, gid=OTHER_GID))
        self.assertEqual(ga.cache.size(GID), 1)
        self.assertEqual(ga.cache.size(OTHER_GID), 1)

    def test_note_message_ignores_non_group_events(self):
        ga = _ga_new()
        ga.note_message(_notice("group_recall"))
        ga.note_message({"post_type": "meta_event"})
        ga.note_message({"post_type": "message", "message_type": "private",
                         "user_id": STRANGER, "message_id": 5})
        self.assertEqual(ga.cache.size(GID), 0)

    def test_stats_counts_messages_users_and_top(self):
        ga = _ga_new()
        for i in range(3):
            ga.note_message(_msg("甲%d" % i, mid=10 + i, uid=STRANGER, name="甲"))
        ga.note_message(_msg("乙", mid=20, uid="30003", name="乙"))
        s = ga.stats(GID)
        self.assertEqual(s["messages"], 4)
        self.assertEqual(s["users"], 2)
        self.assertEqual(s["top_user"], "甲")
        self.assertEqual(s["top_count"], 3)

    def test_stats_ignores_self_messages(self):
        ga = _ga_new()
        ga.note_message(_msg("她说的", mid=1, uid=SELF))
        self.assertEqual(ga.stats(GID)["messages"], 0)

    def test_stats_ignores_qq_guanjia(self):
        ga = _ga_new()
        ga.note_message(_msg("管家", mid=1, uid="2854196310"))
        self.assertEqual(ga.stats(GID)["messages"], 0)

    def test_stats_user_cap_is_observable(self):
        ga = _ga_new({"stats": {"max_users": 1}})
        ga.note_message(_msg("1", mid=1, uid="100"))
        ga.note_message(_msg("2", mid=2, uid="101"))
        s = ga.stats(GID)
        self.assertEqual(s["users"], 1)
        self.assertEqual(s["messages"], 1)
        self.assertEqual(ga._stats.dropped_users, 1)

    def test_stats_empty_group(self):
        s = _ga_new().stats(GID)
        self.assertEqual((s["messages"], s["users"], s["top"]), (0, 0, []))


class TestConfigMerge(unittest.TestCase):
    def test_defaults_present_and_conservative(self):
        cfg = _ga.merge_config(None)
        self.assertFalse(cfg["welcome"]["enabled"])
        self.assertFalse(cfg["leave"]["enabled"])
        self.assertFalse(cfg["banned_words"]["enabled"])
        self.assertFalse(cfg["anti_recall"]["enabled"])
        self.assertEqual(cfg["request"]["policy"], "manual")
        self.assertEqual(cfg["keywords"], [])

    def test_partial_override_keeps_siblings(self):
        cfg = _ga.merge_config({"welcome": {"enabled": True}})
        self.assertTrue(cfg["welcome"]["enabled"])
        self.assertEqual(cfg["welcome"]["cooldown_s"], _ga.DEFAULT_CONFIG["welcome"]["cooldown_s"])

    def test_non_dict_cfg_is_safe(self):
        self.assertEqual(_ga.merge_config(["nope"])["request"]["policy"], "manual")  # type: ignore[arg-type]

    def test_group_allows_semantics(self):
        self.assertTrue(_ga._group_allows([], GID))
        self.assertTrue(_ga._group_allows([GID], GID))
        self.assertFalse(_ga._group_allows([OTHER_GID], GID))

    def test_uid_parsing(self):
        self.assertEqual(_ga._parse_uid("12345"), "12345")
        self.assertEqual(_ga._parse_uid("[@12345]"), "12345")
        self.assertEqual(_ga._parse_uid("[CQ:at,qq=12345]"), "12345")
        self.assertEqual(_ga._parse_uid("abc"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
