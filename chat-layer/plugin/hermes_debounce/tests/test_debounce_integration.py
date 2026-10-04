#!/usr/bin/env python3
"""hermes_debounce 容器内集成自测：直接驱动真实插件 handler（假 event，不碰平台）。

跑法（容器内，插件已部署到 /AstrBot/data/plugins/hermes_debounce/）：
    sudo -A docker cp <host>/chat-layer/plugin/hermes_debounce/tests/test_debounce_integration.py astrbot:/tmp/
    sudo -A docker exec astrbot python3 /tmp/test_debounce_integration.py

覆盖：
  1. 单条 → 一轮一条
  2. 连发 3 条（间隔 < wait）→ 合并成一条，后 2 条被 stop_event()
  3. 两条间隔 > wait → 分两轮
  4. 一直发 → 撞 max_wait 硬上限强制放行（不会无限等）
  5. scope 语义：private 时群聊完全不介入；both 时群聊按「会话+发送者」分开，
     且群里没点到猫猫的消息（无人正在攒 = 无活跃轮次）完全不碰；
     同一个人 @ 完接着补的无 @ 消息会并进这一轮
  6. 指令（/reset）不拦、不合并
  7. enabled=false 时彻底不介入
  8. 隐私：插件日志里不出现消息正文（哨兵词检验）

绝不给主人发消息：不调用 event.send / 不发任何 HTTP。
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import time
from types import SimpleNamespace

MAIN_PATH = "/AstrBot/data/plugins/hermes_debounce/main.py"

spec = importlib.util.spec_from_file_location("hermes_debounce_main_inttest", MAIN_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)  # 触发 @register（独立进程里注册，无副作用）

from astrbot.core.message.components import Plain  # noqa: E402

FAILS: list[str] = []
LOGS: list[str] = []


class _Cap(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            LOGS.append(record.getMessage())
        except Exception:  # noqa: BLE001
            pass


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("✅ " if cond else "❌ ") + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(name)


class FakeEvent:
    def __init__(self, text: str, umo="aiocqhttp:FriendMessage:10000", gid="", sid="10000",
                 addressed=True):
        self.message_str = text
        self.unified_msg_origin = umo
        self.message_obj = SimpleNamespace(message=[Plain(text)])
        self.is_at_or_wake_command = addressed   # 私聊一律 True；群聊非 @ 时为 False
        self._gid = gid
        self._sid = sid
        self.stopped = False

    def get_group_id(self):
        return self._gid

    def get_sender_id(self):
        return self._sid

    def stop_event(self):
        self.stopped = True


def _attach_all() -> None:
    """挂到 root + 当前存在的所有 logger（插件专属 logger 是懒创建的，可能后出现）。"""
    loggers = [logging.getLogger(), *logging.getLogger().manager.loggerDict.values()]
    for lg in loggers:
        if not isinstance(lg, logging.Logger):
            continue
        if not any(isinstance(h, _Cap) for h in lg.handlers):
            lg.addHandler(_Cap())
        try:
            lg.setLevel(logging.INFO)
        except Exception:  # noqa: BLE001
            pass


def new_plugin(**cfg):
    base = {"enabled": True, "wait_seconds": 0.4, "max_wait_seconds": 5, "scope": "private"}
    base.update(cfg)
    ctx = SimpleNamespace(get_config=lambda: {"wake_prefix": []})
    plugin = mod.HermesDebounce(ctx, base)
    _attach_all()
    return plugin


async def feed(plugin, events):
    async def one(delay, ev):
        await asyncio.sleep(delay)
        await plugin.on_message(ev)
        return ev

    return list(await asyncio.gather(*(one(d, e) for d, e in events)))


def run(title, coro):
    print(f"\n--- {title} ---")
    return asyncio.run(coro)


def main() -> int:
    # astrbot 4.28.1 会把插件 logger 调用路由到「插件专属 logger」，故每次构造后再全量挂采集
    _attach_all()

    # 1. 单条
    p = new_plugin()
    ev1 = FakeEvent("单条消息")
    t0 = time.monotonic()
    run("1. 单条消息", feed(p, [(0.0, ev1)]))
    dt = time.monotonic() - t0
    check("只放行 1 轮", p._rounds == 1, f"rounds={p._rounds}")
    check("没有被压制", p._suppressed == 0)
    check("事件未被停（照常走后续管道 → 模型回一次）", not ev1.stopped)
    check("等了满静默窗口再放行", dt >= 0.4 - 0.02, f"{dt:.3f}s")
    check("文本原样", ev1.message_str == "单条消息", repr(ev1.message_str))

    # 2. 连发三条
    p = new_plugin()
    evs = [FakeEvent("句子A"), FakeEvent("句子B"), FakeEvent("句子C")]
    run("2. 连发 3 条（间隔<wait）", feed(p, [(0.0, evs[0]), (0.12, evs[1]), (0.24, evs[2])]))
    check("只放行 1 轮（模型只回一次）", p._rounds == 1, f"rounds={p._rounds}")
    check(
        "leader 的 message_str = 三条合并",
        evs[0].message_str == "句子A\n句子B\n句子C",
        repr(evs[0].message_str),
    )
    check("第 2 条被压制（stop_event）", evs[1].stopped)
    check("第 3 条被压制（stop_event）", evs[2].stopped)
    check("压制计数=2", p._suppressed == 2, f"suppressed={p._suppressed}")
    check("缓冲清空", p.store.active_count() == 0)

    # 3. 间隔超窗口 → 两轮
    p = new_plugin()
    e1, e2 = FakeEvent("第一波"), FakeEvent("第二波")
    run("3. 两条间隔 > wait", feed(p, [(0.0, e1), (1.0, e2)]))
    check("分成 2 轮", p._rounds == 2, f"rounds={p._rounds}")
    check(
        "两轮各自独立文本",
        e1.message_str == "第一波" and e2.message_str == "第二波",
        f"{e1.message_str!r} / {e2.message_str!r}",
    )
    check("没人被压制", p._suppressed == 0)

    # 4. 硬上限
    p = new_plugin(wait_seconds=5, max_wait_seconds=0.5)
    evs = [FakeEvent(f"轰{n}") for n in range(4)]
    t0 = time.monotonic()
    run(
        "4. 一直发 → max_wait 硬上限强制放行",
        feed(p, [(0.0, evs[0]), (0.12, evs[1]), (0.25, evs[2]), (0.4, evs[3])]),
    )
    dt = time.monotonic() - t0
    check("只回一轮", p._rounds == 1, f"rounds={p._rounds}")
    check("硬上限到点就放（~0.5s 而不是 5s）", 0.45 <= dt <= 1.2, f"{dt:.3f}s")
    check("期间收到的都在这一轮", evs[0].message_str == "轰0\n轰1\n轰2\n轰3", repr(evs[0].message_str))

    # 5a. scope=private（默认）群聊不介入
    p = new_plugin(scope="private")
    ge = FakeEvent("群里说的话", umo="aiocqhttp:GroupMessage:9", gid="9", sid="111")
    run("5a. scope=private：群聊不介入", feed(p, [(0.0, ge)]))
    check("群聊消息没被缓冲/没被停", (p._rounds == 0) and (not ge.stopped))
    check("文本没被改", ge.message_str == "群里说的话")

    # 5b. scope=both 群聊按 会话+发送者 分开
    p = new_plugin(scope="both")
    a1 = FakeEvent("甲1", umo="aiocqhttp:GroupMessage:9", gid="9", sid="111")
    a2 = FakeEvent("甲2", umo="aiocqhttp:GroupMessage:9", gid="9", sid="111")
    b1 = FakeEvent("乙1", umo="aiocqhttp:GroupMessage:9", gid="9", sid="222")
    run("5b. scope=both：群聊不同人分轮", feed(p, [(0.0, a1), (0.05, b1), (0.12, a2)]))
    check("两轮（甲一轮、乙一轮）", p._rounds == 2, f"rounds={p._rounds}")
    check("甲的两条只和甲合并", a1.message_str == "甲1\n甲2", repr(a1.message_str))
    check("乙单独一轮", b1.message_str == "乙1", repr(b1.message_str))
    check("不混人：甲轮里没有乙的话", "乙" not in a1.message_str)
    check("被压制的只有 1 条", p._suppressed == 1, f"suppressed={p._suppressed}")

    # 5c. scope=both：群里没点到猫猫的闲聊完全不碰
    p = new_plugin(scope="both")
    chat = FakeEvent("群友闲聊", umo="aiocqhttp:GroupMessage:9", gid="9", sid="333",
                     addressed=False)
    run("5c. scope=both：群聊非@闲聊不介入", feed(p, [(0.0, chat)]))
    check("闲聊没被缓冲/没被停", (p._rounds == 0) and (not chat.stopped))
    check("闲聊文本没被改", chat.message_str == "群友闲聊")

    # 5d. scope=both：@ 完接着补的无 @ 消息并进同一轮
    p = new_plugin(scope="both")
    m1 = FakeEvent("@猫猫 帮我看个事", umo="aiocqhttp:GroupMessage:9", gid="9", sid="111",
                   addressed=True)
    m2 = FakeEvent("补充一句", umo="aiocqhttp:GroupMessage:9", gid="9", sid="111",
                   addressed=False)
    run("5d. scope=both：@ 之后的无 @ 补充并入本轮", feed(p, [(0.0, m1), (0.1, m2)]))
    check("只放行 1 轮", p._rounds == 1, f"rounds={p._rounds}")
    check(
        "补充的话也并进来了",
        m1.message_str == "@猫猫 帮我看个事\n补充一句",
        repr(m1.message_str),
    )
    check("补充消息被压制", m2.stopped)

    # 6. 指令不拦
    p = new_plugin()
    ce = FakeEvent("/reset")
    run("6. 指令 /reset 不拦不合并", feed(p, [(0.0, ce)]))
    check("指令没被缓冲", p._rounds == 0 and not ce.stopped and ce.message_str == "/reset")

    # 7. 开关
    p = new_plugin(enabled=False)
    ev = FakeEvent("关掉之后的话")
    run("7. enabled=false 彻底不介入", feed(p, [(0.0, ev)]))
    check("不缓冲、不停、不改文本", (p._rounds == 0) and (not ev.stopped) and ev.message_str == "关掉之后的话")

    # 8. 隐私：日志里不许出现正文
    sentinel = "SECRET哨兵句子"
    p = new_plugin()
    s1, s2 = FakeEvent(sentinel), FakeEvent(sentinel + "2")
    run("8. 隐私：日志不打印正文", feed(p, [(0.0, s1), (0.1, s2)]))
    leaked = [m for m in LOGS if sentinel in m]
    check("日志里没有消息正文", not leaked, f"leaked={leaked[:2]}")
    check(
        "有插件自己的就绪/放行日志",
        any(("就绪" in m or "放行一轮" in m) for m in LOGS),
        f"命中={[m for m in LOGS if '就绪' in m or '放行一轮' in m][:2]}",
    )
    print("   本进程捕获到的 hermes_debounce 相关日志：")
    for m in LOGS:
        if "hermes_debounce" in m:
            print("     " + m)

    print("\n" + "=" * 60)
    if FAILS:
        print(f"❌ 失败 {len(FAILS)} 项：{FAILS}")
        return 1
    print("✅ 容器内集成自测全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
