"""端到端原型验证：假 NapCat ↔ 适配器反向 WS 真跑一遍。

覆盖（== 交付物「最小原型」的验收）：
  1. 反向 WS 握手 + token 鉴权（错 token 必须被拒）
  2. 入站私聊 → 防抖合并 → 一轮只交给模型一次
  3. 会话标识稳定（build_session_key 复算，确认不是每句新开对话）
  4. 出站分段：`※` 拆多条、`※` 不进消息、段间有间隔、失败有重试且不静默丢
  5. 超阈值整条发；read_only 默认拦截出站但入站照收
  6. 未授权发送者 / QQ 管家 / 未开启的群聊 被丢弃

**全程只连自己进程内的假协议端** —— 不碰 NapCat / AstrBot / 小号。
⚠️ 文件名用 `check_` 前缀（disk-cleanup 会删 test_*，见 check_segmentation.py 头注）。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import re
import time
import socket
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))     # …/hermes_onebot
_PLUGIN_PARENT = os.path.dirname(_PLUGIN_DIR)                                 # …/plugin
_PKG = os.path.basename(_PLUGIN_DIR)
sys.path.insert(0, _PLUGIN_PARENT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gateway.config import PlatformConfig, Platform  # noqa: E402
from gateway.session import build_session_key  # noqa: E402

_adapter_mod = importlib.import_module(f"{_PKG}.adapter")
_gw = importlib.import_module(f"{_PKG}.group_window")
_gwk = importlib.import_module(f"{_PKG}.group_wake")
_gadmin = importlib.import_module(f"{_PKG}.group_admin")
OneBotAdapter = _adapter_mod.OneBotAdapter
from mock_onebot import MockNapCat  # noqa: E402


def _ensure_platform_registered() -> None:
    """生产里由插件发现调 register(ctx) 完成；测试里手动等价压一次。

    没这一步 `Platform("onebot")` 会抛 ValueError —— 这正好证明
    「适配器构造依赖插件已注册」这个真实前提。
    """
    from gateway.platform_registry import PlatformEntry, platform_registry
    if not platform_registry.is_registered("onebot"):
        platform_registry.register(PlatformEntry(
            name="onebot", label="OneBot v11", adapter_factory=OneBotAdapter,
            check_fn=lambda: True, source="plugin", plugin_name="hermes_onebot"))


_ensure_platform_registered()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class AdapterHarness(unittest.IsolatedAsyncioTestCase):
    """把平台锁与 handle_message 换成测试替身，其余全走真代码路径。"""

    TOKEN = \"<SECRET>\"

    async def asyncSetUp(self):
        import tempfile
        self.dispatched: List[Any] = []
        self.port = _free_port()
        self._tmp = tempfile.mkdtemp(prefix="onebot-test-")
        # ★ 单测永远不许把 profile 指到真目录（2026-10-04 两次踩雷：① 用例把 /tmp 假图写进她真库索引；
        #   ② shell 里 export 过 HERMES_HOME=真 profile 时，「记忆隔离硬前置」用例把闸门判据算成"就绪"→ 误判）。
        self._saved_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = self._tmp
        self.adapter = OneBotAdapter(PlatformConfig(
            enabled=True,
            extra={
                "ws_host": "127.0.0.1", "ws_port": self.port, "access_token": \"<SECRET>\",
                "read_only": False, "dm_policy": "allowlist", "allow_from": ["10001"],
                "debounce_enabled": True, "debounce_seconds": 0.35, "debounce_max_seconds": 3.0,
                "debounce_scope": "private",
                "segment_enabled": True,
                "segment_interval": "0.15,0.25",
                # 群窗口也落临时目录（别碰 profile 真实路径）
                "group_window_dir": os.path.join(self._tmp, "onebot-groups"),
                # 表情包库也落临时目录（2026-10-04 漏水：用例把 /tmp 的假图写进了她真库的索引）
                "stickers_dir": os.path.join(self._tmp, "onebot-stickers"),
                # 群唤醒默认档位在单测里显式声明：**不依赖 profile 真实配置**
                # （记忆隔离硬前置默认 true，会把「未装闸」的测试环境全拦掉 —— 那是生产语义）
                "group_wake_mode": "collect-only",
                "group_memory_guard_required": False,
                # 自检落盘改到临时目录，别碰 profile 真实路径
                "state_path": os.path.join(self._tmp, "onebot-state.json"),
                "alert_log_path": os.path.join(self._tmp, "onebot-alerts.log"),
                "alert_report_url": "",          # 测试里不发真告警
                "alert_cooldown_seconds": 0,
            },
        ))
        self.adapter._acquire_platform_lock = lambda *a, **k: True
        self.adapter._release_platform_lock = lambda *a, **k: None

        async def _capture(event):
            self.dispatched.append(event)
        self.adapter.handle_message = _capture  # type: ignore[assignment]

        self.assertTrue(await self.adapter.connect())

    async def asyncTearDown(self):
        if self._saved_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._saved_home
        await self.adapter.disconnect()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/onebot/v11/ws"


class TestAuth(AdapterHarness):
    async def test_bad_token_rejected(self):
        rejected = False
        try:
            async with MockNapCat(self.url, token=\"<SECRET>\") as client:
                await client.push_private("你好")
                await asyncio.sleep(0.05)
        except Exception:
            rejected = True
        self.assertTrue(rejected, "错 token 必须连不上")
        self.assertEqual(self.dispatched, [])

    async def test_good_token_accepted(self):
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await client.push_private("你好")
            await asyncio.sleep(0.05)
            self.assertTrue(client.connected.is_set())


class TestInboundDebounce(AdapterHarness):
    async def test_burst_merges_into_one_turn(self):
        """连发三条 → 只交给模型一轮，且内容是合并的。"""
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await client.push_private("在吗", message_id=1)
            await asyncio.sleep(0.1)
            await client.push_private("我想问你个事", message_id=2)
            await asyncio.sleep(0.1)
            await client.push_private("关于那个配置", message_id=3)
            await asyncio.sleep(1.2)

        self.assertEqual(len(self.dispatched), 1, f"期望一轮，实得 {len(self.dispatched)}")
        self.assertEqual(self.dispatched[0].text, "在吗\n我想问你个事\n关于那个配置")

    async def test_separate_bursts_are_separate_turns(self):
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await client.push_private("第一波", message_id=1)
            await asyncio.sleep(1.0)
            await client.push_private("第二波", message_id=2)
            await asyncio.sleep(1.0)
        self.assertEqual([e.text for e in self.dispatched], ["第一波", "第二波"])

    async def test_command_bypasses_debounce(self):
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await client.push_private("/new", message_id=1)
            await asyncio.sleep(0.15)
        self.assertEqual(len(self.dispatched), 1)
        self.assertEqual(self.dispatched[0].text, "/new")

    async def test_hard_cap_releases_under_continuous_chatter(self):
        """硬上限到点必放行（3.0s 上限 / 0.35s 窗口，持续说话也要出得来）。"""
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            for i in range(30):
                await client.push_private(f"碎念{i}", message_id=i + 1)
                await asyncio.sleep(0.12)
            await asyncio.sleep(1.0)
        self.assertGreaterEqual(len(self.dispatched), 1)

    async def test_group_message_ignored_by_default(self):
        """group_enabled 默认 false → 群聊整条不进（防抖也默认仅私聊）。"""
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await client.push_group("群里说话", group_id="55555", user_id="10001")
            await asyncio.sleep(0.8)
        self.assertEqual(self.dispatched, [])

    async def test_unauthorized_sender_dropped(self):
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await client.push_private("陌生人", user_id="99999")
            await asyncio.sleep(0.8)
        self.assertEqual(self.dispatched, [])

    async def test_qq_guanjia_dropped(self):
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await client.push_private("管家提示", user_id="2854196310")
            await asyncio.sleep(0.5)
        self.assertEqual(self.dispatched, [])


class TestSessionStability(AdapterHarness):
    async def test_session_key_is_stable_across_turns(self):
        """**核心诉求**：会话 id 稳定，别每句新开对话。"""
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            for i in range(4):
                await client.push_private(f"第{i}条", message_id=i + 1)
                await asyncio.sleep(0.8)

        self.assertGreaterEqual(len(self.dispatched), 2)
        keys = {build_session_key(e.source) for e in self.dispatched}
        self.assertEqual(len(keys), 1, f"会话键必须唯一，实得 {keys}")
        self.assertIn(":onebot:dm:10001", keys.pop())

    async def test_source_shape(self):
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await client.push_private("看看来源", user_id="10001", nickname="主人")
            await asyncio.sleep(0.8)
        src = self.dispatched[0].source
        self.assertEqual(src.platform.value, "onebot")
        self.assertEqual(src.chat_type, "dm")
        self.assertEqual(src.chat_id, "10001")     # chat_id 稳定 = 会话稳定
        self.assertEqual(src.user_id, "10001")

    async def test_group_source_and_per_sender_keying(self):
        """**唤醒打开时**：键 = 会话 + 发送者（不同人不混）。

        `collect-only`（默认）→ 群消息走不到这里（见 `TestGroupCollectOnly`）；
        本用例把模式显式切到 `full` 只为守住会话键这条老承诺；C1 的真正形态
        （只在 @ 时唤醒）见 `TestGroupMentionWake`。
        """
        self.adapter.group_enabled = True
        self.adapter.group_wake_mode = "full"
        self.adapter.group_wake_enabled = True
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await client.push_group("甲说话", group_id="55555", user_id="10001")
            await asyncio.sleep(0.8)
        src = self.dispatched[0].source
        self.assertEqual(src.chat_type, "group")
        self.assertEqual(src.chat_id, "55555")
        self.assertEqual(src.user_id, "10001")
        self.assertIn(":onebot:group:55555:10001", build_session_key(src))


class TestGroupCollectOnly(AdapterHarness):
    """★ B 阶段验收：「只看不说」—— 群消息全部收进滚动窗口，**一次 LLM 都不起**。"""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.adapter.group_enabled = True          # 群开关：开
        self.adapter.group_wake_enabled = False    # 唤醒：关（B 阶段红线）

    async def test_group_messages_collected_but_never_reach_agent(self):
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await client.push_group("群里聊两句", group_id="55555", user_id="10001",
                                    message_id=1, nickname="甲")
            await client.push_group("这图有意思", group_id="55555", user_id="20002",
                                    message_id=2, nickname="乙")
            await asyncio.sleep(0.8)

        self.assertEqual(self.dispatched, [], "群消息**绝不能**进 agent（0 次 LLM 调用）")
        self.assertEqual(self.adapter._group_rx_count, 2)
        self.assertEqual(self.adapter._group_llm_calls, 0)

        recs = self.adapter.group_window.tail("55555")
        self.assertEqual([r["text"] for r in recs], ["群里聊两句", "这图有意思"])
        self.assertEqual([r["name"] for r in recs], ["甲", "乙"])
        self.assertEqual([r["uid"] for r in recs], ["10001", "20002"])

    async def test_pure_media_group_message_is_collected(self):
        """群里只发图/只发表情 —— 也算一条（否则「她看不到前面聊了啥」）。"""
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await client.push({
                "post_type": "message", "message_type": "group", "sub_type": "normal",
                "self_id": <BOT_QQ>, "group_id": 55555, "user_id": 10001, "message_id": 7,
                "time": int(time.time()), "sender": {"user_id": 10001, "nickname": "甲", "card": ""},
                "message": [{"type": "face", "data": {"id": "14"}}], "raw_message": "",
            })
            await asyncio.sleep(0.6)
        self.assertEqual(self.dispatched, [])
        recs = self.adapter.group_window.tail("55555")
        self.assertEqual([r["text"] for r in recs],
                         # 9-23 第二批：表情占位带 id
                         ["[表情:14]"])

    async def test_window_is_isolated_per_group(self):
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await client.push_group("一群的话", group_id="55555", user_id="10001")
            await client.push_group("二群的话", group_id="66666", user_id="10001")
            await asyncio.sleep(0.8)
        self.assertEqual([r["text"] for r in self.adapter.group_window.tail("55555")], ["一群的话"])
        self.assertEqual([r["text"] for r in self.adapter.group_window.tail("66666")], ["二群的话"])

    async def test_window_survives_adapter_restart(self):
        """重建容器/重启网关后窗口还在（落 profile 数据目录，不落内存）。"""
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await client.push_group("重启也要记得", group_id="55555", user_id="10001")
            await asyncio.sleep(0.6)
        reloaded = _gw.GroupWindow(self.adapter.group_window_dir)
        self.assertEqual([r["text"] for r in reloaded.tail("55555")], ["重启也要记得"])

    async def test_state_file_is_the_evidence(self):
        """**0 次 LLM 调用的证据**就在心跳文件里（doctor 与人肉排查都读它）。"""
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await client.push_group("留个痕", group_id="55555", user_id="10001")
            await asyncio.sleep(0.6)
        self.adapter.write_state_file()
        state = json.loads(Path(self.adapter.state_path).read_text(encoding="utf-8"))
        self.assertTrue(state["group_enabled"])
        self.assertFalse(state["group_wake_enabled"])
        self.assertEqual(state["group_mode"], "collect-only(0 LLM)")
        self.assertGreaterEqual(state["group_rx_count"], 1)
        self.assertEqual(state["group_llm_calls"], 0)
        self.assertIn("未入主库", state["group_window"])
        self.assertEqual(state["group_window_errors"], 0)

    async def test_group_dispatch_is_blocked_even_if_called_directly(self):
        """纵深防御：绕过 `_ingest` 直接调 `_dispatch` 也拦得住（红线不靠一处 return）。"""
        await self.adapter._dispatch("硬闯的一条", {"message_id": 1},
                                     is_group=True, gid="55555", uid="10001")
        self.assertEqual(self.dispatched, [])
        self.assertEqual(self.adapter._group_llm_calls, 0)

    async def test_full_mode_wakes_and_counts(self):
        """`full` 档：每条群消息都唤醒（老 `group_wake_enabled: true` 的语义，排障用，**未验收**）。"""
        self.adapter.group_wake_mode = "full"
        self.adapter.group_wake_enabled = True
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await client.push_group("随便一句", group_id="55555", user_id="10001")
            await asyncio.sleep(0.8)
        self.assertEqual(len(self.dispatched), 1)
        self.assertEqual(self.adapter._group_llm_calls, 1)
        self.assertEqual(self.adapter._group_full_count, 1)

    async def test_group_enabled_false_drops_before_collection(self):
        self.adapter.group_enabled = False
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await client.push_group("开关关着", group_id="55555", user_id="10001")
            await asyncio.sleep(0.5)
        self.assertEqual(self.dispatched, [])
        self.assertEqual(self.adapter._group_rx_count, 0)
        self.assertEqual(self.adapter.group_window.tail("55555"), [])


class TestGroupMentionWake(AdapterHarness):
    """★ C1 验收：「@ 必答」—— 只有 @ / 被回复 / 点名才唤醒，其余与 B 阶段逐字一致。"""

    async def asyncSetUp(self):  # noqa: D102
        await super().asyncSetUp()
        self.adapter.group_enabled = True
        self.adapter.group_wake_mode = "mention-only"
        self.adapter.group_wake_enabled = True
        # 小号 QQ 号：@ 判据拿它比对（生产里由 ONEBOT_SELF_ID 注入）
        self.adapter.self_id = "90001"
        # 限流放到很宽（单独用例再收紧），免得连续 push 撞上限
        self.adapter._group_wake_limiter = _gwk.WakeLimiter(per_minute=50, per_hour=500)

    @staticmethod
    def _ev(text: str = "", segs: list | None = None, *, uid: str = "10001",
            gid: str = "55555", mid: int = 1) -> Dict[str, Any]:
        """一条 OneBot 群消息事件（真实形状，不走 WS，直接喂 `_ingest`）。"""
        if segs is None:
            segs = [{"type": "text", "data": {"text": text}}]
        return {
            "post_type": "message", "message_type": "group", "sub_type": "normal",
            "group_id": gid, "user_id": uid, "message_id": mid, "message": segs,
            "sender": {"nickname": f"甲{uid[-2:]}"}, "time": int(time.time()),
        }

    async def test_plain_group_message_does_not_wake(self):
        """非 @ 的群消息：进窗口、0 唤醒、0 LLM —— 与 B 阶段逐字一致。"""
        await self.adapter._ingest(self._ev("我中午吃面"))
        self.assertEqual(self.dispatched, [])
        self.assertEqual(self.adapter._group_llm_calls, 0)
        self.assertEqual(self.adapter._group_rx_count, 1)
        self.assertEqual(len(self.adapter.group_window.tail("55555")), 1)
        self.assertEqual(self.adapter._group_wake_blocked, 0)

    async def test_at_wakes_with_window_context(self):
        """@ 小号 → 唤醒一轮，**上下文是本群滚动窗口**（带昵称 + 来源标记）。"""
        await self.adapter._ingest(self._ev("前面这句只是背景", mid=1))
        await self.adapter._ingest(self._ev(
            "你在吗", mid=2, segs=[{"type": "at", "data": {"qq": "90001"}},
                                   {"type": "text", "data": {"text": " 你在吗"}}]))
        self.assertEqual(len(self.dispatched), 1)
        text = self.dispatched[0].text
        self.assertIn("source=qq-group", text)          # 记忆隔离闸靠这个标记认群回合
        self.assertIn("前面这句只是背景", text)           # 窗口内容进了上下文
        self.assertIn("你在吗", text)
        self.assertIn("甲01", text)                      # 带昵称
        self.assertIn("触发=被@", text)                   # 触发原因写清楚（进日志/心跳同名）
        self.assertEqual(self.adapter._group_mention_count, 1)
        self.assertEqual(self.adapter._group_llm_calls, 1)

    async def test_reply_to_her_wakes(self):
        """有人**回复她说过的话** → 唤醒（判据 = 那条 id 是她发的）。"""
        self.adapter._remember_own_mids("55555", [777])
        await self.adapter._ingest(self._ev(
            "接着聊", mid=3, segs=[{"type": "reply", "data": {"id": 777}},
                                  {"type": "text", "data": {"text": "接着聊"}}]))
        self.assertEqual(len(self.dispatched), 1)
        self.assertEqual(self.adapter._group_reply_count, 1)

    async def test_reply_to_someone_else_does_not_wake(self):
        """回复**别人**的消息不算 —— 别把整个群的引用都当她的事。"""
        await self.adapter._ingest(self._ev(
            "我也说一句", mid=4, segs=[{"type": "reply", "data": {"id": 888}},
                                      {"type": "text", "data": {"text": "我也说一句"}}]))
        self.assertEqual(self.dispatched, [])

    async def test_nickname_in_text_wakes(self):
        """正文里直接点名「棉棉」→ 唤醒（主人习惯的打字方式）。"""
        await self.adapter._ingest(self._ev("棉棉你觉得呢", mid=5))
        self.assertEqual(len(self.dispatched), 1)
        self.assertEqual(self.adapter._group_name_count, 1)

    async def test_her_own_group_reply_enters_window_but_not_db(self):
        """她自己的群回复也进该群窗口（否则「接着聊」上下文是断的）——但仍不入主库。"""
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await self.adapter.send("55555", "我在⁂刚吃完", metadata={"chat_type": "group"})
            await asyncio.sleep(0.2)
        recs = self.adapter.group_window.tail("55555")
        self.assertEqual([r["text"] for r in recs], ["我在", "刚吃完"])
        self.assertTrue(all(r["name"] == "[我]" for r in recs))

    async def test_rate_limit_is_not_silent(self):
        """连点 @ 会被上限挡住，但**必须留痕**（计数 + 日志），不许静默丢。"""
        self.adapter._group_wake_limiter = _gwk.WakeLimiter(per_minute=1, per_hour=10)
        for i in range(3):
            await self.adapter._ingest(self._ev(
                f"第{i}次", mid=10 + i, segs=[{"type": "at", "data": {"qq": "90001"}},
                                             {"type": "text", "data": {"text": f" 第{i}次"}}]))
        self.assertEqual(len(self.dispatched), 1)
        self.assertEqual(self.adapter._group_wake_limited, 2)

    async def test_memory_guard_is_a_hard_precondition(self):
        """★ 硬前置：记忆隔离闸没就位 → **拒绝唤醒**（不是警告，是拒绝）。"""
        self.adapter.group_memory_guard_required = True
        self.adapter._group_iso_cache = None
        await self.adapter._ingest(self._ev(
            "你在吗", mid=20, segs=[{"type": "at", "data": {"qq": "90001"}},
                                   {"type": "text", "data": {"text": " 你在吗"}}]))
        self.assertEqual(self.dispatched, [], "闸没装上就不许在群里起付费回合")
        self.assertGreaterEqual(self.adapter._group_wake_blocked, 1)
        self.assertEqual(self.adapter._group_llm_calls, 0)

    def test_collect_only_ignores_mentions(self):
        """`collect-only`（默认档）：@ 也不唤醒 —— B 阶段语义不许被 C1 顺手改掉。"""
        self.adapter.group_wake_mode = "collect-only"
        self.adapter.group_memory_guard_required = False
        self.assertFalse(self.adapter._group_wake_trigger(
            self._ev(segs=[{"type": "at", "data": {"qq": "90001"}}]), gid="55555"))
        self.assertEqual(self.adapter._group_mention_count, 0)


class TestOutboundSegmentation(AdapterHarness):
    async def test_sep_splits_into_multiple_messages(self):
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            res = await self.adapter.send("10001", "第一句※第二句※第三句")
            self.assertTrue(res.success, res.error)
            await asyncio.sleep(0.1)
            sent = client.sent_texts("send_private_msg")

        self.assertEqual(sent, ["第一句", "第二句", "第三句"])
        self.assertFalse(any("※" in s for s in sent))
        self.assertEqual(len(res.continuation_message_ids), 3)

    async def test_segments_are_spaced_in_time(self):
        """段间必须有停顿（太快会被通道侧合并/去重）。"""
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await self.adapter.send("10001", "甲※乙※丙")
            await asyncio.sleep(0.1)
            stamps = [a["t"] for a in client.actions if a.get("action") == "send_private_msg"]

        self.assertEqual(len(stamps), 3)
        gaps = [stamps[i + 1] - stamps[i] for i in range(len(stamps) - 1)]
        for gap in gaps:
            self.assertGreaterEqual(gap, 0.12, f"段间间隔过短：{gap:.3f}s")
        self.assertEqual(stamps[0], min(stamps))   # 首条不延迟

    async def test_action_params_use_dm_key(self):
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await self.adapter.send("10001", "单体消息")
            await asyncio.sleep(0.1)
            act = client.actions[-1]
        self.assertEqual(act["action"], "send_private_msg")
        self.assertEqual(act["params"]["user_id"], 10001)     # 数字型 id
        self.assertNotIn("group_id", act["params"])

    async def test_group_send_uses_group_action(self):
        self.adapter.group_ids.add("55555")
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await self.adapter.send("55555", "群里回一句")
            await asyncio.sleep(0.1)
            act = client.actions[-1]
        self.assertEqual(act["action"], "send_group_msg")
        self.assertEqual(act["params"]["group_id"], 55555)

    async def test_separator_never_reaches_the_wire(self):
        """硬红线：`⁂`/`※` 不许出现在任何一条发出去的消息里（走真实 send 路径）。"""
        long_text = ("一、清单项，讲的是这件事本身。" * 70) + "⁂" + "※" + "⸮" + "\n二、结尾。"
        self.assertGreater(len(long_text), 1000)
        self.adapter.segment_interval = (0.0, 0.0)
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await self.adapter.send("10001", long_text)
            await asyncio.sleep(0.4)
            sent = client.sent_texts("send_private_msg")
        self.assertTrue(sent)
        for text in sent:
            for ch in ("⁂", "※", "⸮"):
                self.assertNotIn(ch, text, f"{ch!r} 漏进了发出去的消息")

    async def test_long_text_without_sep_is_one_message(self):
        """不含 `⁂` 的 1000+ 字 → 一条不切（阈值已废弃，靠「没有分隔符」实现）。"""
        long_text = ("一、清单项，讲的是这件事本身。\n" * 70)
        self.assertGreater(len(long_text), 1000)
        self.assertNotIn("⁂", long_text)
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await self.adapter.send("10001", long_text)
            await asyncio.sleep(0.15)
            sent = client.sent_texts("send_private_msg")
        self.assertEqual(len(sent), 1)

    async def test_long_text_with_sep_is_split_regardless_of_length(self):
        """1000+ 字里只要有 `⁂` 就照切 —— 不再有「太长就不切」这条路。"""
        self.adapter.segment_interval = (0.0, 0.0)   # 本用例只看切分，不等段间隔
        long_text = ("一、清单项，讲的是这件事本身。⁂" * 70) + "\n二、结尾一句。"
        self.assertGreater(len(long_text), 1000)
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await self.adapter.send("10001", long_text)
            await asyncio.sleep(0.4)
            sent = client.sent_texts("send_private_msg")
        self.assertGreater(len(sent), 50)
        for text in sent:
            self.assertNotIn("⁂", text)

    async def test_short_dialogue_is_segmented(self):
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await self.adapter.send("10001", "在啊⁂\n棉棉又不会自己跑掉⁂")
            await asyncio.sleep(0.4)
            sent = client.sent_texts("send_private_msg")
        self.assertEqual(sent, ["在啊", "棉棉又不会自己跑掉"])

    async def test_degraded_path_segmentation_disabled_still_scrubbed(self):
        """降级路径：`segment_enabled=False`（整段直发）也必须先洗掉分隔符。"""
        self.adapter.segment_enabled = False
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await self.adapter.send("10001", "甲⁂乙※丙⸮丁")
            await asyncio.sleep(0.15)
            sent = client.sent_texts("send_private_msg")
        self.assertEqual(sent, ["甲乙丙丁"])

    async def test_separators_only_sends_nothing(self):
        """整条只有分隔符 → 什么都不发，也不把符号发出去。"""
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            res = await self.adapter.send("10001", "⁂⁂⁂")
            await asyncio.sleep(0.15)
            sent = client.sent_texts("send_private_msg")
        self.assertTrue(res.success)
        self.assertEqual(sent, [])

    async def test_send_failure_is_reported_not_swallowed(self):
        self.adapter.send_retries = 1
        await self.adapter.disconnect()   # 没有协议端连接
        res = await self.adapter.send("10001", "会失败的一条")
        self.assertFalse(res.success)
        self.assertTrue(res.error)


class TestReadOnlyDefault(unittest.IsolatedAsyncioTestCase):
    async def test_read_only_blocks_outbound(self):
        port = _free_port()
        a = OneBotAdapter(PlatformConfig(enabled=True, extra={
            "ws_host": "127.0.0.1", "ws_port": port, "access_token": "t", "allow_from": ["10001"],
        }))
        a._acquire_platform_lock = lambda *x, **k: True
        a._release_platform_lock = lambda *x, **k: None
        self.assertTrue(a.read_only, "read_only 必须默认 True（硬约束：只收不发）")
        try:
            self.assertTrue(await a.connect())
            async with MockNapCat(f"http://127.0.0.1:{port}/", token="t") as client:
                res = await a.send("10001", "不该发出去")
                await asyncio.sleep(0.1)
                self.assertFalse(res.success)
                self.assertIn("read_only", (res.error or "").lower())
                self.assertEqual(client.sent_texts("send_private_msg"), [])
        finally:
            await a.disconnect()

    async def test_inbound_still_works_in_read_only(self):
        port = _free_port()
        a = OneBotAdapter(PlatformConfig(enabled=True, extra={
            "ws_host": "127.0.0.1", "ws_port": port, "access_token": "t",
            "allow_from": ["10001"], "debounce_seconds": 0.2, "debounce_max_seconds": 2,
        }))
        a._acquire_platform_lock = lambda *x, **k: True
        a._release_platform_lock = lambda *x, **k: None
        got: List[Any] = []

        async def cap(ev):
            got.append(ev)
        a.handle_message = cap  # type: ignore[assignment]
        try:
            self.assertTrue(await a.connect())
            async with MockNapCat(f"http://127.0.0.1:{port}/", token="t") as client:
                await client.push_private("只收不发也要能收")
                await asyncio.sleep(0.7)
            self.assertEqual([e.text for e in got], ["只收不发也要能收"])
        finally:
            await a.disconnect()


class TestHealthAndAlerts(AdapterHarness):
    """红线 2 的验收：状态可读、故障能自己报出来、状态里不含正文。"""

    async def test_state_file_is_written_and_fresh(self):
        self.adapter.write_state_file()
        self.assertTrue(os.path.isfile(self.adapter.state_path))
        import json as _json
        st = _json.loads(open(self.adapter.state_path, encoding="utf-8").read())
        self.assertTrue(st["listener_up"])
        self.assertEqual(st["port"], self.port)
        self.assertFalse(st["client_connected"])   # 还没有协议端

    async def test_state_reflects_connected_client(self):
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await asyncio.sleep(0.1)
            st = self.adapter.health_state()
            self.assertTrue(st["client_connected"])
            await client.push_private("有收到吗")
            await asyncio.sleep(0.5)
            st = self.adapter.health_state()
            self.assertGreater(st["rx_count"], 0)
            self.assertGreater(st["last_rx_ts"], 0)

    async def test_health_state_never_contains_message_text(self):
        secret = "晚饭吃螺蛳粉还是火锅"
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await client.push_private(secret)
            await asyncio.sleep(0.7)
        blob = json.dumps(self.adapter.health_state(), ensure_ascii=False)
        self.assertNotIn(secret, blob)

    async def test_alert_is_written_locally_even_without_http(self):
        await self.adapter._send_alert("测试告警：listener 挂了", key="listener")
        text = open(self.adapter.alert_log_path, encoding="utf-8").read()
        self.assertIn("listener 挂了", text)
        self.assertEqual(self.adapter.health_state()["alerts_sent"], 1)

    async def test_selfcheck_alerts_when_listener_down(self):
        """监听器掉了 → 自检必须自己报出来（人不用去翻日志）。"""
        self.adapter._site = None
        await self.adapter._check_and_alert()
        text = open(self.adapter.alert_log_path, encoding="utf-8").read()
        self.assertIn("listener", text)

    async def test_selfcheck_alerts_when_client_never_connected(self):
        await self.adapter._check_and_alert()
        text = open(self.adapter.alert_log_path, encoding="utf-8").read()
        self.assertIn("client", text)

    async def test_same_problem_does_not_spam(self):
        """同一项反复检出不重复报；不同项各报一次（互不掩盖）。

        这里把「进程启动时刻」推到宽限期之外：宽限期内的 rx_idle 提醒被有意静音
        （刚启动还没人说话属正常），那是另一条规则，不该混进来测去重。
        """
        self.adapter.alert_cooldown = 3600
        self.adapter._started_ts = time.time() - 3600      # 越过启动宽限期
        for _ in range(4):
            await self.adapter._check_and_alert()
        text = open(self.adapter.alert_log_path, encoding="utf-8").read()
        self.assertEqual(text.count("- client:"), 1, f"client 项被重复报：{text}")
        self.assertEqual(text.count("- rx_idle:"), 1, f"rx_idle 项被重复报：{text}")

    async def test_recovery_is_announced(self):
        """从坏到好要能知道 —— 只报坏不报好也会让人以为一直坏着。"""
        await self.adapter._check_and_alert()          # 先报 client 未连
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await asyncio.sleep(0.1)
            await self.adapter._check_and_alert()
        text = open(self.adapter.alert_log_path, encoding="utf-8").read()
        self.assertIn("恢复正常", text)

    async def test_alert_goes_to_magicpush_with_the_right_shape(self):
        """主人 2026-10-04 授权：这条告警通道从已退役的 :8098 改指 magicpush。

        起一个假接收端，验证真发出去的 URL / 体形状（:818/api/push/<token>，
        token 走 URL 路径、体是 {title, content, type}）。
        """
        from aiohttp import web

        got: Dict[str, Any] = {}

        async def _collect(request):
            got["path"] = request.path
            got["body"] = await request.json()
            return web.json_response({"ok": True})

        app = web.Application()
        app.router.add_post("/api/push/{token}", _collect)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]     # noqa: SLF001
        tok_file = Path(self.adapter.state_path).parent / "mp.token"
        tok_file.write_text("TOKEN123", encoding="utf-8")
        try:
            self.adapter.alert_enabled = True
            self.adapter.alert_report_url = f"http://127.0.0.1:{port}/api/push"
            self.adapter.alert_magicpush = True
            self.adapter.alert_token_file = str(tok_file)
            await self.adapter._send_alert("测试：走 magicpush", key="mp")
            await asyncio.sleep(0.4)
        finally:
            await runner.cleanup()
        self.assertEqual(got.get("path"), "/api/push/TOKEN123",
                         "token 没进 URL 路径 → magicpush 会 401")
        self.assertEqual(got["body"]["type"], "markdown")
        self.assertEqual(got["body"]["title"], "QQ 小号(OneBot)线体自检异常",
                         "默认告警级别是失败 → 标题走「异常」那一支")
        self.assertIn("走 magicpush", got["body"]["content"])
        self.assertNotIn("summary", got["body"], "老通道字段不该混进 magicpush 的体")

    def test_alert_request_shape_magicpush_vs_legacy(self):
        """形状单测（不出网）：新通道 / 老通道各一条。"""
        from onebot import adapter as A
        url, headers, body = A._build_alert_request(
            "http://172.17.0.1:818/api/push", "t k", magicpush=True,
            status="warn", title="T", text="x" * 2000, now=1.0)
        self.assertEqual(url, "http://172.17.0.1:818/api/push/t%20k", "token 要 URL 编码")
        self.assertEqual(headers, {}, "magicpush 认 URL 里的 token，不该带自定义头")
        self.assertEqual(len(body["content"]), 1200, "长文本要截断")

        url, headers, body = A._build_alert_request(
            "http://172.17.0.1:8098/report", "tok", magicpush=False,
            status="fail", title="T", text="y", now=1.0)
        self.assertEqual(url, "http://172.17.0.1:8098/report")
        self.assertEqual(headers["X-Report-Token"], "tok")
        self.assertEqual(body["status"], "fail")
        self.assertEqual(body["summary"], "y")

    async def test_send_failure_counted_for_selfcheck(self):
        self.adapter.send_retries = 1
        await self.adapter.disconnect()
        await self.adapter.send("10001", "会失败")
        st = self.adapter.health_state()
        self.assertGreaterEqual(st["consecutive_failures"], 1)
        self.assertTrue(st["last_error"])


class TestPlatformRegistration(unittest.IsolatedAsyncioTestCase):
    async def test_register_and_platform_enum(self):
        """register(ctx) 入参形状 + Platform("onebot") 能解析（插件平台动态成员）。"""
        captured: Dict[str, Any] = {}

        class _Ctx:
            def register_platform(self, **kw):
                captured.update(kw)
                return None

        _adapter_mod.register(_Ctx())
        self.assertEqual(captured["name"], "onebot")
        self.assertIs(captured["adapter_factory"], OneBotAdapter)
        self.assertTrue(captured["check_fn"]())          # aiohttp 可导入
        self.assertTrue(captured["validate_config"](PlatformConfig(extra={"ws_port": 6700})))
        self.assertIn("platform_hint", captured)

        from gateway.platform_registry import PlatformEntry, platform_registry
        platform_registry.register(PlatformEntry(
            name="onebot", label="OneBot v11", adapter_factory=OneBotAdapter,
            check_fn=captured["check_fn"], source="plugin", plugin_name="hermes_onebot",
        ))
        self.assertTrue(platform_registry.is_registered("onebot"))
        self.assertEqual(Platform("onebot").value, "onebot")
        self.assertIs(Platform("onebot"), Platform("onebot"))   # 缓存成立


class TestGroupGatedWake(AdapterHarness):
    """★ `gated` 档端到端：「免费闸决定叫不叫模型，模型决定开不开口」。

    全程走真适配器 + 真 mock 协议端（只换 handle_message 替身），验三件事：
      ① 低价值群消息：**0 唤醒、0 LLM**（省钱的证据，不是"模型说它不想回"）
      ② 有价值群消息 / @：正常唤醒一轮
      ③ 她回 `[SILENT]`：群里**一个字都发不出去**（潜水是真的没发，不是发了空的）
    """

    async def asyncSetUp(self):  # noqa: D102
        await super().asyncSetUp()
        self.adapter.group_enabled = True
        self.adapter.group_collect_enabled = True
        self.adapter.group_wake_mode = _gwk.MODE_GATED
        self.adapter.group_wake_enabled = True
        self.adapter.self_id = "90001"
        self.adapter.group_gate_threshold = _gwk.GATE_DEFAULT_THRESHOLD
        self.adapter._group_wake_limiter = _gwk.WakeLimiter(per_minute=50, per_hour=500)

    async def asyncTearDown(self):  # noqa: D102
        await super().asyncTearDown()

    @staticmethod
    def _ev(text: str = "", segs: list | None = None, *, uid: str = "10001",
            gid: str = "55555", mid: int = 1) -> Dict[str, Any]:
        if segs is None:
            segs = [{"type": "text", "data": {"text": text}}]
        return {
            "post_type": "message", "message_type": "group", "sub_type": "normal",
            "group_id": gid, "user_id": uid, "message_id": mid, "message": segs,
            "sender": {"nickname": f"甲{uid[-2:]}"}, "time": int(time.time()),
        }

    GROUP_META = {"chat_type": "group"}

    # ① 免费闸：不该叫模型的，一步都不往下走 ------------------------------
    async def test_low_value_chatter_is_gated_before_llm(self):
        for mid, text in enumerate(["哈哈", "嗯", "😀😀😀", "666"], start=1):
            await self.adapter._ingest(self._ev(text, mid=mid))
        self.assertEqual(self.dispatched, [], "低价值消息不该唤醒")
        self.assertEqual(self.adapter._group_llm_calls, 0, "0 次 LLM 调用是省钱红线")
        self.assertEqual(self.adapter._group_rx_count, 4, "但都进了采集窗口")
        self.assertEqual(len(self.adapter.group_window.tail("55555")), 4)

    # ② 该叫的就叫 --------------------------------------------------------
    async def test_real_question_passes_the_gate(self):
        await self.adapter._ingest(self._ev("这个报错怎么处理啊？"))
        self.assertEqual(len(self.dispatched), 1, "真问题该唤醒")
        self.assertEqual(self.adapter._gate_wake_count, 1, "且记的是 gate 这一档")

    async def test_at_still_passes_the_gate(self):
        await self.adapter._ingest(self._ev(
            "你在吗", segs=[{"type": "at", "data": {"qq": "90001"}},
                            {"type": "text", "data": {"text": " 你在吗"}}]))
        self.assertEqual(len(self.dispatched), 1, "@ 必须直通，不受闸影响")
        self.assertEqual(self.adapter._gate_wake_count, 0, "@ 走的是 at 档，不占 gate 计数")

    # ③ 她自己选择闭嘴 ----------------------------------------------------
    async def test_silence_marker_sends_nothing_to_group(self):
        self.adapter.group_ids.add("55555")           # 让 _is_group_chat 认得这是群
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            res = await self.adapter.send("55555", _gwk.SILENCE_MARKER, metadata=self.GROUP_META)
            await asyncio.sleep(0.25)                 # 给 mock 端收帧的机会（有就该看到）
            self.assertTrue(res.success, "闭嘴是正常结果，不是发送失败")
            self.assertIsNone(res.message_id, "没有消息 id —— 因为压根没发")
            self.assertEqual(client.sent_texts("send_group_msg"), [], "群里不许出现任何东西")
        self.assertEqual(self.adapter._silence_count, 1)

    async def test_silence_marker_does_not_work_in_private(self):
        """私聊里 `[SILENT]` **不是**开关 —— 主人找她，她不能拿这个挡。"""
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            res = await self.adapter.send("10001", _gwk.SILENCE_MARKER)
            await asyncio.sleep(0.25)
            self.assertTrue(res.success)
            self.assertIn(_gwk.SILENCE_MARKER, client.sent_texts("send_private_msg"),
                          "私聊应当照发（SOUL 里禁的是群聊场景）")
        self.assertEqual(self.adapter._silence_count, 0)

    async def test_normal_reply_still_goes_out(self):
        self.adapter.group_ids.add("55555")
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            res = await self.adapter.send("55555", "我看下日志再说", metadata=self.GROUP_META)
            await asyncio.sleep(0.25)
            self.assertTrue(res.success)
            self.assertEqual(len(client.sent_texts("send_group_msg")), 1)
        self.assertEqual(self.adapter._silence_count, 0)

    async def test_silence_does_not_update_idle_clock(self):
        """闭嘴不算"她说过话" —— 否则闲置压力会被她自己的潜水和稀泥。"""
        self.adapter.group_ids.add("55555")
        st = self.adapter._gate_state("55555")
        await self.adapter.send("55555", _gwk.SILENCE_MARKER, metadata=self.GROUP_META)
        self.assertEqual(st.last_self_at, 0.0)

    # ── 2026-10-03 修复：打分不许把「本条消息自己」当复读 ──────────────────

    def _quiet_state(self, gid: str = "55555"):
        """把干扰项清零：她刚说过话（没闲置加成）、不在对话态、无历史消息。"""
        st = self.adapter._gate_state(gid)
        st.last_self_at = time.time()
        st.conv_until = 0.0
        st._recent.clear()
        return st

    async def test_borderline_message_is_not_penalised_by_itself(self):
        """刚够过闸的消息必须能叫她。

        `帮我弄一下`（帮=疑问信号 +0.35，5 个字不够"有信息量"，闲置压力≈0）正好 0.35。
        旧顺序把这句先塞进"最近消息"，打分时自己跟自己 100% 相似 → −0.25 → 0.10 → 叫不醒
        （线上日志里每条群消息都带 `-0.25 复读/刷屏`，就是这么来的）。
        """
        self._quiet_state()
        await self.adapter._ingest(self._ev("帮我弄一下"))
        self.assertEqual(len(self.dispatched), 1,
                         "刚够过闸的消息被自己的复读惩罚打下去了（bug 复发）")

    async def test_real_repeat_is_still_penalised(self):
        """修 bug 不能把复读判定修没：同一句连发两次，第二次照样不叫。"""
        st = self._quiet_state()
        await self.adapter._ingest(self._ev("帮我弄一下", mid=1))
        self.assertEqual(len(self.dispatched), 1)
        self.adapter._group_wake_limiter = _gwk.WakeLimiter(per_minute=50, per_hour=500)
        st.conv_until = 0.0            # 灭掉第一轮唤醒带来的对话态加成
        await self.adapter._ingest(self._ev("帮我弄一下", mid=2))
        self.assertEqual(len(self.dispatched), 1, "真复读该被拦住，没拦住等于闸门被修漏了")

    # ── 增量注入（2026-10-03 主人拍板：「取消重复的部分」）──────────────────

    async def test_second_wake_only_sends_the_new_messages(self):
        """第二轮只带"上次之后的新消息"，旧的**不再重发**（之前是每轮都发最近 30 条）。"""
        self._quiet_state()
        self.adapter.group_gate_threshold = 0.0   # 本用例只测注入口径，不掺门控因素
        await self.adapter._ingest(self._ev("第一句话在这里", mid=1))
        self.assertEqual(len(self.dispatched), 1)
        self.assertIn("第一句话在这里", self.dispatched[0].text, "首轮该给一段底稿")

        await self.adapter._ingest(self._ev("第二句话在这里", mid=2))
        self.assertEqual(len(self.dispatched), 2)
        second = self.dispatched[1].text
        self.assertIn("第二句话在这里", second)
        self.assertNotIn("第一句话在这里", second, "旧消息又重发了一遍 —— delta 口径没生效")
        self.assertIn("上次你被叫醒之后", second, "回合正文没说明这是增量")

    async def test_window_mode_keeps_the_old_behaviour(self):
        """回滚路径：配置切回 `window` 就恢复"每轮发最近 N 条"。"""
        self.adapter.group_context_inject_mode = "window"
        self._quiet_state()
        self.adapter.group_gate_threshold = 0.0
        await self.adapter._ingest(self._ev("甲句在这里", mid=1))
        await self.adapter._ingest(self._ev("乙句在这里", mid=2))
        self.assertEqual(len(self.dispatched), 2)
        self.assertIn("甲句在这里", self.dispatched[1].text, "window 口径应保留旧行为")
        self.assertIn("最近的聊天记录", self.dispatched[1].text)

    async def test_delta_caps_after_a_long_silence(self):
        """久未唤醒时一次最多灌 N 条，不许把几百条一口气塞过去。"""
        self.adapter.group_context_inject_msgs = 2
        self._quiet_state()
        self.adapter.group_gate_threshold = 0.0
        await self.adapter._ingest(self._ev("底稿句", mid=1))
        for i, t in enumerate(("新1", "新2", "新3")):
            await self.adapter._ingest(self._ev(t, mid=10 + i))
        last = self.dispatched[-1].text
        self.assertIn("新3", last)
        self.assertNotIn("新1", last, "超过上限的旧消息不该再灌")
        self.assertNotIn("底稿句", last)


class TestOutboundReplySegment(AdapterHarness):
    """出站引用回复：`send(reply_to=)` 必须真的在消息段里带一个 reply。

    以前这个形参**定义了但从头到尾没人用** —— 入站能解析引用、出站不会发，方向不对称。
    """

    async def asyncSetUp(self):  # noqa: D102
        await super().asyncSetUp()
        self.adapter.group_ids.add("55555")

    @staticmethod
    def _first_msg_frame(client, action: str) -> Dict[str, Any]:
        for act in client.actions:
            if act.get("action") == action:
                return act
        raise AssertionError(f"没抓到 {action} 帧")

    async def test_reply_to_prepends_reply_segment(self):
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await self.adapter.send("55555", "收到", reply_to="777",
                                    metadata={"chat_type": "group"})
            await asyncio.sleep(0.25)
            frame = self._first_msg_frame(client, "send_group_msg")
        msg = frame["params"]["message"]
        self.assertEqual(msg[0], {"type": "reply", "data": {"id": "777"}})
        self.assertEqual(msg[1]["type"], "text")
        self.assertEqual(msg[1]["data"]["text"], "收到")

    async def test_no_reply_to_means_no_reply_segment(self):
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await self.adapter.send("55555", "你好", metadata={"chat_type": "group"})
            await asyncio.sleep(0.25)
            frame = self._first_msg_frame(client, "send_group_msg")
        msg = frame["params"]["message"]
        self.assertEqual([s["type"] for s in msg], ["text"])

    async def test_reply_segment_only_on_first_segment(self):
        """分段发送时引用只挂第一条 —— 否则后面几条会一起变成引用回复。"""
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await self.adapter.send("55555", "第一段⁂第二段", reply_to="777",
                                    metadata={"chat_type": "group"})
            await asyncio.sleep(0.6)
            frames = [a for a in client.actions if a.get("action") == "send_group_msg"]
        self.assertGreaterEqual(len(frames), 2, f"应当分成多条发，实得 {len(frames)}")
        types0 = [s["type"] for s in frames[0]["params"]["message"]]
        types1 = [s["type"] for s in frames[1]["params"]["message"]]
        self.assertIn("reply", types0, "第一条该带引用")
        self.assertNotIn("reply", types1, "第二条不该带引用")


class TestGroupAdminWiring(AdapterHarness):
    """群管理与适配器的接线端到端：notice 事件 → Intent → 真 OneBot 帧。

    这是「两个缺口」里的第一个的验收 —— 以前 `should_ignore` 第一句就把
    `post_type != "message"` 全丢掉，入群通知根本到不了任何地方。
    """

    async def asyncSetUp(self):  # noqa: D102
        await super().asyncSetUp()
        self.adapter.group_ids.add("55555")
        self.adapter.self_id = "90001"

    def _enable_admin(self, cfg: dict) -> None:
        self.adapter.group_admin_enabled = True
        self.adapter.group_admin = _gadmin.GroupAdmin(
            cfg, owner_ids=("<OWNER_QQ>",), self_id="90001")

    @staticmethod
    def _increase(uid: str = "10001", gid: str = "55555") -> Dict[str, Any]:
        return {"post_type": "notice", "notice_type": "group_increase",
                "group_id": gid, "user_id": uid, "operator_id": uid,
                "time": int(time.time()), "sender": {"nickname": "新人甲"}}

    @staticmethod
    def _frames(client, action: str) -> List[Dict[str, Any]]:
        return [a for a in client.actions if a.get("action") == action]

    async def test_group_admin_off_by_default(self):
        """默认关：notice 进来也**不动手**（群管理会真禁言真踢人，默认必须保守）。"""
        self.assertIsNone(self.adapter.group_admin)
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await self.adapter._ingest(self._increase())
            await asyncio.sleep(0.25)
            self.assertEqual(self._frames(client, "send_group_msg"), [])

    async def test_notice_is_no_longer_swallowed(self):
        """以前 notify 事件被 `should_ignore` 一句话丢掉；现在会被路由到群管理。"""
        self._enable_admin({"welcome": {"enabled": True, "text": "欢迎 {at} 进群！"}})
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await self.adapter._ingest(self._increase())
            await asyncio.sleep(0.3)
            frames = self._frames(client, "send_group_msg")
        self.assertEqual(len(frames), 1, "入群通知应当触发出一次发送")
        self.assertEqual(self.adapter._group_admin_actions, 1)

    async def test_welcome_at_renders_as_real_at_segment(self):
        """★ `{at}` 必须是**真的 at 段**，不是文本里的一串 `[CQ:at,qq=...]`。

        数组格式里文本段内的 CQ 码各协议端表现不一致 —— 赌它会解析不如直接给真段。
        """
        self._enable_admin({"welcome": {"enabled": True, "text": "欢迎 {at} 来到 {group}！"}})
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await self.adapter._ingest(self._increase(uid="10001"))
            await asyncio.sleep(0.3)
            frames = self._frames(client, "send_group_msg")
        segs = frames[0]["params"]["message"]
        types = [s["type"] for s in segs]
        self.assertIn("at", types, f"应当有真 at 段，实得 {types}")
        at = next(s for s in segs if s["type"] == "at")
        self.assertEqual(str(at["data"]["qq"]), "10001")
        joined = "".join(s["data"].get("text", "") for s in segs if s["type"] == "text")
        self.assertNotIn("[CQ:", joined, "不该把裸 CQ 码当文本发出去")
        self.assertIn("欢迎", joined)

    async def test_disabled_welcome_sends_nothing(self):
        self._enable_admin({"welcome": {"enabled": False, "text": "欢迎 {at}"}})
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await self.adapter._ingest(self._increase())
            await asyncio.sleep(0.25)
            self.assertEqual(self._frames(client, "send_group_msg"), [])

    async def test_internal_intent_is_not_sent_as_action(self):
        """`ACTION_INTERNAL` 只提示、**不发帧** —— 发了会得到「未知 action」错误。

        造一个"撤回但缓存里没有原文"的场景来触发它。
        """
        import importlib as _il
        ga = _gadmin.GroupAdmin({"anti_recall": {"enabled": True}},
                                owner_ids=("<OWNER_QQ>",), self_id="90001")
        self.adapter.group_admin = ga
        recall = {"post_type": "notice", "notice_type": "group_recall",
                  "group_id": "55555", "user_id": "10002", "operator_id": "10002",
                  "message_id": 424242, "time": int(time.time()),
                  "sender": {"nickname": "某人"}}
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await self.adapter._ingest(recall)
            await asyncio.sleep(0.25)
            sent = [a for a in client.actions
                    if a.get("action") not in ("get_login_info",)]
        self.assertEqual(sent, [], f"不该发出任何请求，实得 {[a.get('action') for a in sent]}")
        self.assertTrue(self.adapter._group_admin_notes, "应当留一条提示笔记")

    async def test_all_admin_actions_are_in_verified_allowlist(self):
        """群管理产出的 action 名必须落在**本机实测存在**的白名单里。

        白名单来源：对 NapCat 4.18.28 做过空参只读探测，确认存在这些 action。
        防的是「照文档写了个不存在的 action 名」这类事故。
        """
        verified = {
            "send_group_msg", "send_private_msg", "delete_msg",
            "set_group_ban", "set_group_kick", "set_group_card", "set_group_admin",
            "set_group_whole_ban", "set_group_name", "set_group_special_title",
            "set_essence_msg", "_send_group_notice", "set_group_add_request",
        }
        src = Path(os.path.dirname(os.path.abspath(__file__)), "..", "group_admin.py").read_text(
            encoding="utf-8")
        used = set(re.findall(r'Intent\(\s*"([a-z_]+)"', src))
        self.assertTrue(used, "一个 action 都没提取到？正则坏了")
        self.assertEqual(used - verified, set(), "用了未验证的 action 名")


class TestSilenceWarningSuppressed(AdapterHarness):
    """主人 2026-10-04：「看来还是不行啊，干脆把她提示都关了，一步到位解决」。

    Hermes 在**人回合**上发现她只回了静默标记时，会把正文换成一段 ⚠️ 提示文案
    （`gateway/run_turn.py` 的 `_UNEXPECTED_SILENCE_REPLY`）——这段原先会直接发到主人面前。
    现在按主人要求吞掉：**不发**、只记账 + 记日志（仍可查，不是黑洞）。
    """

    WARN = ("⚠️ The model returned only a silence marker for a message that "
            "needed a reply. Try again or rephrase.")

    async def test_warning_is_swallowed_in_dm(self):
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            res = await self.adapter.send("10001", self.WARN)
            self.assertTrue(res.success, res.error)
            await asyncio.sleep(0.1)
            sent = client.sent_texts("send_private_msg")
        self.assertEqual(sent, [], "静默提示被发到私聊了（主人明确要求关掉）")
        self.assertEqual(self.adapter._silence_warn_suppressed, 1, "没记账，以后查不到")

    async def test_warning_is_swallowed_in_group_too(self):
        """群里也不许出现这段 —— 否则她在群里"装死被拆穿"的话会被外人看到。"""
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await self.adapter.send("55555", self.WARN, metadata={"chat_type": "group"})
            await asyncio.sleep(0.1)
            sent = client.sent_texts("send_group_msg")
        self.assertEqual(sent, [], "群里也不许出现这段提示")

    async def test_normal_text_is_untouched(self):
        async with MockNapCat(self.url, token=\"<SECRET>\") as client:
            await self.adapter.send("10001", "我提一嘴：这条是正常回话")
            await asyncio.sleep(0.1)
            sent = client.sent_texts("send_private_msg")
        self.assertEqual(len(sent), 1, "正常消息被误吞了")
        self.assertEqual(self.adapter._silence_warn_suppressed, 0, "正常消息不该记账")


if __name__ == "__main__":
    unittest.main(verbosity=2)
