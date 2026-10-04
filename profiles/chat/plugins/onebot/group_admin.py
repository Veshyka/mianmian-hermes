"""QQ 群管理决策层（**纯逻辑、零 I/O**）：入群欢迎 / 加群审批 / 关键词回复 /
违禁词 / 防撤回 / 管理命令 / 发言统计。

单独成模块的理由与 `group_wake.py` 一样：判定逻辑要能**离线单测**
（`tests/check_group_admin.py`），不埋进适配器。

## 边界（硬约束，别越线）

这个模块**只做决策**：吃一个 OneBot v11 事件字典，吐一串 `Intent`（要做的事）。
它**不开网络、不读写文件、不记日志、不 import 非标准库**，也不直接发消息 ——
真正发请求/撤回/禁言的活全部由适配器按 `Intent.action` + `Intent.params` 去干。

复用的协议解析全部来自 `onebot_proto`：`group_id` / `sender_id` / `sender_name` /
`notice_type` / `request_type` / `classify_event` / `extract_text` /
`extract_window_text` / `reply_message_id` / `build_action`。

## 默认值一律保守

群管理会**真禁言、真踢人、真撤回**，所以 `DEFAULT_CONFIG` 里凡是「动手」的能力
默认都是关的（入群欢迎关、退群提示关、违禁词关、防撤回关、加群审批走 `manual`）。
开了也尽量先「只提醒不动手」。全量默认值见 `DEFAULT_CONFIG` 注释。

## 适配器怎么接

```python
ga = GroupAdmin(extra.get("group_admin"), owner_ids=owner_ids, self_id=self_id)
if proto.classify_event(event) == "message" and proto.is_group(event):
    ga.note_message(event)          # 每条群消息都要喂（防撤回/违禁词要回溯）
for it in ga.handle(event):
    if it.action == ACTION_INTERNAL:
        ...                          # 不发请求，只把 it.note 追加进 LLM 上下文
    else:
        send_action(it.action, dict(it.params))   # echo/self_id 由适配器补
```

`note_message` 与 `handle` 分开：`handle` **不**隐式写缓存（免得适配器两条链路都调时重复计数）。
"""

from __future__ import annotations

import re
import time
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import onebot_proto as _proto

#: 只写进上下文/日志、**不发任何 OneBot 请求**的 Intent.action。
#: 适配器见到它不许当成 action 名去发帧（发了会得到「未知 action」错误）。
ACTION_INTERNAL = "internal_noop"

#: 每群消息缓存默认容量（防撤回要回查原文，但不能无限长）。
DEFAULT_CACHE_MAX = 200


# ── 默认配置（**唯一真相表**：默认保守 = 默认不动手）───────────────────────
#
# 语义：`groups: []` = 该能力对所有群生效；填了群号 = 只在这些群生效。
# 想「只在某几个群动手」，就必须写 groups。

DEFAULT_CONFIG: Dict[str, Any] = {
    # 总开关：关掉 → handle() 永远返回 []（连通知都不产出）。
    "enabled": True,
    # 每群消息缓存条数上限（防撤回/回溯用）。
    "cache_max": DEFAULT_CACHE_MAX,
    # 入群欢迎：默认关（入群就自动说话是社交噪音，默认不做）。
    "welcome": {
        "enabled": False,
        "text": "{at} 欢迎加入本群～",
        "groups": [],
        "cooldown_s": 3.0,          # 同群连续入群的最小间隔（防踢了又拉刷屏）
    },
    # 退群提示：默认关（有人进进出出很吵）。
    "leave": {
        "enabled": False,
        "text": "{name} 退群了",
        "groups": [],
    },
    # 加群审批：默认 manual（只通知主人，绝不自动放人进来）。
    "request": {
        "policy": "manual",              # auto_approve / auto_reject / manual
        "reject_reason": "抱歉，本群暂不接受申请",   # 拒绝理由**可配**
        "approve_comment": "",           # 通过时附带的话（空 = 不带）
        "notify_owner": True,            # manual 时给主人发通知
        "blacklist_keywords": [],        # 附言命中即拒（任何策略下都优先）
        "whitelist_keywords": [],        # 非空时，auto_approve 要求附言必须命中其一
    },
    # 关键词回复：默认空表（没配规则 = 不回话）。
    # 每条规则：{"pattern":.., "reply":.., "match":"exact|contains|regex",
    #            "groups":[], "case_sensitive":False, "prefix":False}
    "keywords": [],
    "keyword_max_per_min": 10,           # 每群每分钟最多回几次（防被刷爆）
    # 违禁词：默认关（撤回/禁言是破坏性动作，默认不碰）。
    "banned_words": {
        "enabled": False,
        "words": [],                     # 子串匹配（中文不需要分词）
        "regex": [],                     # 正则匹配（写错的正则会被静默跳过，不抛异常）
        "action": "delete",              # delete / delete_ban（delete_ban 才禁言）
        "ban_seconds": 300,
        "warn_text": "",                 # 非空 → 额外发一条警告语
        "exempt_owner": True,            # 主人豁免
        "groups": [],
    },
    # 防撤回：默认关。
    "anti_recall": {
        "enabled": False,
        "text": "{name} 撤回了一条消息：{content}",
        "groups": [],
        "skip_self": True,               # 她自己的消息被撤回不报（按 self_id 判）
    },
    # 管理命令：只有 owner_ids 里的人能用；白名单群为空 = 不限群（主人是最高权限）。
    "commands": {
        "enabled": True,
        "prefix": "/",
        "groups": [],                    # 白名单群；非空 = 只在列出的群里认命令
        "deny_note": True,               # 越权命令产出拒绝提示（内部 Intent，不公开呛人）
        "default_ban_seconds": 300,
        "max_ban_seconds": 2592000,      # 封顶 30 天，防手滑写 99999999
    },
    # 发言统计：内存计数器（不落盘，进程重启即清零）。
    "stats": {
        "enabled": True,
        "top_n": 3,
        "max_users": 1000,               # 每群最多记多少人（防大群撑爆内存）
    },
}

