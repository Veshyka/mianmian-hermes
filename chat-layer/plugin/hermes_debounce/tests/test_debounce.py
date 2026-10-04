#!/usr/bin/env python3
"""hermes_debounce 的离线自测（不依赖 astrbot / 不连网 / 不给主人发消息）。

跑法：
    python3 /opt/data/chat-layer/plugin/hermes_debounce/tests/test_debounce.py

两部分：
  A. 纯数学/状态机：deadline 计算、key 分组、合并文本、指令过滤（假时钟，确定性）
  B. 真异步行为：用真的 asyncio 任务并发跑「leader 等窗口 + follower 并入」，
     把任务要的四种情形（单条 / 连发3条 / 间隔超窗口 / 撞硬上限）实跑一遍
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CORE_PATH = os.path.join(os.path.dirname(HERE), "debounce.py")

_spec = importlib.util.spec_from_file_location("hermes_debounce_core_test", CORE_PATH)
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "✅" if cond else "❌"
    print(f"{mark} {name}" + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------- A. 纯逻辑
def test_deadline_math() -> None:
    print("\n--- A1. 窗口/deadline 计算 ---")
    r = core.Round(wait_seconds=7, max_wait_seconds=45, now=100.0)
    r.add("m1", now=100.0)
    check("首条后要等满静默窗口", abs(r.next_wait(100.0) - 7.0) < 1e-6, f"{r.next_wait(100.0)}")
    check("静默窗口内还没到放行点", not r.hard_cap_hit(100.0))

    # 第 5 秒来了新消息 → 倒计时从第 5 秒重算，仍要等到第 12 秒
    r.add("m2", now=105.0)
    check("新消息重置倒计时", abs(r.next_wait(105.0) - 7.0) < 1e-6, f"{r.next_wait(105.0)}")
    check("重置后 6.9s 时仍不放行", r.next_wait(111.9) > 0, f"{r.next_wait(111.9):.2f}")
    check("重置后 12s 时可以放行", r.next_wait(112.0) <= 1e-9, f"{r.next_wait(112.0)}")

    # 硬上限：从第一条算 45s，哪怕一直有新消息
    r3 = core.Round(wait_seconds=7, max_wait_seconds=45, now=0.0)
    r3.add("a", now=0.0)
    r3.add("b", now=40.0)
    check("硬上限优先于静默窗口", abs(r3.next_wait(40.0) - 5.0) < 1e-6, f"{r3.next_wait(40.0)}")
    check("到硬上限时 next_wait<=0", r3.next_wait(45.0) <= 0 and r3.hard_cap_hit(45.0))
    r4 = core.Round(wait_seconds=10, max_wait_seconds=5, now=0.0)
    check("max_wait 比 wait 小时，硬上限说了算", abs(r4.next_wait(0.0) - 5.0) < 1e-9, f"{r4.next_wait(0.0)}")
    r5 = core.Round(wait_seconds=0, max_wait_seconds=45, now=0.0)
    check("wait=0 时立刻放行（等于不合并）", r5.next_wait(0.0) <= 0)


def test_keys_and_text() -> None:
    print("\n--- A2. 会话键 / 合并 / 指令过滤 ---")
    check(
        "私聊键 = umo",
        core.build_key("aiocqhttp:FriendMessage:1", "", "1", "private") == "aiocqhttp:FriendMessage:1",
    )
    check(
        "默认 scope=private 时群聊不生效",
        core.build_key("aiocqhttp:GroupMessage:9", "9", "111", "private") is None,
    )
    k1 = core.build_key("aiocqhttp:GroupMessage:9", "9", "111", "both")
    k2 = core.build_key("aiocqhttp:GroupMessage:9", "9", "222", "both")
    check("scope=both 群聊按 会话+发送者 分开", bool(k1) and bool(k2) and k1 != k2, f"{k1} vs {k2}")
    check("同人群里同一人归同一键", k1 == core.build_key("aiocqhttp:GroupMessage:9", "9", "111", "both"))

    check("空 umo 不生效", core.build_key("", "", "1", "private") is None)
    check("合并多条", core.merge_texts(["a", "", " b ", "c"]) == "a\nb\nc")
    check("单条原样", core.merge_texts(["只有一条"]) == "只有一条")
    check("空消息算跳过", core.is_command("") and core.is_command("   "))
    check("斜杠指令不拦", core.is_command("/reset") and core.is_command("/help x"))
    check("唤醒前缀不拦", core.is_command("!ping", ["!"]))
    check("普通聊天要拦（=参与合并）", not core.is_command("在吗"))


def test_store_windows() -> None:
    print("\n--- A3. Store：什么时候开新一轮 ---")
    st = core.Store(7, 45)
    r1, lead1 = st.decide("k", "第一条", now=0.0)
    r2, lead2 = st.decide("k", "第二条", now=1.0)
    check("第一条当 leader", lead1)
    check("第二条并入同一轮", (not lead2) and r2 is r1 and r1.count == 2)
    st.close("k", r1)
    r3, lead3 = st.decide("k", "第三条", now=8.0)
    check("放行后再来消息 = 新一轮 leader", lead3 and r3 is not r1)
    check("关掉的轮次不再挂账", st.pending("k") is r3 and st.active_count() == 1)
    r4, _ = st.decide("k2", "别人的话", now=8.0)
    check("不同键互不干扰", r4 is not r3 and st.active_count() == 2)
    check("放行后缓冲为空", (st.close("k", r3), st.close("k2", r4), st.active_count() == 0)[-1])


# ---------------------------------------------------------------- B. 真异步
class Runner:
    """把插件的钩子语义搬过来：真的并发任务 + 真的 asyncio 睡眠。"""

    def __init__(self, wait: float, max_wait: float) -> None:
        self.store = core.Store(wait, max_wait)
        self.rounds: list[dict] = []
        self.suppressed = 0
        self._t0 = time.monotonic()

    async def _one(self, key: str, text: str, delay: float) -> None:
        await asyncio.sleep(delay)
        rnd, is_leader = self.store.decide(key, text)
        if not is_leader:
            self.suppressed += 1
            return                       # 真插件这里还会 event.stop_event()
        waited = await core.wait_window(rnd)
        self.store.close(key, rnd)
        self.rounds.append(
            {
                "texts": list(rnd.texts),
                "merged": rnd.merged(),
                "waited": waited,
                "released_at": time.monotonic() - self._t0,
                "hard_cap": rnd.hard_cap_hit(),
            }
        )

    async def feed(self, key: str, schedule: list[tuple[float, str]]) -> None:
        await asyncio.gather(*(self._one(key, t, d) for d, t in schedule))


def test_async_single() -> None:
    print("\n--- B1. 只发一条 → 一轮、一条 ---")
    r = Runner(wait=0.3, max_wait=5)
    t0 = time.monotonic()
    asyncio.run(r.feed("k", [(0.0, "单条消息")]))
    dt = time.monotonic() - t0
    check("只有 1 轮", len(r.rounds) == 1, f"rounds={len(r.rounds)}")
    check("轮内 1 条、内容未变", r.rounds[0]["texts"] == ["单条消息"])
    check("确实等了静默窗口(>=wait)", r.rounds[0]["waited"] >= 0.3 - 0.02, f"{r.rounds[0]['waited']:.3f}s")
    check("没有被压制的消息", r.suppressed == 0)
    print(f"   实测：整体耗时 {dt:.3f}s（wait=0.3s）")


def test_async_burst_merge() -> None:
    print("\n--- B2. 连发 3 条、间隔 < wait → 合并成一轮 ---")
    r = Runner(wait=0.4, max_wait=5)
    asyncio.run(r.feed("k", [(0.0, "在吗"), (0.1, "今天有空吗"), (0.2, "帮我看看机器")]))
    check("只有 1 轮", len(r.rounds) == 1, f"rounds={len(r.rounds)}")
    if r.rounds:
        rd = r.rounds[0]
        check("一轮里有 3 条", rd["texts"] == ["在吗", "今天有空吗", "帮我看看机器"], str(rd["texts"]))
        check("合并文本是按顺序拼接", rd["merged"] == "在吗\n今天有空吗\n帮我看看机器", repr(rd["merged"]))
        check("最后一条之后再等满 wait 才放行", rd["waited"] >= 0.6 - 0.03, f"{rd['waited']:.3f}s")
        check("不是撞硬上限放行的", not rd["hard_cap"])
    check("2 条被压制（不再各自回一次）", r.suppressed == 2, f"suppressed={r.suppressed}")


def test_async_two_rounds() -> None:
    print("\n--- B3. 两条间隔 > wait → 分两轮 ---")
    r = Runner(wait=0.3, max_wait=5)
    asyncio.run(r.feed("k", [(0.0, "第一波"), (0.8, "第二波")]))
    check("分成 2 轮", len(r.rounds) == 2, f"rounds={len(r.rounds)}")
    if len(r.rounds) == 2:
        check(
            "每轮各 1 条",
            [rd["texts"] for rd in r.rounds] == [["第一波"], ["第二波"]],
            str([rd["texts"] for rd in r.rounds]),
        )
    check("没有消息被压制", r.suppressed == 0, f"suppressed={r.suppressed}")


def test_async_hard_cap() -> None:
    print("\n--- B4. 一直发、间隔 < wait → 撞 max_wait 硬上限强制放行 ---")
    r = Runner(wait=5, max_wait=0.5)      # 静默窗口远大于硬上限
    t0 = time.monotonic()
    asyncio.run(r.feed("k", [(0.0, "a"), (0.15, "b"), (0.30, "c"), (0.45, "d")]))
    dt = time.monotonic() - t0
    check("只有 1 轮（没无限等）", len(r.rounds) == 1, f"rounds={len(r.rounds)}")
    if r.rounds:
        rd = r.rounds[0]
        check(
            "硬上限到点就放行 (~0.5s 而不是 5s)",
            0.45 <= rd["released_at"] <= 1.2,
            f"released_at={rd['released_at']:.3f}s",
        )
        check("标记为撞硬上限", rd["hard_cap"])
        check("已收到的消息都在这一轮里", rd["texts"] == ["a", "b", "c", "d"], str(rd["texts"]))
    print(f"   实测：整体耗时 {dt:.3f}s（wait=5s, max_wait=0.5s）")


def test_async_group_isolation() -> None:
    print("\n--- B5. scope=both 群聊：不同发送者不混轮 ---")
    r = Runner(wait=0.3, max_wait=5)
    ka = core.build_key("umo:g9", "9", "111", "both")
    kb = core.build_key("umo:g9", "9", "222", "both")

    async def go():
        await asyncio.gather(
            r._one(ka, "甲说的话1", 0.0),
            r._one(ka, "甲说的话2", 0.1),
            r._one(kb, "乙说的话", 0.05),
        )

    asyncio.run(go())
    check("两轮（甲、乙各一轮）", len(r.rounds) == 2, f"rounds={len(r.rounds)}")
    texts = sorted(",".join(rd["texts"]) for rd in r.rounds)
    check("甲的两条只和甲合并", texts == ["乙说的话", "甲说的话1,甲说的话2"], str(texts))
    check("被压制的只有 1 条", r.suppressed == 1, f"suppressed={r.suppressed}")


def main() -> int:
    print(f"debounce.py = {CORE_PATH}")
    print(f"python = {sys.version.split()[0]}")

    test_deadline_math()
    test_keys_and_text()
    test_store_windows()
    test_async_single()
    test_async_burst_merge()
    test_async_two_rounds()
    test_async_hard_cap()
    test_async_group_isolation()

    print("\n" + "=" * 56)
    if FAILS:
        print(f"❌ 失败 {len(FAILS)} 项：{FAILS}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
