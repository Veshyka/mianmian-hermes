"""信任档（Trust Tier）单测：主人 / 群内 / 陌生私聊 三种情形 + 「分档 ≠ 准入」。

覆盖的是主人 2026-09-23 拍板的那条规则：
    私聊准入保持**全开**（陌生人也回），但**除主人外一律按群聊生人同款对待**。

所以这里有两组断言，**两组都不能少**：
  1. **档位判定**：谁在什么场合算 owner / stranger（`trust.classify`）；
  2. **准入没被改动**：陌生私聊在 `dm_policy: open` 下**照样放行**——
     分档是**判据**，不是准入（回归守卫：以后有人拿 trust 去收窄 dm_policy 就会被这条测出来）。

⚠️ 文件名用 `check_` 前缀而非 `test_` —— Hermes 的 disk-cleanup 插件会删 `test_*`
（详见 check_segmentation.py 头注）。

运行： bash tests/run_tests.sh            （全量）
      /opt/hermes/.venv/bin/python tests/check_trust.py   （单文件）
"""

from __future__ import annotations

import importlib
import os
import sys
import unittest

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # …/hermes_onebot
_PKG = os.path.basename(_PLUGIN_DIR)
sys.path.insert(0, os.path.dirname(_PLUGIN_DIR))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_trust = importlib.import_module(f"{_PKG}.trust")   # noqa: E402

OWNER = "<OWNER_QQ>"     # 主人大号（QQ uid）
STRANGER = "99912345"    # 陌生人

# 适配器接线检查需要 gateway.*（只有 Hermes 自带解释器有）→ 缺了只跳这一组。
try:
    from gateway.config import PlatformConfig  # noqa: E402
    _adapter_mod = importlib.import_module(f"{_PKG}.adapter")
    OneBotAdapter = _adapter_mod.OneBotAdapter
    _GATEWAY_OK = True
except Exception:          # pragma: no cover - 系统 python3 下会走这里
    _GATEWAY_OK = False


def _dm_event(uid, nickname="某人"):
    return {
        "post_type": "message", "message_type": "private", "sub_type": "friend",
        "self_id": <BOT_QQ>, "user_id": uid, "message_id": 1, "time": 0,
        "sender": {"user_id": uid, "nickname": nickname},
        "raw_message": "hi", "message": [{"type": "text", "data": {"text": "hi"}}],
    }


def _group_event(gid, uid, nickname="某人"):
    return {
        "post_type": "message", "message_type": "group", "sub_type": "normal",
        "self_id": <BOT_QQ>, "group_id": gid, "user_id": uid, "message_id": 2, "time": 0,
        "sender": {"user_id": uid, "nickname": nickname, "card": ""},
        "raw_message": "hi", "message": [{"type": "text", "data": {"text": "hi"}}],
    }


class TestResolveOwnerIds(unittest.TestCase):
    """配置解析：YAML 里不写引号的 QQ 号是 int —— 这是最常见的坑。"""

    def test_missing_falls_back_to_owner_big_account(self):
        self.assertEqual(_trust.resolve_owner_ids(None), _trust.DEFAULT_OWNER_IDS)
        self.assertEqual(_trust.DEFAULT_OWNER_IDS, (OWNER,))

    def test_empty_never_makes_owner_a_stranger(self):
        for v in ([], "", "   ", None, {}):
            self.assertIn(OWNER, _trust.resolve_owner_ids(v), f"空值 {v!r} 不许把主人踢出生人档")

    def test_list_with_int_qq(self):
        self.assertEqual(_trust.resolve_owner_ids([<OWNER_QQ>, "123456"]), (OWNER, "123456"))

    def test_comma_and_space_separated_string(self):
        self.assertEqual(_trust.resolve_owner_ids("<OWNER_QQ>, 123456;789"),
                         (OWNER, "123456", "789"))

    def test_dedup_keeps_order(self):
        self.assertEqual(_trust.resolve_owner_ids(["1", 1, "1", "2"]), ("1", "2"))


