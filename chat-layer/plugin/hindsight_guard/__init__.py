"""群聊回合的**记忆隔离闸**（群聊 C1「@ 必答」的硬前置）。

## 为什么要有这个东西

`PLAN-group-chat.md` §4.3 / §4.1③ 的已定决定是「**群消息默认零入库**」，C1 之前这条是**免费**的 ——
群消息压根不到 agent。C1 打开 `group_wake_mode != collect-only` 之后，被 @ 唤醒的那一轮就是一次
**正常 agent 回合**，而 Hindsight 的 `auto_retain` 是 **profile 级、无条件化**的：

    取证实验（`plugin/hermes_onebot/tests/evidence_group_retain.py`，2026-09-23 实跑）：
    group: retain_count=1 banks=['mianmian-history']
    dm   : retain_count=1 banks=['mianmian-history']
    → 群回合**确实会**把群消息 retain 进主人私聊的同一个 bank（`metadata.chat_type=group`）。

所以「群回合不污染主库」这句话，必须**从源头再加一道闸**才成立。本插件就是那道闸。

## 它怎么做到

它是 `plugins/memory/hindsight`（bundled）的**透明包装**：配置里把
`memory.provider: hindsight` 换成 `hindsight_guard`，其余一切（连接、bank、tags、
recall/retain 实现、工具 schema、shutdown…）**全部原样委托**给 bundled 实现，只在
**群回合**上拦三个动作：

  1. `sync_turn()` —— 不 retain（不写主库）
  2. `prefetch()` / `queue_prefetch()` —— 不召回（群里不注入私聊记忆，PLAN §7 S11/S3 的机制侧保证）
  3. `handle_tool_call()` —— 群里不许调 `hindsight_retain` / `hindsight_recall` / `hindsight_reflect`
     （否则「群里不能写」会被一次工具调用绕过）

**「这一轮是群回合」怎么判**（两个信号，任一命中即拦）：

  * **主信号 = `chat_type`**（第一方字段）：`MemoryManager.initialize_all()` 会把
    `agent._chat_type` 透传给 `initialize(chat_type=...)`（`agent/agent_init.py` 的
    `_GATEWAY_IDENTITY_PARAMS`），群会话就是 `group`。实测聊天门 state.db 里 `sessions.chat_type`
    已有 `dm` 记录，说明这条链路是真的在传值。
  * **兜底信号 = 文本标记**：onebot 适配器给每个群回合的正文头部打的
    `source=qq-group` 标记（`group_wake.py::TURN_MARKER`）。它同时覆盖
    「chat_type 缺失（非网关路径）」与「同一 agent 被复用」两种情况。

**方向是 fail-open 还是 fail-closed？** —— 两处都做了硬保证，不留静默缺口：

  * 闸门自身 fail-open（认不出来就不拦）**换不来安全**，所以适配器侧另有硬前置：
    `group_memory_guard_required: true` 时，**只有本闸就位（`memory.provider` 指向本插件
    且插件文件在位）适配器才会唤醒**；否则拒绝唤醒并记 `group_memory_guard` 异常（见
    `adapter.py::_group_memory_isolation_status`）。即：**闸门没到位 = 不 @ 唤醒**。
  * 拦下的事情都会计数落盘（`state.json`：`retain_skipped` / `recall_skipped` / `tool_blocked`
    + 最后一条的时刻与 session），供 doctor / 主人终验时**读证据**（不落消息正文）。

## 配置

`<HERMES_HOME>/hindsight_guard/config.json`（缺省即用下面的默认值）：

```json
{
  "enabled": true,
  "blocked_chat_types": ["group", "channel", "supergroup", "guild"],
  "marker": "source=qq-group",
  "write_tools": ["hindsight_retain"],
  "read_tools": ["hindsight_recall", "hindsight_reflect"]
}
```

回退（一行，秒级）：`memory.provider: hindsight` → 恢复 C1 之前的样子（群回合会入库）。

红线：本模块**不打印任何消息正文**到日志（只打条数、chat_type、session 尾部与原因）。
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hermes_constants import get_hermes_home

# bundled 实现（**唯一**的基类）：继承它 = 其余行为 100% 与 bundled 一致。
try:  # pragma: no cover - 导入失败时下面的 load_error 会记录并在 is_available 里报不可用
    from plugins.memory.hindsight import HindsightMemoryProvider as _HindsightBase
    _IMPORT_ERROR = ""
except Exception as _e:  # noqa: BLE001
    _HindsightBase = None  # type: ignore[assignment]
    _IMPORT_ERROR = repr(_e)

logger = logging.getLogger(__name__)

GUARD_NAME = "hindsight_guard"
STATE_VERSION = 1
DEFAULT_BLOCKED_CHAT_TYPES: Tuple[str, ...] = ("group", "channel", "supergroup", "guild")
DEFAULT_MARKER = "source=qq-group"
DEFAULT_WRITE_TOOLS: Tuple[str, ...] = ("hindsight_retain",)
DEFAULT_READ_TOOLS: Tuple[str, ...] = ("hindsight_recall", "hindsight_reflect")


def guard_dir() -> Path:
    return get_hermes_home() / GUARD_NAME


def load_guard_config() -> Dict[str, Any]:
    """`<HERMES_HOME>/hindsight_guard/config.json`；缺失/坏文件 = 默认值（坏文件记日志，不抛）。"""
    cfg: Dict[str, Any] = {
        "enabled": True,
        "blocked_chat_types": list(DEFAULT_BLOCKED_CHAT_TYPES),
        "marker": DEFAULT_MARKER,
        "write_tools": list(DEFAULT_WRITE_TOOLS),
        "read_tools": list(DEFAULT_READ_TOOLS),
    }
    p = guard_dir() / "config.json"
    try:
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update({k: v for k, v in data.items() if v is not None})
    except Exception as e:  # noqa: BLE001
        logger.warning("[hindsight_guard] config unreadable (%s), using defaults", type(e).__name__)
    for key in ("blocked_chat_types", "write_tools", "read_tools"):
        v = cfg.get(key)
        if isinstance(v, str):
            cfg[key] = [x.strip() for x in v.split(",") if x.strip()]
        elif not isinstance(v, (list, tuple)):
            cfg[key] = []
        cfg[key] = [str(x).strip().lower() for x in cfg[key]]
    cfg["marker"] = str(cfg.get("marker") or DEFAULT_MARKER)
    cfg["enabled"] = bool(cfg.get("enabled", True))
    return cfg


def is_group_turn_text(text: Any, marker: str = DEFAULT_MARKER) -> bool:
    """文本里有没有适配器打的群回合标记（**纯函数**，可离线单测）。"""
    return bool(marker) and marker in str(text or "")


class HindsightGuardProvider(_HindsightBase if _HindsightBase is not None else object):  # type: ignore[misc]
    """Hindsight 的透明包装 + 群回合记忆隔离闸。"""

    def __init__(self) -> None:
        if _HindsightBase is None:  # pragma: no cover - 只在 bundled 导入失败时
            raise RuntimeError(f"hindsight bundle not importable: {_IMPORT_ERROR}")
        super().__init__()
        self._guard = load_guard_config()
        self._chat_type = ""
        self._guard_session_id = ""
        self._session_blocked = False
        self._turn_blocked: Optional[bool] = None
        self._blocks = 0
        self._counters = {"retain_skipped": 0, "recall_skipped": 0, "tool_blocked": 0}
        self._last_block = ""

    # ── 判据 ───────────────────────────────────────────────────────────────

    def _blocked_chat_type(self, chat_type: Any) -> bool:
        ct = str(chat_type or "").strip().lower()
        return bool(ct) and ct in set(self._guard["blocked_chat_types"])

    def _marker_hit(self, text: Any) -> bool:
        return is_group_turn_text(text, self._guard["marker"])

    def _is_blocked(self, text: Any = "") -> bool:
        """本轮该不该拦。`enabled: false` = 整体关闭（回到 bundled 行为）。"""
        if not self._guard["enabled"]:
            return False
        if self._turn_blocked is not None:
            return bool(self._turn_blocked) or self._marker_hit(text)
        return self._session_blocked or self._marker_hit(text)

    def _note_block(self, kind: str, *, session_id: str = "") -> None:
        self._blocks += 1
        self._counters[kind] = self._counters.get(kind, 0) + 1
        self._last_block = f"{kind}@{int(time.time())}"
        if self._blocks in (1, 2) or self._blocks % 50 == 0:
            logger.info(
                "[hindsight_guard] 群回合记忆隔离生效：%s（本进程累计 %d 次；chat_type=%s；"
                "session 尾 6 位=%s）——不写主库/不召回",
                kind, self._blocks, self._chat_type or "?",
                str(session_id or self._guard_session_id)[-6:],
            )
        self.write_state()

    def write_state(self) -> None:
        """把「闸门拦了什么」写成证据（**只有计数与时刻，没有消息正文**）。"""
        try:
            d = guard_dir()
            d.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": STATE_VERSION,
                "guard": GUARD_NAME,
                "enabled": bool(self._guard["enabled"]),
                "blocked_chat_types": list(self._guard["blocked_chat_types"]),
                "marker": self._guard["marker"],
                "pid": os.getpid(),
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
                "counters": dict(self._counters),
                "blocks_total": self._blocks,
                "last_block": self._last_block,
                "last_session_tail": str(self._guard_session_id)[-6:],
                "last_chat_type": self._chat_type,
            }
            tmp = d / "state.json.tmp"
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(d / "state.json")
        except Exception as e:  # noqa: BLE001
            logger.debug("[hindsight_guard] state write failed: %s", e)

    def read_state(self) -> Dict[str, Any]:
        """只读回读（doctor / 体检用）。"""
        try:
            p = guard_dir() / "state.json"
            if p.is_file():
                data = json.loads(p.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}
        return {}

    # ── 生命周期：认会话 / 认轮 ─────────────────────────────────────────────

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        self._guard_session_id = str(session_id or "")
        self._chat_type = str(kwargs.get("chat_type") or "").strip().lower()
        self._session_blocked = self._blocked_chat_type(self._chat_type)
        self._turn_blocked = None
        super().initialize(session_id, **kwargs)
        logger.info(
            "[hindsight_guard] 就绪：memory.provider=%s（透明包装 %s）· chat_type=%s · "
            "本会话记忆隔离=%s · 拦群聊类型=%s · 标记=%s",
            GUARD_NAME, type(self).__mro__[1].__name__, self._chat_type or "?",
            "ON（群回合不写不读）" if self._session_blocked else "off（私聊照旧）",
            ",".join(self._guard["blocked_chat_types"]) or "-", self._guard["marker"],
        )
        self.write_state()

    def on_turn_start(self, turn_number: int, message: str, **kwargs: Any) -> None:
        # 工具调用拿不到 session/chat_type → 在每轮开头把「本轮是不是群回合」钉下来。
        self._turn_blocked = self._session_blocked or self._marker_hit(message)
        try:
            super().on_turn_start(turn_number, message, **kwargs)
        except Exception as e:  # noqa: BLE001
            logger.debug("[hindsight_guard] inner on_turn_start failed: %s", e)

    def on_session_switch(self, new_session_id: str, **kwargs: Any) -> None:
        self._guard_session_id = str(new_session_id or "")
        ct = kwargs.get("chat_type")
        if ct is not None and str(ct).strip():
            self._chat_type = str(ct).strip().lower()
            self._session_blocked = self._blocked_chat_type(self._chat_type)
            self._turn_blocked = None
        try:
            super().on_session_switch(new_session_id, **kwargs)
        except Exception as e:  # noqa: BLE001
            logger.debug("[hindsight_guard] inner on_session_switch failed: %s", e)

    # ── 拦：不写 ───────────────────────────────────────────────────────────

    def sync_turn(self, user_content: str, assistant_content: str, *,
                  session_id: str = "", **kwargs: Any) -> None:
        if self._is_blocked(user_content):
            self._note_block("retain_skipped", session_id=session_id)
            return
        super().sync_turn(user_content, assistant_content, session_id=session_id, **kwargs)

    # ── 拦：不读 ───────────────────────────────────────────────────────────

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if self._is_blocked(query):
            self._note_block("recall_skipped", session_id=session_id)
            return ""
        return super().prefetch(query, session_id=session_id)

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        if self._is_blocked(query):
            self._note_block("recall_skipped", session_id=session_id)
            return
        super().queue_prefetch(query, session_id=session_id)

    # ── 拦：工具面 ─────────────────────────────────────────────────────────

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs: Any) -> str:
        name = str(tool_name or "").strip().lower()
        blocked_tools = set(self._guard["write_tools"]) | set(self._guard["read_tools"])
        if self._turn_blocked and name in blocked_tools:
            self._note_block("tool_blocked")
            return json.dumps({
                "ok": False,
                "error": "group_turn_memory_isolated",
                "note": ("现在是群聊回合：不写也不读长期记忆库（避免群里的闲聊污染主人私聊的记忆）。"
                         "群里想说什么就直接说；真要她记住什么，让主人在私聊里说。"),
            }, ensure_ascii=False)
        return super().handle_tool_call(tool_name, args, **kwargs)


def register(ctx) -> None:
    """插件入口（记忆 provider 走 `memory.provider` 激活；这里显式注册实例）。"""
    ctx.register_memory_provider(HindsightGuardProvider())
