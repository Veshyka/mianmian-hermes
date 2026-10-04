"""连发消息合并的核心逻辑 —— 纯 Python，不 import astrbot，可离线/宿主机单测。

名词：
  * round（一轮）：一段连发的消息归到一个 Round 里，最后合并成一条文本交给模型
  * leader：一轮里「第一条」消息所在的那次事件处理，由它负责阻塞等待窗口、最后放行
  * follower：窗口期内后续到的消息，只往 Round 里塞文本，然后叫 stop_event() 不再单独触发一轮

时间语义：
  * 静默窗口 = wait_seconds（每来一条新消息就从该条重算）
  * 硬上限   = max_wait_seconds（从本轮第一条算起，到点强制放行，防无限等待）
"""

from __future__ import annotations

import asyncio
import time

SEP = "\n"
SCOPE_PRIVATE = "private"
SCOPE_BOTH = "both"
VALID_SCOPES = (SCOPE_PRIVATE, SCOPE_BOTH)


def build_key(
    umo: str,
    group_id: str = "",
    sender_id: str = "",
    scope: str = SCOPE_PRIVATE,
) -> str | None:
    """算出缓冲用的会话键。返回 None 表示这条消息不在生效范围里。

    * 私聊：键就是 unified_msg_origin（一个私聊会话一路）
    * 群聊（scope=both）：键 = 会话 + 发送者，保证不会把不同人的话混成一轮
    """
    umo = (umo or "").strip()
    if not umo:
        return None
    if group_id:
        if scope != SCOPE_BOTH:
            return None
        return f"{umo}|{sender_id or 'unknown'}"
    return umo


def is_command(text: str, wake_prefixes: list[str] | None = None) -> bool:
    """指令一概不拦（/help、/reset 这些要能立刻执行）。"""
    t = (text or "").strip()
    if not t:
        return True
    if t.startswith("/") or t.startswith("\\"):
        return True
    for p in wake_prefixes or []:
        if p and t.startswith(p):
            return True
    return False


def merge_texts(texts: list[str], sep: str = SEP) -> str:
    """把多条消息拼成一条。空串丢弃，单条时原样返回。"""
    parts = [(t or "").strip() for t in texts]
    parts = [p for p in parts if p]
    return sep.join(parts)


class Round:
    """一轮连发的缓冲。"""

    def __init__(
        self,
        wait_seconds: float,
        max_wait_seconds: float,
        now: float | None = None,
        key: str = "",
    ) -> None:
        self.key = key
        self.wait_seconds = max(0.0, float(wait_seconds))
        # 硬上限是「绝不突破」的上界：配得比静默窗口还小时，硬上限说了算
        self.max_wait_seconds = max(0.0, float(max_wait_seconds))
        self.first_ts = time.monotonic() if now is None else float(now)
        self.last_ts = self.first_ts
        self.texts: list[str] = []
        self.media: list = []          # 后来消息里的图片/文件等（插件侧填，纯逻辑不管）
        self.count = 0
        self.closed = False
        self.activity = asyncio.Event()

    # ---- 收消息 ----
    def add(self, text: str, now: float | None = None, media: list | None = None) -> None:
        now = time.monotonic() if now is None else float(now)
        if (text or "").strip():
            self.texts.append(text.strip())
            self.count += 1
        if media:
            self.media.extend(media)
        self.last_ts = now
        self.activity.set()

    # ---- 时间 ----
    def quiet_deadline(self) -> float:
        return self.last_ts + self.wait_seconds

    def hard_deadline(self) -> float:
        return self.first_ts + self.max_wait_seconds

    def next_wait(self, now: float | None = None) -> float:
        """还要等多少秒；<=0 表示现在就该放行。"""
        now = time.monotonic() if now is None else float(now)
        return max(0.0, min(self.quiet_deadline(), self.hard_deadline()) - now)

    def hard_cap_hit(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else float(now)
        return now >= self.hard_deadline() - 1e-9

    def elapsed(self, now: float | None = None) -> float:
        now = time.monotonic() if now is None else float(now)
        return now - self.first_ts

    def merged(self, sep: str = SEP) -> str:
        return merge_texts(self.texts, sep)


class Store:
    """会话键 -> 当前正在攒的那一轮。"""

    def __init__(self, wait_seconds: float, max_wait_seconds: float) -> None:
        self.wait_seconds = float(wait_seconds)
        self.max_wait_seconds = float(max_wait_seconds)
        self.rounds: dict[str, Round] = {}

    def decide(self, key: str, text: str, now: float | None = None) -> tuple[Round, bool]:
        """把一条消息归入某一轮。

        返回 (round, is_leader)：
          * is_leader=True  → 调用方是 leader，必须自己 await wait_window() 再放行
          * is_leader=False → 调用方是 follower，塞完就该 stop_event()
        没有活跃轮次（或上一轮刚被关掉）时，这条消息开新一轮并当 leader。
        """
        now = time.monotonic() if now is None else float(now)
        rnd = self.rounds.get(key)
        if rnd is None or rnd.closed:
            rnd = Round(self.wait_seconds, self.max_wait_seconds, now=now, key=key)
            self.rounds[key] = rnd
            rnd.add(text, now)
            return rnd, True
        rnd.add(text, now)
        return rnd, False

    def close(self, key: str, rnd: Round) -> bool:
        """关掉一轮（放行前调用）。返回 True 表示这轮是当前活跃轮、成功关掉。"""
        rnd.closed = True
        cur = self.rounds.get(key)
        if cur is rnd:
            self.rounds.pop(key, None)
            return True
        return False

    def pending(self, key: str) -> Round | None:
        rnd = self.rounds.get(key)
        if rnd is None or rnd.closed:
            return None
        return rnd

    def active_count(self) -> int:
        return sum(1 for r in self.rounds.values() if not r.closed)


async def wait_window(rnd: Round, now=time.monotonic) -> float:
    """leader 用：阻塞到静默窗口结束或硬上限到点，返回本轮实际经历秒数。

    每被新消息唤醒一次就按新的 last_ts 重算；硬上限到了立刻返回（不无限等）。
    """
    start = rnd.first_ts
    while True:
        t = rnd.next_wait(now())
        if t <= 0:
            return now() - start
        try:
            await asyncio.wait_for(rnd.activity.wait(), timeout=t)
        except asyncio.TimeoutError:
            # 到点或刚好有新消息：回到循环上重算（重算后 <=0 就真的放行）
            continue
        rnd.activity.clear()
