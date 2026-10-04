"""给她/给主人看的时间，统一按 **Asia/Shanghai**（2026-10-04 的教训落成的规矩）。

背景：Hermes 容器跑在 **UTC**（`date` = UTC，`TZ` 是空的），而主人和她的世界是北京时间。
之前 `proactive.py` 用 `time.localtime()` 判静默窗 → 容器把 UTC 当本地 →
「00:30–07:00 不触发」实际落在了**北京 08:30–15:00**，结果她凌晨 4:34 / 5:44 / 6:05 / 7:25
连发了 4 条（主人：「晚上好像没暂停」）。

规矩：**凡是给人看的时间**（静默窗判断、群窗口时间戳、注入里的"现在几点"、媒体目录日期、
"今天第几次"）都走这里，别用 `time.localtime()` / 裸 `datetime.now()`。
内部存储（epoch 秒、JSON 里的时间戳）继续用 epoch，不受影响。
"""
from __future__ import annotations

import datetime as _dt

try:                                              # 镜像里有 tzdata 就用真时区
    from zoneinfo import ZoneInfo
    TZ: _dt.tzinfo = ZoneInfo("Asia/Shanghai")
except Exception:                                 # 没有就固定 +8（中国无夏令时，等价）
    TZ = _dt.timezone(_dt.timedelta(hours=8), "CST")


def now() -> _dt.datetime:
    return _dt.datetime.now(TZ)


def at(ts: float) -> _dt.datetime:
    return _dt.datetime.fromtimestamp(float(ts), TZ)


def hhmm(ts: float = None) -> str:
    return (at(ts) if ts is not None else now()).strftime("%H:%M")


def stamp(ts: float = None) -> str:
    """``MM-DD HH:MM:SS`` —— 群窗口每行前面的那个时间。"""
    return (at(ts) if ts is not None else now()).strftime("%m-%d %H:%M:%S")


def full_stamp(ts: float = None) -> str:
    """``YYYY-MM-DD HH:MM:SS`` —— 日志行那种完整时间戳。"""
    return (at(ts) if ts is not None else now()).strftime("%Y-%m-%d %H:%M:%S")


def date(ts: float = None) -> str:
    """``YYYY-MM-DD`` —— 媒体目录、跨日计数用这个。"""
    return (at(ts) if ts is not None else now()).strftime("%Y-%m-%d")


def minutes_of_day(ts: float = None) -> int:
    d = at(ts) if ts is not None else now()
    return d.hour * 60 + d.minute


def epoch(y: int, mo: int, d: int, h: int, mi: int, s: int = 0) -> float:
    """北京时刻 → epoch 秒（测试用；生产没人需要反着算）。"""
    return _dt.datetime(y, mo, d, h, mi, s, tzinfo=TZ).timestamp()
