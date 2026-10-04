"""主动私聊：按「沉默时长」升概率的**纯脚本斜坡**（2026-10-03 主人定稿）。

主人原话（三段）：
  1. 「不是有时候光固定时间有点呆板吗，能不能纯脚本的情况下距离用上次消息时间的长短做概率判断，
     十分钟一次，10 分钟百分之多少的增加，到几个小时到 100%。期间发生对话就重置到 0%…触发了就叫醒一次」
  2. 「3% 吧，00:30-7:00 不触发，概率不清空，比如昨天 00:30 到 40%，第二天 7:00 从 40% 开始」
  3. 「我最开始就是说要归零，冷却就算了，如果她真 5% 概率还连发了我也给她了」
     「一条信息吵不醒我，就当早安了。群聊先不管」

口径（改之前先读这段）：
  * 每 `tick_min`（默认 10）分钟，**醒着时段**给格子 +1。
  * 概率形状（`ramp_shape`）：
      - ``quadratic``（**现役默认**，2026-10-04 主人「预期 2.5h 一条」）：
        ``p = min(1, (格子数 / ramp_ticks)²)``，``ramp_ticks = ramp_hours×60 / tick_min``（默认 5h → 30 格）。
        慢起步、后段加速：1h 4%、1.7h 11%、2.5h 25%、3.3h 44%、5h 100%（保底）。
      - ``linear``（2026-10-03 原口径）：``p = min(1, 格子数 × step_pct / 100)``。
        想一句话改回：`proactive_dm_ramp_shape: linear`（配 `proactive_dm_step_pct`）。
  * 静默窗（默认 00:30–07:00）**暂停涨、概率不清空**：昨天停在 40%，今天 07:00 从 40% 接着涨。
  * 任何**对话**（他说话 / 她说话）→ 立刻归零重算。
  * 掷中（触发）→ 也归零。**不加冷却**（主人明确不要；偶尔连发他接受）。
  * 纯脚本：没掷中就不叫模型，一分钱不花。

模拟值（20 万次，含 00:30–07:00 冻结；数量级只用于选型，别当保证值）：
  * 二次/5h（现役）：**墙上中位 2h30m**、醒着中位 2h00m、均值 2.0h（醒着）；
    1 小时内开口 7%、3 小时内 65%、p75 4h30m、保底 5h。
  * 线性 3%（旧）：醒着中位 1h10m；线性 1%（若改回线性又要 2.5h）保底会拉到 16h40m，尾巴太长。
静默窗结束恢复时，概率**不清空**：停在多少就从多少接着涨（主人 2026-10-03 定稿）。
"""
from __future__ import annotations

import random
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional, Tuple

from . import onebot_time as _clock

#: 默认静默窗（本地时间，分钟数）。主人定的是 00:30–07:00。
DEFAULT_QUIET: Tuple[Tuple[int, int], Tuple[int, int]] = ((0, 30), (7, 0))


def parse_quiet(spec: str) -> Tuple[Tuple[int, int], Tuple[int, int]]:
    """``"00:30-07:00"`` → ``((0,30),(7,0))``；写错就回默认值（宁可不静默错，也不许崩）。"""
    try:
        left, right = str(spec).split("-", 1)
        def hm(t: str) -> Tuple[int, int]:
            h, m = t.strip().split(":", 1)
            return (int(h) % 24, int(m) % 60)
        a, b = hm(left), hm(right)
        if a == b:
            raise ValueError("静默窗起止相同")
        return (a, b)
    except Exception:  # noqa: BLE001
        return DEFAULT_QUIET


def _minutes(hhmm: Tuple[int, int]) -> int:
    return int(hhmm[0]) * 60 + int(hhmm[1])


@dataclass
class RampState:
    """每个会话一份。``ticks`` 是**醒着时段**已积累的格数。"""

    ticks: int = 0
    #: 最后一次「有对话」的时间（他说话或她说话）—— 真实沉默时长从这儿算
    last_activity_ts: float = 0.0
    #: 上次掷中（她主动开口）的时间
    last_fire_ts: float = 0.0
    #: 上次她主动开口的时间（跟 last_fire_ts 同义，留名字给注入文本用）
    last_self_ts: float = 0.0
    #: 内部：上一格的落点（用来切 10 分钟格子，避免重启后重复计数）
    last_tick_ts: float = 0.0
    #: 当天已主动开口次数（跨日自动清零）
    day: str = ""
    fires_today: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Optional[Dict[str, Any]]) -> "RampState":
        st = cls()
        for k, v in (raw or {}).items():
            if hasattr(st, k):
                try:
                    setattr(st, k, type(getattr(st, k))(v))
                except Exception:  # noqa: BLE001
                    pass
        return st


