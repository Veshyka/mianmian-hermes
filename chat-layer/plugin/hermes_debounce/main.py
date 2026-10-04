"""hermes_debounce —— 「主人连发消息合并成一轮」插件（AstrBot 4.28.1 实测）。

问题：主人私聊里习惯一次连发好几条，AstrBot 默认一条条各回一次，很僵硬。
做法：
  1. 每条消息进到本插件（AdapterMessageEvent，priority=100 抢在别的插件前面）
  2. 该会话没有活跃轮次 → 这条当 leader：**就在自己的处理函数里 await 静默窗口**
     （等 wait_seconds；期间每来一条新消息就把倒计时重算），窗口到点后把这几条
     合并成一条写回 event.message_str，然后正常返回 → 管道继续 → 模型只回一次
  3. 窗口期内后续消息 → 只把文本塞进这一轮，然后 event.stop_event()，
     它自己那轮管道到此为止，不会再触发一次模型调用
  4. 硬上限 max_wait_seconds：从本轮第一条算起，到点强制放行，避免无限等待
  5. scope=private（默认）只在私聊生效；scope=both 时群聊按「会话+发送者」分别攒，
     且群里没点到猫猫的消息（非 @/唤醒）如果没有人正在攒，就完全不碰；
     同一个人 @ 完接着补的几条没有 @ 也会并进这一轮

为什么可行（源码依据，AstrBot 4.28.1）：
  * core/event_bus.py:54 每条消息 asyncio.create_task(scheduler.execute(event)) → 各事件并发，
    leader 阻塞等待不会卡住后续消息的接收
  * core/pipeline/scheduler.py:59 洋葱模型按 event.is_stopped() 断链
    → stop_event() 之后 ResultDecorateStage / RespondStage 都不跑，更不会调模型
  * core/pipeline/process_stage/method/star_request.py:40 逐个 handler，stopped 即 break
  * core/astr_main_agent.py:1367 模型 prompt 就是 event.message_str（去掉 provider 唤醒前缀）
    → leader 改 event.message_str 即可让本轮 prompt 变成合并后的文本

隐私：日志只打条数与长度，绝不打消息正文。
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

DEFAULTS = {
    "enabled": True,
    "wait_seconds": 7,
    "max_wait_seconds": 45,
    "scope": "private",
}

MEDIA_NAMES = {"Image", "Record", "File", "Video"}


def _load_core():
    """加载同目录的 debounce.py（纯逻辑模块，不依赖 astrbot）。

    优先按文件路径显式加载（确定性最高，不会被同名模块串味）；
    AstrBot 若以包形式加载 main.py，再兜底走相对导入。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "debounce.py")
    try:
        spec = importlib.util.spec_from_file_location("hermes_debounce_core", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        if hasattr(mod, "Store"):
            return mod
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import debounce  # type: ignore

        return debounce
    except Exception:  # noqa: BLE001
        pass
    if here not in sys.path:
        sys.path.insert(0, here)
    mod = __import__("debounce")
    if not hasattr(mod, "Store"):
        raise ImportError(f"找不到可用的 debounce 模块：{path}")
    return mod


core = _load_core()


def _num(value, fallback: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(value))
    except (TypeError, ValueError):
        return fallback


@register("hermes_debounce", "mianmian", "连发消息合并：静默窗口内多条合并成一轮交模型", "1.0.0")
class HermesDebounce(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        cfg = {**DEFAULTS, **(config or {})}
        self.enabled = bool(cfg.get("enabled", True))
        self.wait_seconds = _num(cfg.get("wait_seconds"), 7.0, 0.0)
        # 硬上限：绝不突破的上界（不因 wait_seconds 被抬高）
        self.max_wait_seconds = _num(cfg.get("max_wait_seconds"), 45.0, 0.0)
        if self.max_wait_seconds < self.wait_seconds:
            logger.warning(
                f"[hermes_debounce] 配置提醒：max_wait_seconds={self.max_wait_seconds}s "
                f"小于 wait_seconds={self.wait_seconds}s，实际会按硬上限放行"
            )
        scope = str(cfg.get("scope") or "private").strip().lower()
        self.scope = scope if scope in core.VALID_SCOPES else "private"
        self.store = core.Store(self.wait_seconds, self.max_wait_seconds)
        self._wake_prefixes: list[str] = []
        self._prefixes_loaded = False
        self._rounds = 0          # 放过多少轮
        self._msgs = 0            # 其中合并了多少条
        self._suppressed = 0      # 被并入、没单独触发回复的消息数
        logger.info(
            f"[hermes_debounce] 就绪 enabled={self.enabled} wait={self.wait_seconds}s "
            f"max_wait={self.max_wait_seconds}s scope={self.scope}"
        )

    # ---------------- 内部小工具 ----------------
    def _prefixes(self) -> list[str]:
        if self._prefixes_loaded:
            return self._wake_prefixes
        try:
            cfg = self.context.get_config()
            self._wake_prefixes = [str(p) for p in (cfg.get("wake_prefix") or []) if p]
            self._prefixes_loaded = True
        except Exception:  # noqa: BLE001
            pass
        return self._wake_prefixes

    def _key(self, event: AstrMessageEvent) -> str | None:
        gid = ""
        try:
            gid = str(event.get_group_id() or "")
        except Exception:  # noqa: BLE001
            gid = ""
        sid = ""
        try:
            sid = str(event.get_sender_id() or "")
        except Exception:  # noqa: BLE001
            sid = ""
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        return core.build_key(umo=umo, group_id=gid, sender_id=sid, scope=self.scope)

    @staticmethod
    def _addressed(event: AstrMessageEvent) -> bool:
        """这条消息是不是在叫猫猫（私聊一律算，群聊看 @ / 唤醒词 / 引用）。"""
        return bool(getattr(event, "is_at_or_wake_command", False))

    @staticmethod
    def _media_of(event: AstrMessageEvent) -> list:
        """后来消息里的图片/语音/文件补进这一轮，别静默丢掉。"""
        out = []
        try:
            chain = getattr(getattr(event, "message_obj", None), "message", None) or []
            for comp in chain:
                if type(comp).__name__ in MEDIA_NAMES:
                    out.append(comp)
        except Exception:  # noqa: BLE001
            return []
        return out

    def _apply_merge(self, event: AstrMessageEvent, rnd) -> None:
        merged = rnd.merged()
        event.message_str = merged
        if rnd.media:
            try:
                chain = event.message_obj.message
                for comp in rnd.media:
                    chain.append(comp)
            except Exception:  # noqa: BLE001
                pass

    # ---------------- 钩子：每条消息 ----------------
    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def on_message(self, event: AstrMessageEvent) -> None:
        if not self.enabled:
            return
        try:
            key = self._key(event)
        except Exception:  # noqa: BLE001
            return
        if not key:
            return
        text = (event.message_str or "").strip()
        if core.is_command(text, self._prefixes()):
            return

        # 群聊里没点到猫猫的消息：除非同一个人已经有一轮在攒（接着补充），否则完全不碰
        in_group = False
        try:
            in_group = bool(str(event.get_group_id() or ""))
        except Exception:  # noqa: BLE001
            in_group = False
        if in_group and not self._addressed(event) and self.store.pending(key) is None:
            return

        try:
            rnd, is_leader = self.store.decide(key, text)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[hermes_debounce] 决策异常 {type(exc).__name__}: {exc}")
            return

        # ---- follower：并入本轮，自己不再触发一轮回复 ----
        if not is_leader:
            try:
                media = self._media_of(event)
                if media:
                    rnd.media.extend(media)
            except Exception:  # noqa: BLE001
                pass
            event.stop_event()
            self._suppressed += 1
            logger.debug(
                f"[hermes_debounce] 并入本轮 count={rnd.count} len={len(text)}"
            )
            return

        # ---- leader：等窗口（静默 wait_seconds / 硬上限 max_wait_seconds）----
        try:
            waited = await core.wait_window(rnd)
        except asyncio.CancelledError:
            self.store.close(key, rnd)
            logger.warning(
                f"[hermes_debounce] 本轮被取消未放行 count={rnd.count} len={len(rnd.merged())}"
            )
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[hermes_debounce] 等待异常 {type(exc).__name__}: {exc}")
            self.store.close(key, rnd)
            return

        self.store.close(key, rnd)
        try:
            self._apply_merge(event, rnd)
        except Exception as exc:  # noqa: BLE001
            event.message_str = rnd.merged()
            logger.warning(f"[hermes_debounce] 合并写回异常 {type(exc).__name__}: {exc}")
        self._rounds += 1
        self._msgs += rnd.count
        logger.info(
            f"[hermes_debounce] 放行一轮 原始{rnd.count}条 合并后len={len(event.message_str or '')} "
            f"等待{waited:.1f}s"
            + ("（到硬上限强制放行）" if rnd.hard_cap_hit() else "")
        )