#: 加群审批策略取值
POLICIES = ("auto_approve", "auto_reject", "manual", "agent")
#: `agent`（2026-10-03 新增）：群管理**不动手、也不通知主人** —— 加群申请交给适配器起的
#: 「审申请回合」，由她自己写 `[同意入群:…]` / `[拒绝入群:…]` 决定。主人原话：
#: 「我想让她能直接自己看到申请并看是不是通过…我更倾向于宽松审核」。

#: 命令别名表：别名 → 规范名（中文别名与英文等价）
_COMMAND_ALIASES: Dict[str, str] = {
    "ban": "ban", "禁言": "ban",
    "unban": "unban", "解禁": "unban",
    "kick": "kick", "踢": "kick", "踢人": "kick",
    "card": "card", "名片": "card", "改名片": "card",
    "recall": "recall", "撤回": "recall", "撤": "recall",
    "essence": "essence", "精华": "essence", "设精": "essence",
    "unessence": "unessence", "取消精华": "unessence",
    "stats": "stats", "统计": "stats",
}

_AT_IN_TEXT_RE = re.compile(r"\[@(\d+)\]")
_CQ_AT_RE = re.compile(r"\[CQ:at,qq=(\d+)")


# ── 事件 → Intent ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Intent:
    """一件「要做的事」。**只是描述**，执行在适配器。"""

    action: str                       # OneBot action 名，如 "set_group_ban"；或 ACTION_INTERNAL
    params: dict                      # action 参数（不带 echo/self_id，适配器补）
    kind: str                         # 人读分类，进日志，如 "welcome"/"anti_recall"
    note: str = ""                    # 非空 → 适配器会把这句话追加给聊天门的 LLM 上下文


# ── 小工具（纯函数）────────────────────────────────────────────────────────


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_int(value: Any, default: int, *, minimum: int = 0) -> int:
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float, *, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(value))
    except (TypeError, ValueError):
        return default


def _as_str_list(value: Any) -> List[str]:
    """字符串列表（逗号/顿号分隔的单串也认）；其它一律当空表。"""
    if value is None:
        return []
    if isinstance(value, str):
        parts = value.replace("，", ",").replace("、", ",").split(",")
        return [p.strip() for p in parts if p.strip()]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(x).strip() for x in value if str(x).strip()]
    return []