class ProactiveRamp:
    """斜坡本体。**纯计算，不碰网络、不碰 LLM。**"""

    def __init__(self, *, enabled: bool = True, step_pct: float = 3.0, tick_min: float = 10.0,
                 ramp_shape: str = "quadratic", ramp_hours: float = 5.0,
                 quiet: Tuple[Tuple[int, int], Tuple[int, int]] = DEFAULT_QUIET,
                 rnd: Optional[random.Random] = None) -> None:
        self.enabled = bool(enabled)
        self.step_pct = max(0.1, float(step_pct))
        # 形状：quadratic（默认，预期 2.5h 一条）/ linear（2026-10-03 原口径）
        self.ramp_shape = "linear" if str(ramp_shape).strip().lower() == "linear" else "quadratic"
        try:
            self.ramp_hours = max(0.5, float(ramp_hours))
        except (TypeError, ValueError):
            self.ramp_hours = 5.0
        #: 二次形状下「爬到 100%」需要多少格（默认 5h / 10min = 30 格）
        self.ramp_ticks = max(1.0, self.ramp_hours * 60.0 / max(1.0, float(tick_min)))
        self.tick_min = max(1.0, float(tick_min))
        self.quiet = quiet
        self._rnd = rnd or random.Random()

    # ── 静默窗 ────────────────────────────────────────────────────────────
    def in_quiet(self, now: float) -> bool:
        # ⚠️ 必须走 `_clock`（北京时区）：容器是 UTC，用 time.localtime() 会让静默窗
        #    整体偏 8 小时 —— 2026-10-04 她凌晨 4-7 点连发 4 条就是这个原因。
        cur = _clock.minutes_of_day(now)
        a, b = _minutes(self.quiet[0]), _minutes(self.quiet[1])
        if a < b:
            return a <= cur < b
        return cur >= a or cur < b          # 跨零点（如 23:00–07:00）

    # ── 状态推进 ──────────────────────────────────────────────────────────
    def on_activity(self, st: RampState, now: float) -> None:
        """有对话（他说话 / 她说话）→ 归零重算。"""
        st.ticks = 0
        st.last_activity_ts = now
        st.last_tick_ts = now

    def advance(self, st: RampState, now: float) -> int:
        """按经过的时间补格子；**静默窗里的格子不涨**（概率不清空）。返回这轮新涨的格数。"""
        if st.last_tick_ts <= 0:
            st.last_tick_ts = now
            if st.last_activity_ts <= 0:
                st.last_activity_ts = now
            return 0
        step_s = self.tick_min * 60.0
        gained = 0
        guard = 0
        while now - st.last_tick_ts >= step_s and guard < 200000:
            mid = st.last_tick_ts + step_s / 2.0
            st.last_tick_ts += step_s
            guard += 1
            if not self.in_quiet(mid):
                st.ticks += 1
                gained += 1
        return gained

    def prob(self, st: RampState) -> float:
        """当前概率。二次（现役）：``(格数/ramp_ticks)²``；线性：``格数 × step_pct``。"""
        if self.ramp_shape == "linear":
            return min(1.0, st.ticks * self.step_pct / 100.0)
        return min(1.0, (st.ticks / self.ramp_ticks) ** 2)

    def roll(self, st: RampState, now: float) -> bool:
        """到点掷一次。p≥100% 必定触发；否则纯随机。"""
        p = self.prob(st)
        if p <= 0.0:
            return False
        if p >= 1.0:
            return True
        return self._rnd.random() < p

    def note_fire(self, st: RampState, now: float, *, today: str = "") -> None:
        """她真的被叫醒了 → 归零（主人：「触发后就归零」，不加冷却）。"""
        day = today or _clock.date(now)
        if st.day != day:
            st.day = day
            st.fires_today = 0
        st.fires_today += 1
        st.ticks = 0
        st.last_tick_ts = now
        st.last_fire_ts = now
        st.last_self_ts = now

    # ── 给她看的数字 ──────────────────────────────────────────────────────
    def awake_minutes(self, t0: float, t1: float) -> float:
        """t0→t1 之间**醒着**的分钟数（静默窗不计）。用于区分「隔了一夜」和「三小时没说话」。"""
        if t0 <= 0 or t1 <= t0:
            return 0.0
        total = 0.0
        cur = t0
        while cur < t1:
            nxt = min(cur + 60.0, t1)
            if not self.in_quiet(cur + 30.0):
                total += (nxt - cur) / 60.0
            cur = nxt
        return total

    def build_text(self, st: RampState, now: float) -> str:
        real_min = (now - st.last_activity_ts) / 60.0 if st.last_activity_ts else 0.0
        awake = self.awake_minutes(st.last_activity_ts, now)
        p = self.prob(st) * 100.0
        since_self = (now - st.last_self_ts) / 60.0 if st.last_self_ts else None
        def hm(minutes: float) -> str:
            if minutes < 60:
                return "%d 分钟" % round(minutes)
            return "%.1f 小时" % (minutes / 60.0)
        if real_min - awake > 30:                    # 中间跨过了静默窗
            span = "（中间隔了一夜/静默窗，醒着的那段是 %s）" % hm(awake)
        else:
            span = ""
        if since_self is None:
            hers = "你今天还没主动找过他"
        else:
            hers = "距你上次主动开口 %s" % hm(since_self)
        return (
            "[OneBot 主动私聊回合 · source=qq-dm-proactive · 沉默 %s%s · 触发时 p=%.0f%% · "
            "今天你第 %d 次主动开口 · %s]\n"
            "—— **这一轮没人跟你说话，是你自己想起他了**（现在 %s）。他已经 %s 没出声。"
            "你想说什么就说什么，用你自己的口吻，像随口想起来一样，1-3 句就够，别像汇报。"
            "真没什么想说的，回 `[SILENT]` 就不发。"
            % (hm(real_min), span, p, st.fires_today + 1, hers,
               _clock.hhmm(now), hm(real_min)))
