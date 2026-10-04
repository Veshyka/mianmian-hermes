"""群消息滚动窗口 —— 「连贯层」（B 阶段）。

主人定的三层设计里的第 1 层（见 `../RESEARCH-group-chat-two-benchmarks-mapping.md` §8）：
**每个群一份滚动窗口（含发送者昵称、时间、纯文本与 `[图片]`/`[表情]` 占位），只活在群上下文里。**
以后她被 @ 唤醒时，把该群的窗口当引用材料给她，她就能「看到前面聊了啥」。

红线（本模块的**全部**职责边界，改动前先读三遍）：

1. **绝不入主记忆库。** 本模块不 import 任何 memory / hindsight / recall 相关模块，
   也没有任何写库调用 —— 单一职责：把记录追进一个**有上限**的 JSONL 文件。
   单测 `tests/check_group_window.py::TestNoMemorySink` 有源码级守卫盯着这条。
2. **上限双闸**：条数（默认 200）+ 总字节（默认 256 KiB）。超了**丢最旧**，
   绝不让一个话痨群把磁盘吃光（同一份调研的 R5 风险）。
3. **写失败不许炸采集链路**：任何异常都吃进 `errors` 计数，调用方永远拿到 `None` 而不是异常。
4. **窗口文件里只有：时间、QQ 号、昵称、正文/占位。** 不落原始事件、不落图片 URL、不落凭据。

落盘：`<profile>/onebot-groups/<群号>.jsonl`（在 `/opt/data` 持久卷里，重建容器不丢）。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import onebot_time as _clock

logger = logging.getLogger(__name__)

DEFAULT_MAX_MSGS = 200            # 条数上限
DEFAULT_MAX_BYTES = 256 * 1024    # 总字节上限（256 KiB）
SOURCE_LABEL = "qq-group"         # 窗口材料的来源标签（C 阶段注入提示词时要带上）

_SAFE_GID = set("0123456789-_")


class GroupWindow:
    """每群一份、有上限、可落盘重载的滚动窗口。线程/协程内使用（适配器是单事件循环）。"""

    def __init__(self, root: Any, *, max_msgs: int = DEFAULT_MAX_MSGS,
                 max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self.root = Path(root)
        self.max_msgs = max(1, int(max_msgs))
        self.max_bytes = max(1024, int(max_bytes))
        self.appended = 0        # 本进程累计收下的群消息条数
        self.trimmed = 0         # 因上限被淘汰的条数
        self.errors = 0          # 落盘/读取失败次数（>0 会在心跳里报提醒）
        self._cache: Dict[str, List[Dict[str, Any]]] = {}
        self._bytes: Dict[str, int] = {}

    # ── 路径 ───────────────────────────────────────────────────────────────

    def path(self, gid: str) -> Path:
        """窗口文件路径。群号理论上一定是数字；非数字字符一律换成 `_`（防路径穿越）。"""
        safe = "".join(c if c in _SAFE_GID else "_" for c in str(gid)) or "unknown"
        return self.root / f"{safe}.jsonl"

    # ── 读 ─────────────────────────────────────────────────────────────────

    def _load(self, gid: str) -> List[Dict[str, Any]]:
        if gid in self._cache:
            return self._cache[gid]
        recs: List[Dict[str, Any]] = []
        total = 0
        p = self.path(gid)
        try:
            if p.is_file():
                with p.open(encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except ValueError:
                            continue      # 半行（进程被杀）容忍：跳过这一行，不炸
                        if isinstance(rec, dict):
                            recs.append(rec)
                            total += len(line.encode("utf-8")) + 1
        except OSError as e:
            self.errors += 1
            logger.warning("[onebot] group window unreadable (%s): %s", _mask_path(p), e)
        self._cache[gid] = recs
        self._bytes[gid] = total
        return recs

    def tail(self, gid: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """最近 N 条（新的在后面）。不改窗口、不落盘。"""
        recs = self._load(gid)
        if limit is None or limit >= len(recs):
            return list(recs)
        return recs[-max(0, int(limit)):] if limit > 0 else []

    def groups(self) -> List[str]:
        out = set(self._cache)
        try:
            if self.root.is_dir():
                out |= {p.stem for p in self.root.glob("*.jsonl")}
        except OSError:
            self.errors += 1
        return sorted(out)

    # ── 写 ─────────────────────────────────────────────────────────────────

    def append(self, gid: str, *, text: str, uid: str = "", name: str = "",
               ts: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """追一条进窗口。**永不抛异常**（采集链路的最后一环，坏了只记账）。"""
        text = (text or "").strip()
        if not text:
            return None
        when = float(ts) if ts is not None else time.time()
        rec: Dict[str, Any] = {
            "ts": when,
            # 容器是 UTC，但窗口里给她看的时间必须是北京时间（见 onebot_time 头注）
            "t": _clock.stamp(when),
            "uid": str(uid or ""),
            "name": str(name or "") or str(uid or "N/A"),
            "text": text,
        }
        try:
            recs = self._load(gid)
            recs.append(rec)
            self.appended += 1
            # 先把这条的字节算进去，再判上限 —— 否则字节闸会慢一条（实测差 ~120 字节）
            self._bytes[gid] = self._bytes.get(gid, 0) + _rec_size(rec)
            self._trim(gid)
            self._flush(gid)
            return rec
        except Exception as e:                      # noqa: BLE001 —— 采集不许炸
            self.errors += 1
            logger.warning("[onebot] group window append failed (group %s): %s", str(gid)[:12], e)
            return None

    def _trim(self, gid: str) -> None:
        """双闸淘汰：先按条数，再按字节。都从**最旧**那头丢。"""
        recs = self._cache.get(gid, [])
        if len(recs) > self.max_msgs:
            drop = len(recs) - self.max_msgs
            del recs[:drop]
            self.trimmed += drop
        while recs and self._bytes.get(gid, 0) > self.max_bytes:
            gone = recs.pop(0)
            self._bytes[gid] = self._bytes.get(gid, 0) - (_rec_size(gone))
            self.trimmed += 1

    def _flush(self, gid: str) -> None:
        """整份重写（原子替换）。窗口最大 200 条 → 一次几 KB，代价可忽略，换来「永不半截」。"""
        recs = self._cache.get(gid, [])
        self.root.mkdir(parents=True, exist_ok=True)
        p = self.path(gid)
        tmp = p.with_suffix(".jsonl.tmp")
        total = 0
        with tmp.open("w", encoding="utf-8") as fh:
            for rec in recs:
                line = json.dumps(rec, ensure_ascii=False)
                fh.write(line + "\n")
                total += len(line.encode("utf-8")) + 1
        tmp.replace(p)
        self._bytes[gid] = total

    # ── 呈现 ───────────────────────────────────────────────────────────────

    @staticmethod
    def rec_id(rec: Dict[str, Any]) -> Tuple[float, str, str]:
        """一条记录的稳定标识（增量注入的游标用它，缺 id 字段也不用改 schema）。"""
        return (float(rec.get("ts") or 0.0), str(rec.get("uid") or ""), str(rec.get("text") or ""))

    def delta_since(self, gid: str, cursor: Optional[Dict[str, Any]],
                    limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """``cursor`` 之后**新增**的记录（新的在后面）—— 增量注入用，避免每轮重发整段。

        主人 2026-10-03：「取消重复的部分，上下文它是完全看得到的」—— 上一轮注入过的
        内容已经在会话历史里，重发一遍既浪费 token 又让压缩提前触发。

        * ``cursor`` 为空（首次唤醒 / 刚重启）→ 退化成 ``tail(limit)``（先给一段底稿）
        * ``cursor`` 已经被淘汰（超出窗口条数）→ 同样退回 ``tail(limit)``（宁多勿断）
        * ``limit`` → 每次最多注入多少条（久未唤醒时防止一次灌几百条）
        """
        recs = self._load(gid)
        if not cursor:
            return self.tail(gid, limit=limit)
        want = self.rec_id(cursor)
        idx: Optional[int] = None
        for i in range(len(recs) - 1, -1, -1):
            if self.rec_id(recs[i]) == want:
                idx = i
                break
        if idx is None:
            return self.tail(gid, limit=limit)
        fresh = recs[idx + 1:]
        if limit is not None and limit > 0 and len(fresh) > limit:
            fresh = fresh[-int(limit):]
        return fresh

    def render_records(self, gid: str, recs: List[Dict[str, Any]], *,
                       delta: bool = False) -> str:
        """把若干条记录渲染成给**她**看的材料（窗口口径 / 增量口径共用）。"""
        head = (f"【群聊{'新增' if delta else '上下文'} · source={SOURCE_LABEL} · 群{_mask_gid(gid)} · "
                f"{'上次之后 ' if delta else '最近 '}{len(recs)} 条 · 仅供理解，别照抄】")
        lines = [head]
        for rec in recs:
            # 带上 QQ 号（2026-10-03）：她要 @ 人必须知道对方号码 —— 出站 @ 靠
            # 正文里的 `[CQ:at,qq=<号>]`（`onebot_proto.cq_to_segments` 会转成真 at 段）。
            # 号码写在同一行括号里，既给人看也给她抄。
            who = str(rec.get("name", ""))
            uid = str(rec.get("uid", "") or "")
            label = f"{who}(QQ:{uid})" if uid else who
            lines.append(f"[{rec.get('t', '')}] {label}: {rec.get('text', '')}")
        return "\n".join(lines)

    def render_context(self, gid: str, limit: int = 100) -> str:
        """把窗口渲染成给**她**看的上下文材料（C 阶段被 @ 唤醒时注入）。

        带 `source=qq-group` 标签：这是在源头就把「这是群聊来的」写清楚 ——
        将来她要写记忆时，这条材料本身已经标了来源。
        """
        return self.render_records(gid, self.tail(gid, limit=limit))

    def stats(self) -> Dict[str, Any]:
        gids = self.groups()
        msgs = 0
        size = 0
        for gid in gids:
            recs = self._load(gid)
            msgs += len(recs)
            size += self._bytes.get(gid, 0)
        return {
            "dir": str(self.root),
            "groups": len(gids),
            "msgs": msgs,
            "bytes": size,
            "max_msgs": self.max_msgs,
            "max_bytes": self.max_bytes,
            "appended": self.appended,
            "trimmed": self.trimmed,
            "errors": self.errors,
        }

    def summary(self) -> str:
        """一行摘要（进心跳，给 doctor / 人肉排查看）。"""
        s = self.stats()
        return (f"{s['groups']} 群/{s['msgs']} 条/{_human(s['bytes'])}"
                f"（上限 {s['max_msgs']} 条/{_human(s['max_bytes'])}，"
                f"未入主库；淘汰 {s['trimmed']}，错 {s['errors']}）")


# ── 小工具 ─────────────────────────────────────────────────────────────────


def _rec_size(rec: Dict[str, Any]) -> int:
    return len(json.dumps(rec, ensure_ascii=False).encode("utf-8")) + 1


def _human(n: int) -> str:
    x = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if x < 1024 or unit == "GB":
            return f"{x:.0f}{unit}" if unit == "B" else f"{x:.1f}{unit}"
        x /= 1024
    return f"{x:.1f}GB"


def _mask_gid(gid: str) -> str:
    s = str(gid)
    return s if len(s) <= 4 else f"{s[:2]}***{s[-2:]}"


def _mask_path(p: Path) -> str:
    return f"{p.parent.name}/{p.name}"