def merge_config(user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """把用户配置叠到 `DEFAULT_CONFIG` 上（递归合并 dict，其它整段覆盖）。"""
    def _merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(base)
        for k, v in (over or {}).items():
            if k not in out:
                out[k] = v                      # 多余键照留（调用方自己看）
            elif isinstance(out[k], dict) and isinstance(v, dict):
                out[k] = _merge(out[k], v)
            else:
                out[k] = v
        return out

    if not isinstance(user, dict):
        return {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULT_CONFIG.items()}
    merged = _merge(DEFAULT_CONFIG, user)
    if str(merged.get("request", {}).get("policy") or "").strip().lower() not in POLICIES:
        merged["request"]["policy"] = "manual"      # 非法策略 → 最保守
    return merged


def _group_allows(groups: Sequence[str], gid: str) -> bool:
    """空名单 = 所有群；非空 = 只认列出的群号。"""
    if not groups:
        return True
    return str(gid) in {str(x) for x in groups}


def _num(value: Any):
    s = str(value)
    return int(s) if s.lstrip("-").isdigit() else s


def _event_time(event: Dict[str, Any]) -> float:
    """事件自带时间戳优先（OneBot 的 `time` 是秒）—— 可测且省一次系统调用。"""
    try:
        t = event.get("time")
        if t is not None:
            return float(t)
    except (TypeError, ValueError):
        pass
    return time.time()


def _parse_uid(arg: str) -> str:
    """从命令参数里抠出 QQ 号：`123` / `[@123]` / `[CQ:at,qq=123]` 都认。"""
    s = str(arg or "").strip()
    m = _AT_IN_TEXT_RE.fullmatch(s) or _CQ_AT_RE.match(s)
    if m:
        return m.group(1)
    return s if s.isdigit() else ""


def _fill(template: str, **kw: Any) -> str:
    txt = str(template or "")
    for k, v in kw.items():
        txt = txt.replace("{%s}" % k, str(v))
    return txt


# ── 发言统计（内存计数，纯数据）────────────────────────────────────────────


class GroupStats:
    """每群发言计数：条数 / 人数 / 最活跃者。**不落盘**，进程重启即清零。"""

    def __init__(self, *, enabled: bool = True, top_n: int = 3, max_users: int = 1000) -> None:
        self.enabled = bool(enabled)
        self.top_n = max(1, int(top_n))
        self.max_users = max(1, int(max_users))
        self._count: Dict[str, Counter] = {}
        self._names: Dict[str, Dict[str, str]] = {}
        self._total: Dict[str, int] = {}
        self._last: Dict[str, float] = {}
        #: 因超过 max_users 而**没被计入**的新人次数（可观测，不静默）
        self.dropped_users = 0

    def note(self, gid: str, uid: str, name: str, *, now: Optional[float] = None) -> None:
        if not self.enabled or not gid or not uid:
            return
        gid = str(gid)
        c = self._count.setdefault(gid, Counter())
        if uid not in c and len(c) >= self.max_users:
            self.dropped_users += 1
            return
        c[uid] += 1
        self._names.setdefault(gid, {})[uid] = str(name or uid)
        self._total[gid] = self._total.get(gid, 0) + 1
        if now is not None:
            self._last[gid] = float(now)

    def snapshot(self, gid: str) -> Dict[str, Any]:
        gid = str(gid)
        c = self._count.get(gid) or Counter()
        names = self._names.get(gid) or {}
        top = [(names.get(u, u), n) for u, n in c.most_common(self.top_n)]
        return {
            "group_id": gid,
            "messages": self._total.get(gid, 0),   # 总条数
            "users": len(c),                       # 发言人数
            "top": top,                            # [(名字, 条数), ...] 最活跃在前
            "top_user": top[0][0] if top else "",
            "top_count": top[0][1] if top else 0,
            "enabled": self.enabled,
        }


# ── 消息缓存（防撤回/回溯用，容量有上限）────────────────────────────────────


class MessageCache:
    """每群一份定长缓存：按 `message_id` 去重，超容量丢最旧的。"""

    def __init__(self, max_per_group: int = DEFAULT_CACHE_MAX) -> None:
        self.max_per_group = max(1, int(max_per_group))
        self._by_group: Dict[str, "OrderedDict[str, Dict[str, Any]]"] = {}
        self.evicted = 0

    def put(self, gid: str, mid: str, record: Dict[str, Any]) -> None:
        gid, mid = str(gid), str(mid)
        if not gid or not mid:
            return
        dq = self._by_group.setdefault(gid, OrderedDict())
        if mid in dq:
            dq.move_to_end(mid)
        dq[mid] = record
        while len(dq) > self.max_per_group:
            dq.popitem(last=False)
            self.evicted += 1

    def get(self, gid: str, mid: str) -> Optional[Dict[str, Any]]:
        return (self._by_group.get(str(gid)) or {}).get(str(mid))

    def last_mid_by_user(self, gid: str, uid: Any) -> Optional[str]:
        """该用户在这个群里**最近一条**还在缓存里的 message_id（没有 → None）。

        用途：她写 `[撤回:QQ号]` 时，靠它把「这个人」翻成协议端要的 message_id ——
        她的上下文里没有消息 id（窗口只给时间/名字/号码/正文），不能让她去记 id。
        """
        uid = str(uid or "").strip()
        if not uid:
            return None
        dq = self._by_group.get(str(gid)) or {}
        for mid in reversed(dq.keys()):
            rec = dq.get(mid) or {}
            if str(rec.get("user_id") or rec.get("uid") or "") == uid:
                return str(mid)
        return None

    def size(self, gid: str) -> int:
        return len(self._by_group.get(str(gid)) or {})

    def groups(self) -> List[str]:
        return list(self._by_group)


# ── 主体 ───────────────────────────────────────────────────────────────────


class GroupAdmin:
    """群管理决策器。**纯逻辑**：同一份输入 + 同一份内存状态 → 同一个输出。"""

    def __init__(self, cfg: Optional[dict] = None, *, owner_ids: Iterable[str] = (),
                 self_id: str = "") -> None:
        self.cfg = merge_config(cfg)
        self.owner_ids: Tuple[str, ...] = tuple(
            str(x).strip() for x in (owner_ids or ()) if str(x).strip())
        self.self_id = str(self_id or "")
        self.cache = MessageCache(_as_int(self.cfg.get("cache_max"), DEFAULT_CACHE_MAX, minimum=1))
        st = self.cfg.get("stats") or {}
        self._stats = GroupStats(enabled=_truthy(st.get("enabled"), True),
                                 top_n=_as_int(st.get("top_n"), 3, minimum=1),
                                 max_users=_as_int(st.get("max_users"), 1000, minimum=1))
        self._kw_times: Dict[str, deque] = {}
        self._welcome_at: Dict[str, float] = {}
        self._kw_re = self._compile_keywords()
        self._ban_re = self._compile_banned()
        #: 最近一次「收到事件但没动手」的原因（可观测，**不静默丢**）
        self.last_skip = ""
        #: 累计产出过多少 Intent（回归用：通知不许被吞成空表）
        self.emitted = 0

    # ── 编译期（构造时一次性）──────────────────────────────────────────────

    def _compile_keywords(self) -> List[Dict[str, Any]]:
        """关键词规则编译。**正则写错 = 这条规则作废**（不是整模块崩）。"""
        out: List[Dict[str, Any]] = []
        for raw in self.cfg.get("keywords") or []:
            if not isinstance(raw, dict):
                continue
            pattern = str(raw.get("pattern") or "").strip()
            reply = str(raw.get("reply") or "").strip()
            if not pattern or not reply:
                continue
            match = str(raw.get("match") or "contains").strip().lower()
            if match not in ("exact", "contains", "regex"):
                match = "contains"
            rule: Dict[str, Any] = {
                "pattern": pattern, "reply": reply, "match": match,
                "groups": _as_str_list(raw.get("groups")),
                "case_sensitive": _truthy(raw.get("case_sensitive"), False),
                "prefix": _truthy(raw.get("prefix"), False),
                "regex": None,
            }
            if match == "regex":
                try:
                    rule["regex"] = re.compile(pattern, 0 if rule["case_sensitive"] else re.I)
                except re.error:
                    continue            # 坏正则 → 丢弃这条规则（不抛异常给适配器）
            out.append(rule)
        return out

    def _compile_banned(self) -> List[Any]:
        out: List[Any] = []
        for raw in (self.cfg.get("banned_words") or {}).get("regex") or []:
            try:
                out.append(re.compile(str(raw)))
            except re.error:
                continue
        return out

    # ── 入口一：消息缓存 ───────────────────────────────────────────────────

    def note_message(self, event: Dict[str, Any]) -> None:
        """每条群消息都喂一次。**只为缓存/统计**，不产出任何 Intent。

        * 她自己发的（`self_id`）不入缓存、不计统计（不然「她自己撤回自己」会被当群友撤回）
        * 只认群消息；私聊/通知事件直接忽略
        """
        if not isinstance(event, dict) or _proto.classify_event(event) != "message":
            return
        if not _proto.is_group(event):
            return
        gid = _proto.group_id(event)
        if not gid:
            return
        uid = _proto.sender_id(event)
        if not uid or (self.self_id and uid == self.self_id):
            return
        if uid == _proto.QQ_GUANJIA_ID:
            return
        name = _proto.sender_name(event)
        text = _proto.extract_window_text(event)
        now = _event_time(event)
        mid = event.get("message_id")
        if mid is not None:
            self.cache.put(gid, str(mid), {
                "message_id": str(mid), "group_id": gid, "user_id": uid, "name": name,
                "text": text, "time": now, "reply_to": _proto.reply_message_id(event) or "",
            })
        self._stats.note(gid, uid, name, now=now)

    def stats(self, gid: str) -> Dict[str, Any]:
        """某群发言统计：`messages` 条数 / `users` 人数 / `top` 最活跃者列表。"""
        return self._stats.snapshot(gid)

    # ── 入口二：主决策 ─────────────────────────────────────────────────────

    def handle(self, event: Dict[str, Any]) -> List[Intent]:
        """主入口。**纯函数式**：返回要执行的动作列表（可为空）。"""
        self.last_skip = ""
        if not isinstance(event, dict) or not _truthy(self.cfg.get("enabled"), True):
            return self._skip("总开关关闭或事件非法")
        kind = _proto.classify_event(event)
        if kind == "notice":
            out = self._handle_notice(event)
        elif kind == "request":
            out = self._handle_request(event)
        elif kind == "message":
            out = self._handle_message(event)
        else:
            out = self._skip("非群管理事件：%s" % kind)
        self.emitted += len(out)
        return out

    def _skip(self, why: str) -> List[Intent]:
        self.last_skip = why
        return []

    # ── 通知类（入群/退群/撤回）────────────────────────────────────────────

    def _handle_notice(self, event: Dict[str, Any]) -> List[Intent]:
        nt = _proto.notice_type(event)
        if nt == "group_increase":
            return self._on_group_increase(event)
        if nt == "group_decrease":
            return self._on_group_decrease(event)
        if nt == "group_recall":
            return self._on_group_recall(event)
        return self._skip("未处理的通知类型：%s" % (nt or "空"))

    def _on_group_increase(self, event: Dict[str, Any]) -> List[Intent]:
        c = self.cfg.get("welcome") or {}
        gid = _proto.group_id(event)
        if not _truthy(c.get("enabled")):
            return self._skip("入群欢迎未开启")
        if not _group_allows(_as_str_list(c.get("groups")), gid):
            return self._skip("该群不在欢迎名单")
        uid = _proto.sender_id(event)       # notice 里 user_id 就是新人
        if not uid:
            return self._skip("入群事件没有 user_id")
        now = _event_time(event)
        cd = _as_float(c.get("cooldown_s"), 3.0)
        prev = self._welcome_at.get(gid)
        if prev is not None and cd > 0 and (now - prev) < cd:
            return self._skip("欢迎冷却中（%.1fs 内已欢迎过）" % cd)
        self._welcome_at[gid] = now
        text = _fill(c.get("text") or "{at} 欢迎加入本群～",
                     at="[CQ:at,qq=%s]" % uid, name=_proto.sender_name(event), group=gid)
        return [self._send_group(gid, text, "welcome", "新人入群欢迎")]

    def _on_group_decrease(self, event: Dict[str, Any]) -> List[Intent]:
        c = self.cfg.get("leave") or {}
        gid = _proto.group_id(event)
        if not _truthy(c.get("enabled")):
            return self._skip("退群提示未开启")
        if not _group_allows(_as_str_list(c.get("groups")), gid):
            return self._skip("该群不在退群提示名单")
        uid = _proto.sender_id(event)
        if self.self_id and uid == self.self_id:
            return self._skip("是自己被移出/退出，不提示")
        text = _fill(c.get("text") or "{name} 退群了",
                     name=_proto.sender_name(event), at="[CQ:at,qq=%s]" % uid, group=gid)
        return [self._send_group(gid, text, "leave", "有人退群：%s" % text)]

    def _on_group_recall(self, event: Dict[str, Any]) -> List[Intent]:
        c = self.cfg.get("anti_recall") or {}
        gid = _proto.group_id(event)
        if not _truthy(c.get("enabled")):
            return self._skip("防撤回未开启")
        if not _group_allows(_as_str_list(c.get("groups")), gid):
            return self._skip("该群不在防撤回名单")
        uid = _proto.sender_id(event)
        if _truthy(c.get("skip_self"), True) and self.self_id and uid == self.self_id:
            return self._skip("自己撤回自己的消息，不报")
        mid = event.get("message_id")
        rec = self.cache.get(gid, str(mid)) if mid is not None else None
        if rec is None:
            # 缓存里没有原文（没开之前的消息 / 超出容量）→ **仍然留一条痕迹**，别静默
            return [self._internal("anti_recall",
                                   "有人撤回了消息（message_id=%s），但缓存里没有原文，无法还原内容。" % mid)]
        text = _fill(c.get("text") or "{name} 撤回了一条消息：{content}",
                     name=rec.get("name") or rec.get("user_id") or "有人",
                     content=rec.get("text") or "[原消息内容为空]",
                     group=gid)
        return [self._send_group(gid, text, "anti_recall",
                                 "防撤回补发：%s：%s" % (rec.get("name"), rec.get("text")))]

    # ── 请求类（加群审批）──────────────────────────────────────────────────

    def _handle_request(self, event: Dict[str, Any]) -> List[Intent]:
        if _proto.request_type(event) != "group":
            return self._skip("非加群请求：%s" % (_proto.request_type(event) or "空"))
        c = self.cfg.get("request") or {}
        gid = _proto.group_id(event)
        uid = str(event.get("user_id") or "")
        if not uid:
            return self._skip("加群请求没有 user_id")
        comment = str(event.get("comment") or "")
        flag = event.get("flag") or ""
        policy = str(c.get("policy") or "manual").strip().lower()
        if policy not in POLICIES:
            policy = "manual"

        black = _as_str_list(c.get("blacklist_keywords"))
        hit_black = [w for w in black if w in comment]
        if hit_black:
            policy = "auto_reject"                # 黑名单优先，任何策略都压得住
            why = "附言命中黑名单词：%s" % "、".join(hit_black)
        else:
            why = ""

        if policy == "auto_approve":
            white = _as_str_list(c.get("whitelist_keywords"))
            if white and not any(w in comment for w in white):
                note = ("加群申请待人工处理：%s（附言没命中白名单词，没自动通过）｜附言：%s"
                        % (uid, comment or "无"))
                return self._owner_notice(note, "request_manual")
            out = [Intent("set_group_add_request",
                          {"flag": flag, "sub_type": "add", "approve": True,
                           "comment": str(c.get("approve_comment") or "")},
                          "request_approve", "已自动通过 %s 的加群申请" % uid)]
            return out
        if policy == "auto_reject":
            out = [Intent("set_group_add_request",
                          {"flag": flag, "sub_type": "add", "approve": False,
                           "reason": str(c.get("reject_reason") or "")},
                          "request_reject", "已拒绝 %s 的加群申请（%s）" % (uid, why or "策略 auto_reject"))]
            return out
        if policy == "agent":
            # 交给她自己审：不起动作、不给主人发私聊（主人嫌被提醒烦）
            return self._skip("入群申请交给 AI 自己审（policy=agent）")
        # manual：**只通知不动手**
        note = ("加群申请待处理：%s 申请加入群 %s（附言：%s）。%s"
                % (uid, gid or "?", comment or "无", why or "按配置需要你手动决定，我没有替他动手。"))
        return self._owner_notice(note, "request_manual")

    # ── 消息类（命令 / 违禁词 / 关键词）────────────────────────────────────

    def _handle_message(self, event: Dict[str, Any]) -> List[Intent]:
        if not _proto.is_group(event):
            return self._skip("私聊消息不归群管理管")
        gid = _proto.group_id(event)
        uid = _proto.sender_id(event)
        if uid == _proto.QQ_GUANJIA_ID:
            return self._skip("QQ 管家噪声")
        text = _proto.extract_text(event)

        cmd = self._try_command(event, gid, uid, text)
        if cmd is not None:
            return cmd

        banned = self._check_banned(event, gid, uid, text)
        if banned:
            return banned           # 已动手处理，同一条不再走关键词回复（免得刷屏）
        return self._check_keywords(event, gid, text)

    def _check_banned(self, event: Dict[str, Any], gid: str, uid: str, text: str) -> List[Intent]:
        c = self.cfg.get("banned_words") or {}
        if not _truthy(c.get("enabled")):
            return self._skip("违禁词未开启")
        if not text:
            return self._skip("空正文，不查违禁词")
        if not _group_allows(_as_str_list(c.get("groups")), gid):
            return self._skip("该群不在违禁词名单")
        if _truthy(c.get("exempt_owner"), True) and self._is_owner(uid):
            return self._skip("主人豁免违禁词")
        words = _as_str_list(c.get("words"))
        hit = [w for w in words if w in text]
        if not hit:
            for rx in self._ban_re:
                m = rx.search(text)
                if m:
                    hit.append(m.group(0) or rx.pattern)
                    break
        if not hit:
            return self._skip("没命中违禁词")
        mid = event.get("message_id")
        out: List[Intent] = []
        if mid is not None:
            out.append(Intent("delete_msg", {"message_id": _num(mid)}, "banned_delete",
                              "已撤回一条命中违禁词的消息（%s）：%s" % ("、".join(hit), text)))
        else:
            out.append(self._internal("banned_delete",
                                      "命中违禁词但没有 message_id，撤不回来：%s" % "、".join(hit)))
        if str(c.get("action") or "delete") == "delete_ban" and uid:
            secs = min(_as_int(c.get("ban_seconds"), 300, minimum=1),
                       _as_int((self.cfg.get("commands") or {}).get("max_ban_seconds"),
                               2592000, minimum=1))
            out.append(Intent("set_group_ban",
                              {"group_id": _num(gid), "user_id": _num(uid), "duration": secs},
                              "banned_ban", "已禁言 %s %d 秒（违禁词）" % (uid, secs)))
        warn = str(c.get("warn_text") or "")
        if warn:
            out.append(self._send_group(gid, _fill(warn, at="[CQ:at,qq=%s]" % uid,
                                                   name=_proto.sender_name(event), group=gid),
                                        "banned_warn", "已发违禁词警告"))
        return out

    def _check_keywords(self, event: Dict[str, Any], gid: str, text: str) -> List[Intent]:
        if not self._kw_re or not text:
            return self._skip("没有关键词规则或正文为空")
        for rule in self._kw_re:
            if not _group_allows(rule["groups"], gid):
                continue
            if not self._kw_hit(rule, text):
                continue
            if not self._kw_allow(gid, _event_time(event)):
                return self._skip("关键词回复限流中（每分钟上限）")
            return [self._send_group(gid, rule["reply"], "keyword_reply",
                                     "关键词命中「%s」→ 已回复固定话术" % rule["pattern"])]
        return self._skip("没命中关键词")

    def _kw_hit(self, rule: Dict[str, Any], text: str) -> bool:
        pat = rule["pattern"]
        hay = text if rule["case_sensitive"] else text.lower()
        needle = pat if rule["case_sensitive"] else pat.lower()
        if rule["match"] == "exact":
            return hay.strip() == needle.strip()
        if rule["match"] == "regex":
            rx = rule["regex"]
            return bool(rx and rx.search(text))
        if rule["prefix"]:
            return hay.startswith(needle)
        return needle in hay

    def _kw_allow(self, gid: str, now: float) -> bool:
        cap = _as_int(self.cfg.get("keyword_max_per_min"), 10, minimum=1)
        dq = self._kw_times.setdefault(str(gid), deque(maxlen=max(8, cap + 1)))
        while dq and now - dq[0] > 60.0:
            dq.popleft()
        if len(dq) >= cap:
            return False
        dq.append(now)
        return True

    # ── 管理命令 ───────────────────────────────────────────────────────────

    def _try_command(self, event: Dict[str, Any], gid: str, uid: str,
                     text: str) -> Optional[List[Intent]]:
        """返回 None = 这不是命令（继续走关键词/违禁词）；返回列表 = 命令分支已定论。"""
        c = self.cfg.get("commands") or {}
        if not _truthy(c.get("enabled"), True):
            return None
        prefix = str(c.get("prefix") or "/")
        if not prefix or not text.startswith(prefix):
            return None
        body = text[len(prefix):].strip()
        if not body:
            return None
        parts = body.split()
        name = _COMMAND_ALIASES.get(parts[0].lower())
        if not name:
            return None                        # 未知命令 → 当普通消息，继续走别的判定
        args = parts[1:]

        deny = self._permission_deny(gid, uid, c)
        if deny:
            if _truthy(c.get("deny_note"), True):
                return [self._internal("cmd_denied", deny)]
            return []

        intents, err = self._run_command(name, event, gid, uid, args)
        if err:
            return [self._internal("cmd_error", err)]
        return intents or []

    def _permission_deny(self, gid: str, uid: str, c: Dict[str, Any]) -> str:
        """**自己写的权限判定**：主人 + 白名单群，两条都要满足。"""
        if not self._is_owner(uid):
            return ("拒绝执行管理命令：%s 不在主人名单里（此命令只有主人能用，已忽略）。"
                    % (uid or "未知用户"))
        groups = _as_str_list(c.get("groups"))
        if groups and not _group_allows(groups, gid):
            return ("拒绝执行管理命令：群 %s 不在命令白名单里（已忽略）。" % gid)
        return ""

    def _is_owner(self, uid: str) -> bool:
        return bool(uid) and str(uid) in self.owner_ids

    def _run_command(self, name: str, event: Dict[str, Any], gid: str, uid: str,
                     args: Sequence[str]) -> Tuple[List[Intent], str]:
        c = self.cfg.get("commands") or {}
        if name == "ban":
            if not args:
                return [], "用法：%sban <QQ> [秒数]" % str(c.get("prefix") or "/")
            tid = _parse_uid(args[0])
            if not tid:
                return [], "QQ 号不对：%r" % args[0]
            secs = _as_int(args[1], _as_int(c.get("default_ban_seconds"), 300, minimum=1),
                           minimum=1) if len(args) > 1 else _as_int(c.get("default_ban_seconds"), 300, minimum=1)
            secs = min(secs, _as_int(c.get("max_ban_seconds"), 2592000, minimum=1))
            return [Intent("set_group_ban",
                           {"group_id": _num(gid), "user_id": _num(tid), "duration": secs},
                           "cmd_ban", "已禁言 %s %d 秒" % (tid, secs))], ""
        if name == "unban":
            if not args:
                return [], "用法：解禁 <QQ>"
            tid = _parse_uid(args[0])
            if not tid:
                return [], "QQ 号不对：%r" % args[0]
            return [Intent("set_group_ban",
                           {"group_id": _num(gid), "user_id": _num(tid), "duration": 0},
                           "cmd_unban", "已解除 %s 的禁言" % tid)], ""
        if name == "kick":
            if not args:
                return [], "用法：踢 <QQ> [拒再进 1/0]"
            tid = _parse_uid(args[0])
            if not tid:
                return [], "QQ 号不对：%r" % args[0]
            reject = _truthy(args[1]) if len(args) > 1 else False
            return [Intent("set_group_kick",
                           {"group_id": _num(gid), "user_id": _num(tid),
                            "reject_add_request": reject},
                           "cmd_kick", "已把 %s 移出本群（拒再进=%s）" % (tid, reject))], ""
        if name == "card":
            if len(args) < 2:
                return [], "用法：改名片 <QQ> <新名片>"
            tid = _parse_uid(args[0])
            if not tid:
                return [], "QQ 号不对：%r" % args[0]
            card = " ".join(args[1:])
            return [Intent("set_group_card",
                           {"group_id": _num(gid), "user_id": _num(tid), "card": card},
                           "cmd_card", "已把 %s 的名片改成 %s" % (tid, card))], ""
        if name in ("recall", "essence", "unessence"):
            mid = ""
            if args:
                mid = args[0] if str(args[0]).isdigit() else ""
                if not mid:
                    return [], "消息 id 不对：%r" % args[0]
            else:
                mid = _proto.reply_message_id(event) or ""   # 不带参数 → 操作你引用的那条
            if not mid:
                return [], "没给消息 id，也没引用任何消息"
            if name == "recall":
                return [Intent("delete_msg", {"message_id": _num(mid)}, "cmd_recall",
                               "已撤回消息 %s" % mid)], ""
            action = "set_essence" if name == "essence" else "delete_essence"
            return [Intent(action, {"message_id": _num(mid)},
                           "cmd_essence" if name == "essence" else "cmd_unessence",
                           "已%s消息 %s" % ("设为精华" if name == "essence" else "取消精华", mid))], ""
        if name == "stats":
            s = self.stats(gid)
            return [self._internal("cmd_stats",
                                   "本群统计：累计 %d 条发言，%d 人参与，最活跃的是 %s（%d 条）。"
                                   % (s["messages"], s["users"], s["top_user"] or "无", s["top_count"]))], ""
        return [], "没实现的命令：%s" % name

    # ── Intent 构造小工具 ─────────────────────────────────────────────────

    def _send_group(self, gid: str, text: str, kind: str, note: str = "") -> Intent:
        return Intent("send_group_msg", _group_text_params(gid, text), kind, note)

    def _internal(self, kind: str, note: str) -> Intent:
        """不发请求、只把 note 送进日志/上下文（通知类必须有这条兜底，不许吞成空表）。"""
        return Intent(ACTION_INTERNAL, {}, kind, note)

    def _owner_notice(self, note: str, kind: str) -> List[Intent]:
        """给主人的通知：有主人号就私聊他；没有主人号也**照样产出**（内部 Intent）。"""
        c = self.cfg.get("request") or {}
        if not _truthy(c.get("notify_owner"), True) or not self.owner_ids:
            return [self._internal(kind, note)]
        params = _proto.build_action("send_private_msg", self.owner_ids[0], note,
                                     is_group=False)["params"]
        return [Intent("send_private_msg", params, kind, note)]


def _group_text_params(gid: str, text: str) -> Dict[str, Any]:
    """群发文本的 action 参数（复用 proto 的构造，**不带 echo/self_id**）。"""
    return _proto.build_action("send_group_msg", gid, text, is_group=True)["params"]