class TestClassify(unittest.TestCase):
    """三种情形（主人私聊 / 群内 / 陌生私聊）是本文件的核心。"""

    def test_owner_dm_is_owner(self):
        self.assertEqual(_trust.classify(OWNER), _trust.TIER_OWNER)

    def test_owner_dm_with_int_uid_is_owner(self):
        self.assertEqual(_trust.classify(<OWNER_QQ>), _trust.TIER_OWNER)

    def test_stranger_dm_is_stranger(self):
        self.assertEqual(_trust.classify(STRANGER), _trust.TIER_STRANGER)

    def test_group_is_always_stranger_even_for_owner(self):
        """公共场合规则：群里一律生人档，**主人本人在场也不变私密**。"""
        self.assertEqual(_trust.classify(OWNER, is_group=True), _trust.TIER_STRANGER)
        self.assertEqual(_trust.classify(STRANGER, is_group=True), _trust.TIER_STRANGER)

    def test_identity_axis_is_venue_independent(self):
        """身份轴（用来认出主人）与档位轴是两回事：他在群里仍是 owner 身份。"""
        self.assertTrue(_trust.is_owner(OWNER))
        self.assertFalse(_trust.is_owner(STRANGER))
        self.assertTrue(_trust.is_public(is_group=True))
        self.assertFalse(_trust.is_public(is_group=False))

    def test_unknown_or_empty_uid_is_stranger(self):
        for uid in (None, "", "   ", 0):
            self.assertEqual(_trust.classify(uid), _trust.TIER_STRANGER, f"uid={uid!r}")

    def test_custom_owner_ids(self):
        self.assertEqual(_trust.classify("123456", owner_ids=["123456"]), _trust.TIER_OWNER)
        # 自定义名单里没有主人大号 → 主人大号按名单判（配置是唯一真源）
        self.assertEqual(_trust.classify(OWNER, owner_ids=["123456"]), _trust.TIER_STRANGER)

    def test_tier_values_are_stable_strings(self):
        """档位字面量是契约（提示词/日志会引用）：改字面量＝破坏性变更。"""
        self.assertEqual((_trust.TIER_OWNER, _trust.TIER_STRANGER), ("owner", "stranger"))
        self.assertEqual(_trust.VALID_TIERS, ("owner", "stranger"))


@unittest.skipUnless(_GATEWAY_OK, "需要 Hermes 自带的 gateway 包（用 run_tests.sh 跑）")
class TestAdapterWiring(unittest.TestCase):
    """适配器只**读名单**：默认值正确、但不改准入。"""

    @classmethod
    def setUpClass(cls):
        # 生产里由插件发现调 register(ctx) 完成；测试里手动等价压一次，
        # 否则 `Platform("onebot")` 会抛 ValueError（见 check_adapter_e2e 同款做法）。
        from gateway.platform_registry import PlatformEntry, platform_registry
        if not platform_registry.is_registered("onebot"):
            platform_registry.register(PlatformEntry(
                name="onebot", label="OneBot v11", adapter_factory=OneBotAdapter,
                check_fn=lambda: True, source="plugin", plugin_name="hermes_onebot"))

    def _adapter(self, **extra):
        base = {"access_token": "", "group_window_dir": "/tmp/onebot-trust-test-groups"}
        base.update(extra)
        return OneBotAdapter(PlatformConfig(enabled=True, extra=base))

    def test_default_owner_ids(self):
        a = self._adapter()
        self.assertEqual(tuple(a.trust_tier_owner_ids), (OWNER,))
        self.assertEqual(a.trust_tier(OWNER), _trust.TIER_OWNER)
        self.assertEqual(a.trust_tier(STRANGER), _trust.TIER_STRANGER)

    def test_config_override_read(self):
        a = self._adapter(**{_trust.CONFIG_KEY: [<OWNER_QQ>, "123456"]})
        self.assertEqual(tuple(a.trust_tier_owner_ids), (OWNER, "123456"))
        self.assertEqual(a.trust_tier("123456"), _trust.TIER_OWNER)

    def test_empty_config_falls_back(self):
        a = self._adapter(**{_trust.CONFIG_KEY: []})
        self.assertIn(OWNER, tuple(a.trust_tier_owner_ids))

    def test_group_is_stranger_for_owner(self):
        a = self._adapter()
        self.assertEqual(a.trust_tier(OWNER, is_group=True), _trust.TIER_STRANGER)

    def test_tiering_does_not_touch_dm_policy_or_allow_from(self):
        """★ 回归守卫：分档是判据，不是准入 —— 全开照旧。"""
        a = self._adapter(dm_policy="open", allow_from=["*"])
        self.assertEqual(a.dm_policy, "open")
        self.assertEqual(a.allow_from, {"*"})
        # 陌生人的私聊：档位是生人，但**照样放行**
        ev = _dm_event(STRANGER)
        self.assertEqual(a.trust_tier(STRANGER), _trust.TIER_STRANGER)
        self.assertTrue(a._authorized_sender(ev), "open 策略下陌生私聊必须放行（准入全开）")
        # 主人的私聊同样放行
        self.assertTrue(a._authorized_sender(_dm_event(OWNER)))

    def test_group_authorization_unchanged_by_tier(self):
        """群授权只看 group_enabled（现状），档位不参与。"""
        a = self._adapter(group_enabled=True, group_ids=["55555"])
        ev = _group_event("55555", STRANGER)
        self.assertEqual(a.trust_tier(STRANGER, is_group=True), _trust.TIER_STRANGER)
        self.assertTrue(a._authorized_sender(ev))


if __name__ == "__main__":
    unittest.main(verbosity=2)
