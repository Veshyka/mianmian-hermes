"""群聊 C1「@ 必答」的唤醒判定 / 限流 / 回合文本拼装 / 记忆隔离体检（**纯函数 + 纯数据**）。

单独成模块的理由：判定逻辑要能**离线单测**（`tests/check_group_wake.py`），不能埋进
906 行的适配器里；适配器只负责「调用它 → 按结果走」。

## 三态唤醒模式（`platforms.onebot.extra.group_wake_mode`）

| 模式 | 群消息进来时 | 成本 |
|---|---|---|
| `collect-only` | 只进滚动窗口（B 阶段现状） | **0 LLM** |
| `mention-only` | **被 @ / 被回复她 / 名字被点到** → 唤醒一轮 | 仅这几个命中点 |
| `full` | **每条群消息都唤醒**（＝旧 `group_wake_enabled: true` 的语义） | 按群消息量付费 |

* `mention-only` 是 **C1 落地档**（主人原话「被@时候必定说话」）。
* `full` 已实现但**未验收**：它是 `group_wake_enabled: true` 的老语义，留作退路，别开。
* 配置里只写了遗留键 `group_wake_enabled: true` → 解析成 `full`（老语义不悄悄改）；
  写了 `group_wake_mode` → **模式键是唯一权威**（避免两个键语义打架）。

## 记忆隔离硬前置

`group_memory_guard_required`（默认 true）时，适配器**只在记忆隔离就位时才唤醒**：
`memory.provider` 指向闸门 provider（默认 `hindsight_guard`）且插件文件在位。
判据与证据见 `hindsight_guard`（`<profile>/plugins/hindsight_guard/`）。
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

MODE_COLLECT = "collect-only"
MODE_MENTION = "mention-only"
MODE_GATED = "gated"
MODE_FULL = "full"
MODES = (MODE_COLLECT, MODE_MENTION, MODE_GATED, MODE_FULL)

#: `gated` 档的免费前置闸（照麦麦 `reply_necessity` / qq-bridge wake 概率重写）：
#: 过不了闸 → **连模型都不叫**（这才是省钱的地方）；过了闸 → 唤醒，**回不回由她自己判**（`SILENCE_MARKER`）。
GATE_DEFAULT_THRESHOLD = 0.35
#: 她"主动闭嘴"的标记：整条回复清洗后等于它 → 不发消息（只留日志）。
SILENCE_MARKER = "[SILENT]"

#: 「对话态」窗口：她刚被叫起来说过话后的这段时间里，**不必被 @ 也能接话**
#: （对应设计文档的状态机：潜水态 / 对话态。窗口内一律放行，回不回仍由她 `[SILENT]` 决定。）
CONV_WINDOW_S = 180.0
#: 对话态加成：够把一条普通消息（+0.20 有信息量）顶过默认阈值 0.35
CONV_BONUS = 0.45

#: 群回合正文头部标记（**闸门认它**：`hindsight_guard` 的兜底信号；也是给她看的场合提示）
#: 机器标记行（`hindsight_guard` 靠 `source=qq-group` + 行里的群号认群）。
#: 2026-10-03 去掉"生人档"字样 —— 主人看到这词觉得把群里人设整刻薄了；
#: 这行本来就只是给机器读的。
TURN_MARKER = ("[OneBot 群聊回合 · source=qq-group · {gid} · 触发={trigger}]")
#: 闸门用的机器标记（`hindsight_guard/config.json` 的 `marker` 必须与它一致）
SOURCE_MARKER = "source=qq-group"

DEFAULT_ALIAS_NAMES: Tuple[str, ...] = ("棉棉", "小棉")
GUARD_PROVIDER = "hindsight_guard"

TRIGGER_LABEL = {"at": "被@", "reply": "被回复", "name": "被点名", "gate": "过闸",
                 "full": "全量", "join_request": "有人申请入群", "none": "无"}

#: 「有人申请入群」这一轮单独加的任务说明（2026-10-03：主人要她自己审，不要他转达）
JOIN_REVIEW_NOTE = (
    "—— **这一轮不是聊天，是让你审一份入群申请**：最后那条系统记录里有申请人的 QQ 号和附言。"
    "默认**宽松放行**：看着像正常人就直接批（写 `[同意入群:QQ号]`）。"
    "只有明显是广告机器人、小号、来路不明，或者附言整段都是推广，才拒"
    "（写 `[拒绝入群:QQ号,理由]`，理由会带给对方，别写难听话）。"
    "拿不准也放进来——真有问题后面再踢。"
    "**批/拒随意，不用问主人**（他授权你自己审）。"
    "**这一轮别在群里说话**：你写的正文不会发到群里，只有上面两个标记起作用"
    "（主人要求申请这事不反馈到群里）——所以什么都不用说，写标记就行。")


# ── 配置解析 ───────────────────────────────────────────────────────────────


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def parse_mode(extra: Dict[str, Any]) -> str:
    """三态唤醒模式；非法值 → `collect-only`（**fail-safe 方向是不说话**）。"""
    raw = str((extra or {}).get("group_wake_mode") or "").strip().lower()
    if raw in MODES:
        return raw
    if raw:
        logger.warning("[onebot] unknown group_wake_mode=%r → fallback %s", raw, MODE_COLLECT)
        return MODE_COLLECT
    # 老键回退：历史语义就是「每条群消息都唤醒」→ full
    return MODE_FULL if _truthy((extra or {}).get("group_wake_enabled")) else MODE_COLLECT


def parse_alias_names(value: Any) -> Tuple[str, ...]:
    """她在这个群里的称呼（T2 点名）。逗号/空格/顿号分隔都认；写空 → 默认。"""
    if value is None or value == "":
        return DEFAULT_ALIAS_NAMES
    if isinstance(value, str):
        parts: List[str] = []
        for chunk in value.replace("，", ",").replace("、", ",").replace(" ", ",").split(","):
            if chunk.strip():
                parts.append(chunk.strip())
        return tuple(parts) or DEFAULT_ALIAS_NAMES
    if isinstance(value, Iterable):
        parts = [str(x).strip() for x in value if str(x).strip()]
        return tuple(parts) or DEFAULT_ALIAS_NAMES
    return DEFAULT_ALIAS_NAMES


def parse_int(value: Any, default: int, *, minimum: int = 0) -> int:
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


# ── 触发判定（纯函数）──────────────────────────────────────────────────────


#: 别人 `@全体成员` 算不算「被 @ 」（2026-10-03 主人拍板：**算**）。
#: 他原话「完全放开，而且别人@全体也要能唤醒」—— 群公告/通知也要能叫她。
AT_ALL_WAKES = True


def at_mentions(event: Dict[str, Any], self_id: str) -> bool:
    """这条消息有没有 @ 小号（含 `@全体成员`，见 `AT_ALL_WAKES`）。段落与 CQ 串两种格式都认。"""
    sid = str(self_id or "").strip()
    if not sid:
        return False

    def _hit(qq: str) -> bool:
        qq = str(qq or "").strip()
        if not qq:
            return False
        return qq == sid or (AT_ALL_WAKES and qq == "all")

    msg = event.get("message")
    if isinstance(msg, list):
        for seg in msg:
            if not isinstance(seg, dict) or seg.get("type") != "at":
                continue
            if _hit((seg.get("data") or {}).get("qq")):
                return True
        return False
    if isinstance(msg, str):
        if f"[CQ:at,qq={sid}]" in msg or f"[CQ:at,qq={sid}," in msg:
            return True
        return AT_ALL_WAKES and ("[CQ:at,qq=all]" in msg or "[CQ:at,qq=all," in msg)
    return False


def text_hits_alias(text: str, alias_names: Sequence[str]) -> bool:
    """群名片/昵称点名（T2）。**只认整串出现**，不做模糊匹配（防话痨）。"""
    t = str(text or "")
    if not t:
        return False
    return any(name and name in t for name in alias_names)


def decide(event: Dict[str, Any], *, self_id: str, alias_names: Sequence[str] = DEFAULT_ALIAS_NAMES,
           own_message_ids: Iterable[str] = (), reply_wakes: bool = True,
           mode: str = MODE_COLLECT, gate_state: Optional["GateState"] = None,
           gate_threshold: float = GATE_DEFAULT_THRESHOLD,
           now: Optional[float] = None,
           text: Optional[str] = None) -> Tuple[str, str]:
    """这一条群消息要不要唤醒她。

    返回 ``(reason, detail)``：``reason`` ∈ ``{"at","reply","name","gate","full","none"}``，
    ``detail`` 是给人看的一行（进日志/心跳，**不含正文**）。
    优先级 ``at > reply > name > gate``（一个回合只记一次主因）。

    ``text`` 是**判定用文本**的覆盖项（默认 ``_text(event)`` = 原始渲染）。
    存在的理由：语音（record 段）在原始渲染里只有 "[语音]" 占位，拿它打分等于这条消息没内容
    —— 2026-10-04 主人报「发语音从来没叫醒过她」，根因就在这。适配器会把**转写后的文本**传进来。
    """
    if mode == MODE_COLLECT:
        return "none", "collect-only(不唤醒)"
    if at_mentions(event, self_id):
        return "at", "被 @ 小号"
    if mode == MODE_FULL:
        return "full", "full 模式：每条群消息都唤醒"
    if reply_wakes:
        rid = event_reply_id(event)
        if rid and rid in set(str(x) for x in own_message_ids):
            return "reply", "引用/回复了她说的话"
    if text_hits_alias(_text(event) if text is None else text, alias_names):
        return "name", "正文里点了她的名字"
    if mode == MODE_GATED:
        if gate_state is None:
            return "none", "gated：门控状态缺失（fail-closed，不唤醒）"
        score, why = gate_score(event, self_id=self_id, alias_names=alias_names,
                                state=gate_state, now=now, threshold=gate_threshold,
                                text=text)
        if score < gate_threshold:
            return "none", "gated 未过闸 %.2f<%.2f｜%s" % (score, gate_threshold, why)
        return "gate", "gated 过闸 %.2f｜%s" % (score, why)
    return "none", "没被 @/回复/点名"


def event_reply_id(event: Dict[str, Any]) -> Optional[str]:
    msg = event.get("message")
    if isinstance(msg, list):
        for seg in msg:
            if isinstance(seg, dict) and seg.get("type") == "reply":
                rid = (seg.get("data") or {}).get("id")
                if rid is not None:
                    return str(rid)
    return None


def _text(event: Dict[str, Any]) -> str:
    from . import onebot_proto as _proto
    return _proto.extract_text(event)


# ── 限流（纯代码，0 token）─────────────────────────────────────────────────


class WakeLimiter:
    """@ 通道的每分钟/每小时硬上限（**防连点 + 成本兜底**，按群号独立）。

    被限流**必须留痕**（`last_reason`），不许静默丢弃 —— 静默丢弃会让主人以为「她坏了」。
    """

    def __init__(self, per_minute: int = 2, per_hour: int = 30) -> None:
        self.per_minute = max(1, int(per_minute))
        self.per_hour = max(1, int(per_hour))
        self._stamps: Dict[str, deque] = {}
        self.limited = 0
        self.last_reason = ""

    def _dq(self, gid: str) -> deque:
        return self._stamps.setdefault(str(gid), deque(maxlen=max(8, self.per_hour + 1)))

    def allow(self, gid: str, *, now: Optional[float] = None) -> Tuple[bool, str]:
        t = float(now if now is not None else time.time())
        dq = self._dq(gid)
        while dq and t - dq[0] > 3600.0:
            dq.popleft()
        minute_n = sum(1 for x in dq if t - x <= 60.0)
        if minute_n >= self.per_minute:
            self.limited += 1
            self.last_reason = f"per-minute({minute_n}/{self.per_minute})"
            return False, self.last_reason
        if len(dq) >= self.per_hour:
            self.limited += 1
            self.last_reason = f"per-hour({len(dq)}/{self.per_hour})"
            return False, self.last_reason
        dq.append(t)
        return True, "ok"

    def snapshot(self, gid: str, *, now: Optional[float] = None) -> Dict[str, int]:
        t = float(now if now is not None else time.time())
        dq = self._stamps.get(str(gid)) or deque()
        return {"minute": sum(1 for x in dq if t - x <= 60.0),
                "hour": sum(1 for x in dq if t - x <= 3600.0)}


# ── 回合文本 ───────────────────────────────────────────────────────────────


def mask_gid(gid: str) -> str:
    s = str(gid or "")
    return s if len(s) <= 5 else f"{s[:3]}***{s[-2:]}"


def build_turn_text(*, gid: str, trigger: str, window_text: str, limit_note: str = "",
                    ctx_mode: str = "window") -> str:
    """群回合投给她的正文 = **标记行 + 该群滚动窗口 + 一行场合规矩**。

    标记行是**闸门（`hindsight_guard`）的兜底信号**：只要正文里有它，这一轮就不会写主库。

    ``ctx_mode="delta"``（2026-10-03，主人拍板）—— 只带"上次被叫醒之后的新消息"，
    重复的部分不再发（上一轮的窗口已经在会话历史里，重发只会浪费 token 并让压缩提前触发）。
    """
    # ⚠️ 这里**不脱敏**（2026-10-03 实测）：标记行是给机器读的——`hindsight_guard` 要从它里面
    # 抠出群号来决定"写哪个群库 / 按哪个 group 标签召回"。原先写成 `mask_gid(gid)`（<GROUP_ID>），
    # 结果群记忆分流**静默失效**（带标记=True 但认不出群）。这行只出现在她自己的回合正文里，
    # 她本来就知道自己在哪个群，没有任何脱敏价值。
    head = TURN_MARKER.format(gid=gid, trigger=TRIGGER_LABEL.get(trigger, trigger))
    parts = [head]
    if limit_note:
        parts.append(limit_note)
    if ctx_mode == "delta":
        parts.append("（下面是上次你被叫醒之后群里新增的消息）")
    else:
        parts.append("（下面是你这个群最近的聊天记录，含你自己说过的话；**末尾一条就是刚触发你的那条**）")
    if window_text:
        parts.append(str(window_text))
    if trigger == "join_request":
        # 这一轮的正文就是「审申请」，不套用「回应末尾那条消息」那套
        parts.append(JOIN_REVIEW_NOTE)
        parts.append(
            "（标记写法与平时一样，主人看不到标记本身。）\n"
            "（这一轮你写的话不进群——想好了直接写标记，别等提示。）")
        return "\n".join(parts)
    # 末尾这段是主人 2026-10-03 亲自改的措辞（他嫌原来"生人档"那套把人设整刻薄了）。
    # 改动请整段替换、别只改半句 —— 排版会跟着乱。
    parts.append(
        "—— 请在这个群里用你自己的口吻回应新消息（要分段就用 `⁂`）。"
        "**要 @ 谁，直接在回复里写 `[CQ:at,qq=QQ号]`**（例如 `[CQ:at,qq=<OWNER_QQ>]`），"
        "发出去时会变成真的 @；**要 @ 全体成员就写 `[CQ:at,qq=all]`**——"
        "它会通知到整个群（主人授权可用，不必再问）。别凭空编号码。"
        "**群里能管的事情**：禁言、解禁、踢人、撤回、精华、全体禁言、头衔、名片、"
        "上/撤管理、发公告、改群名、戳一戳、批或拒入群申请——"
        "**完整写法和限制看 skill `group-admin`**，先 `skill_view('group-admin')` 再动手。"
        "风格就当在朋友群里说话。")
    return "\n".join(parts)


# ── 记忆隔离体检（给适配器/doctor 用）──────────────────────────────────────


def memory_isolation_status(home: Path, *, provider_name: Optional[str] = None,
                            guard_name: str = GUARD_PROVIDER) -> Dict[str, Any]:
    """记忆隔离闸就位了没有（**静态判据**：配置 + 插件文件；运行态证据单独读 state.json）。

    * `config_ok` —— `<home>/config.yaml` 的 `memory.provider` 指向闸门 provider
    * `plugin_ok` —— `<home>/plugins/<guard_name>/__init__.py` 在位
    * `ok`        —— 两者都真 → 适配器才允许唤醒（`group_memory_guard_required` 时）
    * `evidence`  —— 闸门的状态文件（有就读，只有计数；没有也算就绪，不影响判据）
    """
    home = Path(home)
    out: Dict[str, Any] = {
        "guard": guard_name,
        "config_provider": "",
        "expected_provider": str(provider_name or guard_name),
        "config_ok": False,
        "plugin_ok": False,
        "ok": False,
        "evidence": None,
    }
    try:
        import yaml
        cfg = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}
        out["config_provider"] = str(((cfg.get("memory") or {}).get("provider")) or "")
    except Exception as e:  # noqa: BLE001
        out["config_error"] = type(e).__name__
    out["config_ok"] = out["config_provider"] == out["expected_provider"]
    try:
        out["plugin_ok"] = (home / "plugins" / guard_name / "__init__.py").is_file()
    except OSError:
        out["plugin_ok"] = False
    try:
        sp = home / guard_name / "state.json"
        if sp.is_file():
            out["evidence"] = json.loads(sp.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        out["evidence"] = None
    out["ok"] = bool(out["config_ok"] and out["plugin_ok"])
    return out


# ── gated 档：免费前置闸（照麦麦 reply_necessity / qq-bridge wake 概率重写）─────
#
# 分工（关键，别搞反）：
#   * **省钱靠这里** —— 过不了闸的群消息**根本不叫模型**，0 token。
#   * **人味靠模型** —— 过了闸的由她自己判要不要开口（`[SILENT]` 即闭嘴）。
#   把"回不回"整个交给模型＝每条群消息都付钱；把"回不回"整个交给规则＝死鱼。
#   两段式是这两者唯一的折中。

_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2b00-\u2bff\ufe0f\u200d]")
_SPACE_RE = re.compile(r"\s+")

#: 疑问/求助/点话题的信号词 —— 命中即"有话可说"
_ASK_HINTS: Tuple[str, ...] = (
    "?", "？", "吗", "么", "怎么", "咋", "为什么", "为啥", "哪", "什么", "谁",
    "帮", "求", "能不能", "可不可以", "有没有", "是不是", "该不该", "推荐",
)


def visible_len(text: str) -> int:
    """去掉 emoji / 零宽 / 空白后的可见字符数（表情不算"说了话"）。"""
    return len(_SPACE_RE.sub("", _EMOJI_RE.sub("", text or "")).strip())


def silence_of(text: str) -> bool:
    """整条回复是否等于"主动闭嘴"标记（允许前后空白/换行）。"""
    return (text or "").strip().strip("\u200b").strip() == SILENCE_MARKER


def _fp(text: str) -> str:
    return _SPACE_RE.sub("", _EMOJI_RE.sub("", (text or "").lower()))


class GateState:
    """群级门控状态（**纯数据**，由适配器按群维护、喂给 `gate_score`）。

    - ``last_self_at``：她上次在这个群说话的时间 → 闲置压力（麦麦 `idle_pressure_bonus`）
    - ``_self_times``：最近她自己发言的时间 → 自说率（麦麦 `recent_self_ratio` 惩罚）
    - ``_recent``：最近群消息指纹 → 复读/刷屏去重
    """

    def __init__(self, recent_max: int = 12, self_max: int = 12) -> None:
        self._recent: deque = deque(maxlen=max(2, int(recent_max)))
        self._self_times: deque = deque(maxlen=max(2, int(self_max)))
        self.last_self_at: float = 0.0
        #: 对话态截止时间（epoch 秒）。> now 表示"她正在跟人聊"→ 不必 @ 也能被叫醒。
        self.conv_until: float = 0.0

    def note_conversation(self, *, now: Optional[float] = None,
                          window_s: float = CONV_WINDOW_S) -> None:
        """进入/续期对话态（她刚被叫起来、或刚在这个群说过话时调用）。

        窗口内每条群消息都能唤醒她 —— 这是"聊到一半不需要每次 @"的机制：
        潜水态靠免费闸筛（省钱），对话态直接放行（连贯），回不回仍由她自己 `[SILENT]` 决定。
        """
        t = float(now if now is not None else time.time())
        self.conv_until = max(self.conv_until, t + float(window_s))

    def in_conversation(self, *, now: Optional[float] = None) -> bool:
        t = float(now if now is not None else time.time())
        return bool(self.conv_until) and t < self.conv_until

    def note_incoming(self, text: str, *, now: Optional[float] = None) -> None:
        """每条群消息都喂进来（**包括不唤醒的**），复读判定才准。"""
        self._recent.append(_fp(text))

    def note_self_spoke(self, *, now: Optional[float] = None) -> None:
        t = float(now if now is not None else time.time())
        self.last_self_at = t
        self._self_times.append(t)

    def dedup_ratio(self, text: str) -> float:
        """这条与最近群消息里"最像的那条"的相似度（0~1，字符集 Jaccard，够用且便宜）。"""
        cur = set(_fp(text))
        if not cur:
            return 0.0
        best = 0.0
        for prev in self._recent:
            pv = set(prev)
            if not pv:
                continue
            inter = len(cur & pv)
            union = len(cur | pv)
            if union:
                best = max(best, inter / union)
        return best

    def self_ratio(self, *, now: Optional[float] = None, window_s: float = 600.0) -> float:
        """最近窗口里"她说话"占的份额（0~1）。窗口内她没说 → 0。"""
        t = float(now if now is not None else time.time())
        if not self._self_times:
            return 0.0
        n_self = sum(1 for x in self._self_times if t - x <= window_s)
        if n_self <= 0:
            return 0.0
        # 分母用「她说的条数 + 群消息条数的粗估」：群消息队列就是 _recent
        n_group = max(1, len(self._recent))
        return min(1.0, n_self / float(n_self + n_group))


def gate_score(event: Dict[str, Any], *, self_id: str = "", alias_names: Sequence[str] = DEFAULT_ALIAS_NAMES,
               state: Optional[GateState] = None, now: Optional[float] = None,
               threshold: float = GATE_DEFAULT_THRESHOLD,
               idle_window_s: float = 1800.0, idle_bonus_max: float = 0.25,
               self_ratio_soft: float = 0.5, self_ratio_weight: float = 0.6,
               conv_bonus: float = CONV_BONUS,
               text: Optional[str] = None) -> Tuple[float, str]:
    """免费前置闸：给这条群消息打一个 0~1 的"值不值得叫她"分。**不碰 LLM。**

    规则（照麦麦：话题压力 + 闲置压力 − 自己话多）：
      +0.35 疑问/求助信号词         （有话要答）
      +0.20 可见长度 ≥ 6            （有信息量）
      +0.10 可见长度 ≥ 15           （长消息）
      −0.40 纯表情/纯符号           （没话说）
      −0.30 可见长度 ≤ 2            （"哈哈""嗯"）
      −0.25×相似度 复读/刷屏        （重复刷屏）
      +闲置压力（0~idle_bonus_max） （她越久没说话越愿意接）
      −自说率惩罚（占比超阈值后）   （她刚说过话就别抢）
    """
    t = float(now if now is not None else time.time())
    text = _text(event) if text is None else text
    if not text.strip():
        return 0.0, "无可读正文"

    score = 0.0
    why: List[str] = []

    if any(h in text for h in _ASK_HINTS):
        score += 0.35
        why.append("+0.35 疑问/求助")
    n = visible_len(text)
    if n >= 6:
        score += 0.20
        why.append("+0.20 有信息量")
    if n >= 15:
        score += 0.10
        why.append("+0.10 长消息")
    if n == 0 or _EMOJI_RE.sub("", text).strip() == "":
        score -= 0.40
        why.append("-0.40 纯表情/符号")
    if n <= 2:
        score -= 0.30
        why.append("-0.30 过短")

    if state is not None:
        if state.in_conversation(now=t):
            score += conv_bonus
            why.append("+%.2f 对话态(%.0fs 内她说过话)" % (conv_bonus, max(0.0, state.conv_until - t)))
        d = state.dedup_ratio(text)
        if d >= 0.6:
            score -= 0.25 * d
            why.append("-%.2f 复读/刷屏" % (0.25 * d))
        if state.last_self_at:
            idle = min(1.0, max(0.0, (t - state.last_self_at) / float(idle_window_s)))
        else:
            idle = 1.0
        bonus = idle * idle_bonus_max
        if bonus > 0.01:
            score += bonus
            why.append("+%.2f 闲置压力" % bonus)
        sr = state.self_ratio(now=t)
        pen = max(0.0, sr - self_ratio_soft) * self_ratio_weight
        if pen > 0.01:
            score -= pen
            why.append("-%.2f 自说率%.0f%%" % (pen, sr * 100))
    else:
        why.append("(无状态：闲置/自说率/复读三项免评)")

    score = max(0.0, min(1.0, score))
    return score, "; ".join(why)
