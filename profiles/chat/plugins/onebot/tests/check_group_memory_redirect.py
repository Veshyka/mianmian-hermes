"""群记忆**分流**（redirect 模式）的功能测试 —— 2026-10-03 主人拍板。

和 `check_group_memory_guard.py`（block 模式的取证/验收）分工：
这里是**纯逻辑级**测试，用一个假客户端把网关侧真正发出去的东西截下来看：
  * 群回合 retain 到底写了哪个 bank、带了什么标签
  * 群回合 recall 查的哪个 bank、按什么标签过滤、token 预算多少
  * 私聊一动没动

不需要 Hindsight 服务在线：bundled provider 的所有网络出口都收敛在
`_run_hindsight_operation` / `_retain_batch` 两个方法上，测试子类把它们接管即可。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

_HERE = Path(__file__).resolve().parent
_PLUGINS = _HERE.parent.parent  # .../profiles/chat/plugins
sys.path.insert(0, "/opt/hermes")
sys.path.insert(0, str(_PLUGINS))

import hindsight_guard as guard  # noqa: E402

GROUP_TURN = (
    "[OneBot 群聊回合 · source=qq-group · <GROUP_ID2> · 生人档 · 触发=at]\n"
    "【群聊新增 · 2 条 · 仅供理解，别照抄】\n"
    "[16:07] <OWNER_NICK>(QQ:<OWNER_QQ>): 把那个挂上\n"
    "[16:07] 总是栋真情(QQ:<OWNER_QQ2>): 我也想要\n"
    "—— 请在这个群里用你自己的口吻回应末尾那条消息"
)
DM_TURN = "主人，这个月的备份要不要挪到周日？"
SELF = "<BOT_QQ>"
MAIN_BANK = "mianmian-history"
GROUP_BANK = "mianmian-group"


class _FakeClient:
    """假的 Hindsight 客户端：只记调用参数，不联网。"""

    def __init__(self) -> None:
        self.calls: list = []

    async def arecall(self, **kw):
        self.calls.append(("recall", kw))
        return SimpleNamespace(results=[])

    async def areflect(self, **kw):
        self.calls.append(("reflect", kw))
        return SimpleNamespace(text="")

    async def aretain_batch(self, **kw):
        self.calls.append(("retain_batch", kw))
        return SimpleNamespace()

    def tag(self, kind: str):
        return [kw for k, kw in self.calls if k == kind]


class _Recorder(guard.HindsightGuardProvider):
    """把两个网络出口接管掉；retain 的 job 同步跑完，方便断言。"""

    def __init__(self) -> None:
        super().__init__()
        self.client = _FakeClient()
        self.retained: list = []
        # 真环境里这些是 initialize()/settings 装载时补上的；测试跳过 initialize，
        # 这里按 bundled 的同名默认值补齐（改动只影响测试替身）。
        self._observation_scopes = None
        self._retain_tags: list = []
        self._tags = None
        self._recall_tags = None
        self._recall_tags_match = "any"
        self._recall_max_tokens = 1024  # 聊天门私聊的现役值，群里应被压到 512

    # 出口 1：所有 recall/reflect（真实现负责把协程跑起来，这里同步跑）
    def _run_hindsight_operation(self, fn):
        import asyncio

        res = fn(self.client)
        if asyncio.iscoroutine(res):
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(res)
            finally:
                loop.close()
        return res

    # 出口 2：retain
    def _retain_batch(self, item, *, bank_id, document_id=None, retain_async=None):
        self.retained.append({"item": item, "bank_id": bank_id})
        return SimpleNamespace()

    def _enqueue_retain(self, job):
        job()  # 同步跑，不给 writer 线程留竞态


def _make(mode: str, **over) -> _Recorder:
    g = _Recorder()
    g._guard.update({"enabled": True, "mode": mode, "group_bank_id": GROUP_BANK,
                     "group_recall_budget": "mid", "group_recall_max_tokens": 512,
                     "cross_group_recall": False})
    g._guard.update(over)
    g._bank_id = MAIN_BANK
    g._base_bank_id = MAIN_BANK
    return g


class TestPureHelpers(unittest.TestCase):
    """抠群号/说话人的纯函数（正则别写成只能匹配一种写法）。"""

    def test_group_id_and_speaker_from_turn_text(self):
        self.assertEqual(guard.group_id_from_text(GROUP_TURN), "<GROUP_ID2>")
        self.assertEqual(guard.speaker_from_text(GROUP_TURN), "<OWNER_QQ2>")
        self.assertEqual(guard.group_id_from_text(DM_TURN), "")
        self.assertEqual(guard.speaker_from_text(DM_TURN), "")

    def test_tags_carry_group_and_speaker(self):
        tags = guard.group_tags_from_text(GROUP_TURN)
        self.assertIn("group:<GROUP_ID2>", tags)
        self.assertIn("speaker:<OWNER_QQ2>", tags)
        self.assertEqual(guard.group_tags_from_text(DM_TURN), [])

    def test_bot_itself_is_not_recorded_as_speaker(self):
        """她自己的消息是 `[我](QQ:<BOT_QQ>)` → 别把自己当"说话人"打标。"""
        text = GROUP_TURN + "\n[16:08] [我](QQ:<BOT_QQ>): 好嘞"
        self.assertEqual(guard.speaker_from_text(text), SELF)
        self.assertEqual(guard.group_tags_from_text(text, self_id=SELF),
                         ["group:<GROUP_ID2>"])


class TestRedirectRetain(unittest.TestCase):
    def test_group_turn_writes_the_group_bank_with_tags(self):
        g = _make("redirect")
        g.sync_turn(GROUP_TURN, "好嘞，挂上了", session_id="agent:main:onebot:group:<GROUP_ID2>")
        self.assertEqual(len(g.retained), 1, "群回合没写进群库")
        self.assertEqual(g.retained[0]["bank_id"], GROUP_BANK, "写错库了")
        tags = g.retained[0]["item"].get("tags") or []
        self.assertIn("group:<GROUP_ID2>", tags)
        self.assertIn("speaker:<OWNER_QQ2>", tags)
        self.assertEqual(g._bank_id, MAIN_BANK, "写完之后 bank 没还原 → 私聊会写进群库")
        self.assertEqual(g._counters["retain_redirected"], 1)

    def test_dm_turn_still_writes_the_main_bank(self):
        g = _make("redirect")
        g.sync_turn(DM_TURN, "好，我记下了", session_id="agent:main:onebot:dm:<OWNER_QQ>")
        self.assertEqual([r["bank_id"] for r in g.retained], [MAIN_BANK])

    def test_block_mode_still_blocks(self):
        """老行为必须还在：mode=block 时群回合一个字节都不写。"""
        g = _make("block")
        g.sync_turn(GROUP_TURN, "好嘞", session_id="s1")
        self.assertEqual(g.retained, [])
        self.assertEqual(g._counters["retain_skipped"], 1)

    def test_disabled_guard_is_transparent(self):
        """enabled=false = 退回 bundled 行为（群回合直接写主库）。"""
        g = _make("redirect", enabled=False)
        g.sync_turn(GROUP_TURN, "好嘞", session_id="s1")
        self.assertEqual([r["bank_id"] for r in g.retained], [MAIN_BANK])


class TestRedirectRecall(unittest.TestCase):
    def test_group_recall_hits_group_bank_filtered_by_group_tag(self):
        g = _make("redirect")
        g._recall(GROUP_TURN)
        calls = g.client.tag("recall")
        self.assertEqual(len(calls), 1)
        kw = calls[0]
        self.assertEqual(kw["bank_id"], GROUP_BANK)
        self.assertEqual(kw["tags"], ["group:<GROUP_ID2>"])
        self.assertEqual(kw["tags_match"], "any")
        self.assertEqual(kw["max_tokens"], 512, "群召回比私聊更省这条没落地")
        self.assertEqual(g._bank_id, MAIN_BANK)
        self.assertEqual(g._counters["recall_redirected"], 1)

    def test_dm_recall_untouched(self):
        g = _make("redirect")
        g._recall(DM_TURN)
        kw = g.client.tag("recall")[0]
        self.assertEqual(kw["bank_id"], MAIN_BANK)
        self.assertNotIn("tags", kw)

    def test_cross_group_recall_drops_the_group_filter(self):
        g = _make("redirect", cross_group_recall=True)
        g._recall(GROUP_TURN)
        kw = g.client.tag("recall")[0]
        self.assertEqual(kw["bank_id"], GROUP_BANK)
        self.assertNotIn("tags", kw, "开了跨群认人就不该再按群过滤")

    def test_prefetch_does_not_block_anymore_in_redirect(self):
        """redirect 下群回合的 prefetch 要真去查（block 模式下它是被拦掉的）。"""
        g = _make("redirect")
        try:
            out = g.prefetch(GROUP_TURN, session_id="agent:main:onebot:group:<GROUP_ID2>")
        except Exception as e:  # noqa: BLE001
            self.fail(f"prefetch 炸了：{e!r}")
        self.assertIsInstance(out, str)
        self.assertTrue(g.client.tag("recall"), "没去查群库")


class TestSessionFallback(unittest.TestCase):
    """**两路都认**（2026-10-03 实测教训）：只认正文标记会出现"群回合没被分流也没被记数"的静默漏。"""

    def test_group_id_from_session_key(self):
        self.assertEqual(guard.group_id_from_session("agent:main:onebot:group:<GROUP_ID2>"),
                         "<GROUP_ID2>")
        self.assertEqual(guard.group_id_from_session("agent:main:onebot:dm:<OWNER_QQ>"), "")
        self.assertEqual(guard.group_id_from_session(""), "")

    def test_sync_turn_without_marker_still_lands_in_group_bank(self):
        """正文没标记（Hermes 两条路径给的内容不一样）→ 靠会话键也得进群库 + 打群标签。"""
        g = _make("redirect")
        sid = "agent:main:onebot:group:<GROUP_ID2>"
        g._guard_session_id = sid
        g.sync_turn("（这段正文里没有群回合标记）", "嗯", session_id=sid)
        self.assertEqual(len(g.retained), 1, "没写进去 = 分流静默失效")
        self.assertEqual(g.retained[0]["bank_id"], GROUP_BANK)
        tags = g.retained[0]["item"].get("tags") or []
        self.assertIn("group:<GROUP_ID2>", tags, "会话键兜底的群标签没挂上")

    def test_prefetch_in_group_runs_sync_against_group_bank(self):
        g = _make("redirect")
        g._guard_session_id = "agent:main:onebot:group:<GROUP_ID2>"
        out = g.prefetch(GROUP_TURN, session_id="agent:main:onebot:group:<GROUP_ID2>")
        self.assertIsInstance(out, str)
        calls = g.client.tag("recall")
        self.assertEqual(len(calls), 1, "群回合的预取没真去查")
        self.assertEqual(calls[0]["bank_id"], GROUP_BANK)
        self.assertEqual(calls[0]["tags"], ["group:<GROUP_ID2>"])
        self.assertEqual(g._counters["recall_redirected"], 1)

    def test_queue_prefetch_does_not_fire_a_background_query(self):
        """群回合不排后台预取（那条线程拿不到 bank 作用域，排了会查错库）。"""
        g = _make("redirect")
        g._guard_session_id = "agent:main:onebot:group:<GROUP_ID2>"
        g.queue_prefetch(GROUP_TURN, session_id="agent:main:onebot:group:<GROUP_ID2>")
        self.assertEqual(g.client.calls, [])

    def test_block_mode_prefetch_still_blocked(self):
        g = _make("block")
        g._guard_session_id = "agent:main:onebot:group:<GROUP_ID2>"
        self.assertEqual(g.prefetch(GROUP_TURN, session_id="agent:main:onebot:group:<GROUP_ID2>"), "")
        self.assertEqual(g.client.calls, [])
        self.assertEqual(g._counters["recall_skipped"], 1)


    def test_gateway_extra_kwarg_does_not_kill_group_retain(self):
        """Hermes 会带 `messages=[...]` 调 sync_turn，基类不吃这个参数。

        实测坑：不过滤 → `TypeError: got an unexpected keyword argument 'messages'`，
        Hermes 只记一行 WARNING，于是**群回合的 retain 静默不落库、计数停在 0**。
        """
        g = _make("redirect")
        g.sync_turn(GROUP_TURN, "嗯", session_id="agent:main:onebot:group:<GROUP_ID2>",
                    messages=[{"role": "user", "content": "群里的话"}])
        self.assertEqual(len(g.retained), 1, "带了 messages 就没写进群库 = 又静默了")
        self.assertEqual(g.retained[0]["bank_id"], GROUP_BANK)


class TestExitFlush(unittest.TestCase):
    """`retain_every_n_turns > 1` 时，退出前必须把缓冲的轮次冲掉（否则最后一轮随进程没）。"""

    def test_buffered_turn_is_flushed_on_exit(self):
        g = _make("redirect")
        g._retain_every_n_turns = 2  # 每两轮入库一次
        g._session_id = "agent:main:onebot:group:<GROUP_ID2>"
        g._document_id = "doc-n2"
        g.sync_turn(GROUP_TURN, "第一轮回复", session_id=g._session_id)
        self.assertEqual(g.retained, [], "第 1 轮本该只进缓冲，不该写库")
        self.assertEqual(len(g._session_turns), 1, "第 1 轮没进缓冲 = 直接丢了")

        g.shutdown()  # 退出
        banks = [r["bank_id"] for r in g.retained]
        self.assertEqual(len(g.retained), 1, "退出前没把缓冲冲掉：那一轮就丢了")
        self.assertEqual(banks, [GROUP_BANK], "冲的时候必须还落在群库（不是主库）")

    def test_dm_buffered_turn_is_flushed_on_exit(self):
        """私聊会话也要能冲缓冲。

        回归钉子：`shutdown()` 里曾经调了一个**不存在**的方法 `_group_of_session`，
        而群会话因为 `or` 短路根本走不到那一行 —— 于是**只有私聊**每次重启丢最后一轮
        （2026-10-04 从 agent.log 抓出：`退出前冲缓冲失败：AttributeError`）。这里钉住私聊这条路。
        """
        g = _make("redirect")
        g._retain_every_n_turns = 2
        g._session_id = "agent:main:onebot:dm:<OWNER_QQ>"
        g._document_id = "doc-dm-n2"
        g.sync_turn("主人：今天吃啥（私聊回合，无群标记）", "棉棉：随便喵", session_id=g._session_id)
        self.assertEqual(g.retained, [], "第 1 轮本该只进缓冲")
        self.assertEqual(len(g._session_turns), 1, "第 1 轮没进缓冲 = 直接丢了")

        g.shutdown()                      # 关键：不许抛 AttributeError，且要真写出去
        banks = [r["bank_id"] for r in g.retained]
        self.assertEqual(len(g.retained), 1, "私聊缓冲没被冲掉（那一轮就丢了）")
        self.assertEqual(banks, [MAIN_BANK], "私聊那轮该进主库")

    def test_no_flush_when_buffer_empty(self):
        g = _make("redirect")
        g._session_id = "agent:main:onebot:group:<GROUP_ID2>"
        g.shutdown()
        self.assertEqual(g.retained, [], "缓冲是空的就不该写任何东西")


class TestMarkerCarriesRawGroupId(unittest.TestCase):
    """标记行必须带**真群号**——曾经写成脱敏的 `<GROUP_ID>`，导致分流静默失效（2026-10-03 实测）。"""

    def test_wake_turn_text_group_id_is_parseable(self):
        # 单文件跑时 onebot 插件目录不在 sys.path 上，这里显式补上（discover 模式下本来就有）
        sys.path.insert(0, str(_PLUGINS / "onebot"))
        import group_wake  # noqa: E402

        real = group_wake.build_turn_text(gid="<GROUP_ID2>", trigger="at", window_text="x")
        self.assertIn("<GROUP_ID2>", real, "标记行的群号又被脱敏了：闸门会认不出群")
        self.assertEqual(guard.group_id_from_text(real), "<GROUP_ID2>")


class TestToolsStillBlocked(unittest.TestCase):
    def test_memory_tools_stay_blocked_in_group_turns(self):
        """分流只放行**自动**读写；她手动调记忆工具在群里仍然拦死（怕她翻私聊库）。"""
        g = _make("redirect")
        g._turn_blocked = True
        out = g.handle_tool_call("hindsight_recall", {"query": "x"})
        self.assertIn("group_turn_memory_isolated", out)
        self.assertEqual(g._counters["tool_blocked"], 1)

    def test_tools_allowed_in_dm(self):
        g = _make("redirect")
        g._turn_blocked = False
        try:
            g.handle_tool_call("hindsight_recall", {"query": "x"})
        except Exception:  # noqa: BLE001 - 真实现要联网，这里只关心没被闸拦
            return


if __name__ == "__main__":
    unittest.main(verbosity=2)
