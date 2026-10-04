#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""表情包库（sticker library）—— 偷图 / 入库 / 上限 / 挑图 / 打标。

设计要点（依据见 `chat-layer/PLAN-v6-表情包.md`）：

* **判据用协议字段，不用启发式**：NapCat 把「收藏/自定义表情」上报成 ``type=image``
  但带 ``sub_type=1``（KCUSTOM）／``sub_type=2``（KHOT）／``emoji_id``（商城超级表情）；
  普通照片是 ``sub_type=0``。见 ``is_sticker_image()``。
* **去重按内容**：文件名 = 图片字节 sha256 前 16 位 + 扩展名（与 ``media_dir`` 同名规则），
  同 hash 直接复用、只更新最后使用时间。
* **索引是给人/给模型读的纯文本**（她只有 file 工具，没有 execute_code）：
  ``onebot-stickers/index.txt``，一行一条，空格分隔：
  ``<hash> <标签> <字节数> <最后使用ISO> <绝对路径>``，标签逗号分隔、未打标写 ``未标``。
* **上限**：默认 200 条 / 1GB（主人 2026-10-03 定），单条 ≤ 8MB；超限按「最后使用时间」
  最旧优先淘汰（LRU）。
* **不留悬念**：任何一步失败都返回结构化的 ``{"ok": False, "reason": ...}``，绝不静默。

CLI（供 cron、私聊里的 terminal、排障用）::

    python3 sticker_lib.py stats
    python3 sticker_lib.py list --limit 50
    python3 sticker_lib.py pick --emotion 开心
    python3 sticker_lib.py gc --dry-run
    python3 sticker_lib.py tag --all        # 用本地视觉模型给未标的表情打情绪标签
"""

from __future__ import annotations

import argparse
import base64
import hashlib
from datetime import datetime
import json
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── 默认参数（可被环境变量覆盖；适配器也会直接传值进来）────────────────────

#: 单条表情的体积上限（字节）。超过就不入库（但消息里的 [表情包:<路径>] 仍保留）。
MAX_ITEM_BYTES = 8 * 1024 * 1024

#: 库里最多留多少条 / 总体积上限（主人 2026-10-03 定：200 张 / 1G）。
MAX_ITEMS = 200
MAX_TOTAL_BYTES = 1024 * 1024 * 1024

#: 多久没被用过就清理（天）。0 = 不按时间清理。
RETENTION_DAYS = 30

#: 判据只有一份实现：`onebot_proto.is_sticker_image()`（协议层的事留在协议层）。
#: 这里只是给她/CLI 复用一个名字，**不要在本地再抄一份**（防两边漂移）。
try:                                                    # 被适配器当包导入时
    from . import onebot_proto as _proto
except ImportError:                                     # 直接 `python3 sticker_lib.py` 时
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import onebot_proto as _proto  # type: ignore

is_sticker_image = _proto.is_sticker_image
sticker_label = _proto.sticker_label

UNTAGGED = "未标"


def default_dir(profile_home: Optional[str] = None) -> Path:
    """表情包库目录：``<HERMES_HOME>/onebot-stickers``。"""
    if profile_home:
        base = Path(profile_home)
    else:
        base = Path(os.environ.get("HERMES_HOME") or "/opt/data")
    return base / "onebot-stickers"


# ── 索引读写 ────────────────────────────────────────────────────────────────

class Item:
    __slots__ = ("hash", "tags", "size", "last_used", "path")

    def __init__(self, h: str, tags: List[str], size: int, last_used: float, path: str):
        self.hash = h
        self.tags = tags
        self.size = size
        self.last_used = last_used
        self.path = path

    @property
    def line(self) -> str:
        tags = ",".join(self.tags) if self.tags else UNTAGGED
        # ⚠️ 时间戳**必须带微秒**：索引是文本、每次 add 都要「读-改-写」一遍，
        # 秒级精度会让同一秒内入库的几条 last_used 完全相同 → LRU 排序变成随机，
        # 淘汰会删错人（这个 bug 是 check_sticker.py 的 LRU 用例抓出来的）。
        ts = datetime.fromtimestamp(self.last_used).strftime("%Y-%m-%dT%H:%M:%S.%f")
        return f"{self.hash} {tags} {self.size} {ts} {self.path}"

    def as_dict(self) -> Dict[str, Any]:
        return {"hash": self.hash, "tags": self.tags, "size": self.size,
                "last_used": self.last_used, "path": self.path}


def _parse_line(line: str) -> Optional[Item]:
    parts = line.strip().split()
    if len(parts) < 5:
        return None
    h, tags, size, ts, path = parts[0], parts[1], parts[2], parts[3], " ".join(parts[4:])
    try:
        size_i = int(size)
    except ValueError:
        size_i = 0
    # ⚠️ 必须用 datetime.strptime：`time.strptime` **不支持 %f**（会抛
    # "'f' is a bad directive"）→ 全表解析失败 → last_used 全变 0 → LRU 删错人。
    last = 0.0
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):   # 老索引（秒级）也认
        try:
            last = datetime.strptime(ts, fmt).timestamp()
            break
        except ValueError:
            continue
    tag_list = [] if tags in (UNTAGGED, "-", "") else [t for t in tags.split(",") if t]
    return Item(h, tag_list, size_i, last, path)


class StickerLib:
    """表情包库。所有方法都不抛异常（除非磁盘真炸），失败给 ``{"ok": False}``。"""

    def __init__(self, root: Optional[Path] = None, *,
                 max_items: int = MAX_ITEMS,
                 max_total_bytes: int = MAX_TOTAL_BYTES,
                 max_item_bytes: int = MAX_ITEM_BYTES,
                 retention_days: int = RETENTION_DAYS):
        self.root = Path(root) if root else default_dir()
        self.index_path = self.root / "index.txt"
        self.max_items = max_items
        self.max_total_bytes = max_total_bytes
        self.max_item_bytes = max_item_bytes
        self.retention_days = retention_days

    # -- 读写 ---------------------------------------------------------------
    def load(self) -> List[Item]:
        if not self.index_path.is_file():
            return []
        items: List[Item] = []
        try:
            for line in self.index_path.read_text(encoding="utf-8").splitlines():
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                it = _parse_line(line)
                if it is not None:
                    items.append(it)
        except Exception:  # noqa: BLE001
            return []
        return items

    def save(self, items: List[Item]) -> bool:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = self.index_path.with_name(self.index_path.name + ".tmp")
            body = "".join(it.line + "\n" for it in items)
            tmp.write_text(body, encoding="utf-8")
            tmp.replace(self.index_path)
            return True
        except Exception:  # noqa: BLE001
            return False

    # -- 入库 ---------------------------------------------------------------
    def add(self, blob: bytes, data: Dict[str, Any], path: str,
            tags: Optional[List[str]] = None) -> Dict[str, Any]:
        """把一张表情包记进库（文件已由调用方落盘，这里只登记 + 去重 + 限量）。

        ``path`` 是落盘后的绝对路径；``data`` 是 OneBot image 段的 data（用来兜底猜标签）。
        """
        if not blob:
            return {"ok": False, "reason": "empty"}
        if len(blob) > self.max_item_bytes:
            return {"ok": False, "reason": "item_too_large", "size": len(blob)}
        h = hashlib.sha256(blob).hexdigest()[:16]
        now = time.time()
        items = self.load()
        for it in items:
            if it.hash == h:
                it.last_used = now
                if tags:
                    it.tags = tags
                it.path = path
                self.save(items)
                return {"ok": True, "dedup": True, "hash": h, "path": path,
                        "count": len(items)}
        items.append(Item(h, tags or [], len(blob), now, path))
        evicted = self.enforce_limits(items, save=False)
        self.save(items)
        return {"ok": True, "dedup": False, "hash": h, "path": path,
                "count": len(items), "evicted": evicted}

    def touch(self, h: str) -> bool:
        items = self.load()
        hit = False
        now = time.time()
        for it in items:
            if it.hash == h:
                it.last_used = now
                hit = True
        if hit:
            self.save(items)
        return hit

    def set_tags(self, h: str, tags: List[str]) -> bool:
        items = self.load()
        hit = False
        for it in items:
            if it.hash == h:
                it.tags = tags
                hit = True
        if hit:
            self.save(items)
        return hit

    # -- 上限与淘汰 ---------------------------------------------------------
    def enforce_limits(self, items: Optional[List[Item]] = None, *, save: bool = True,
                       dry_run: bool = False) -> List[Dict[str, Any]]:
        """超条数/超体积 → 按最后使用时间最旧的先删（LRU）。返回被淘汰的清单。"""
        items = self.load() if items is None else items
        evicted: List[Dict[str, Any]] = []
        # 1) 按体积
        total = sum(it.size for it in items)
        while (len(items) > self.max_items or total > self.max_total_bytes) and items:
            oldest = min(items, key=lambda i: (i.last_used, i.size))
            items.remove(oldest)
            total -= oldest.size
            evicted.append({"hash": oldest.hash, "path": oldest.path,
                            "size": oldest.size, "reason": "lru_limit"})
            if not dry_run:
                self._unlink(oldest.path)
        if save and not dry_run:
            self.save(items)
        return evicted

    def gc(self, *, dry_run: bool = False) -> Dict[str, Any]:
        """① 删掉索引里已不存在的文件 ② 超过保留天数没用的删掉 ③ 跑一遍上限。"""
        items = self.load()
        removed: List[Dict[str, Any]] = []
        keep: List[Item] = []
        cutoff = time.time() - self.retention_days * 86400 if self.retention_days > 0 else 0
        for it in items:
            if not Path(it.path).is_file():
                removed.append({"hash": it.hash, "path": it.path, "reason": "missing_file"})
                continue
            if cutoff and it.last_used and it.last_used < cutoff:
                removed.append({"hash": it.hash, "path": it.path, "reason": "expired"})
                if not dry_run:
                    self._unlink(it.path)
                continue
            keep.append(it)
        evicted = self.enforce_limits(keep, save=False, dry_run=dry_run)
        removed.extend(evicted)
        if not dry_run:
            self.save(keep)
        return {"ok": True, "removed": removed, "kept": len(keep),
                "dry_run": dry_run}

    def _unlink(self, path: str) -> None:
        try:
            p = Path(path)
            if p.is_file():
                p.unlink()
        except Exception:  # noqa: BLE001
            pass

    # -- 挑图 ---------------------------------------------------------------
    def pick(self, emotion: Optional[str] = None, *,
             rng: Optional[random.Random] = None) -> Optional[str]:
        """按情绪标签随机挑一条（没有匹配就退到全部随机；库空返回 None）。"""
        items = [it for it in self.load() if Path(it.path).is_file()]
        if not items:
            return None
        pool = items
        if emotion:
            want = emotion.strip()
            hit = [it for it in items if any(want in t or t in want for t in it.tags)]
            if hit:
                pool = hit
            else:
                loose = [it for it in items
                         if any(k in "".join(it.tags) for k in _EMOTION_ALIASES.get(want, [want]))]
                if loose:
                    pool = loose
        r = rng or random
        return r.choice(pool).path

    def stats(self) -> Dict[str, Any]:
        items = self.load()
        return {"ok": True, "count": len(items), "bytes": sum(i.size for i in items),
                "tagged": sum(1 for i in items if i.tags),
                "untagged": sum(1 for i in items if not i.tags),
                "root": str(self.root),
                "max_items": self.max_items, "max_total_bytes": self.max_total_bytes}


#: 情绪的近义扩展 —— 她说「开心」，库里标签可能是「高兴/笑/乐」。
_EMOTION_ALIASES: Dict[str, List[str]] = {
    "开心": ["高兴", "笑", "乐", "happy", "愉快", "哈哈"],
    "难过": ["伤心", "哭", "委屈", "低落", "sad"],
    "生气": ["愤怒", "气", "怒", "angry", "无语"],
    "惊讶": ["震惊", "吃惊", "懵", "惊", "surprise"],
    "喜欢": ["爱", "心动", "比心", "亲", "love", "可爱"],
    "无语": ["尴尬", "汗", "白眼", "沉默", "冷"],
    "鼓励": ["加油", "打气", "抱抱", "安慰", "支持"],
}


# ── 打标（**不用视觉模型**：标签由她自己按当时的情绪写）──────────────────────
#
# 主人的决定（2026-10-03）：**不做 VLM 自动打标**。挑图只用「情绪/标签 + 随机」——
# 有标签就按标签挑，没标签就从全部里随机。所以打标是**手写入口**：
#
#     python3 sticker_lib.py tag --last --tags 开心,无语      # 最新偷到的那条
#     python3 sticker_lib.py tag --hash <前16位> --tags 笑死
#     python3 sticker_lib.py tag --path /opt/.../<hash>.gif --tags 生气
#
# 她偷表情时正好在场，知道当时是什么情绪 —— 这个信息比让模型看图案猜更准、还免费。

def normalize_tags(text: str) -> List[str]:
    """把自由文本揉成标签列表（最多 5 个、单个 ≤ 6 字、去重）。"""
    out: List[str] = []
    raw = re.sub(r"[，、;；/|]+", ",", str(text or "").replace("\n", ","))
    for tok in raw.split(","):
        t = tok.strip().strip("。.!！:：\"'“”[]【】-—0123456789. ").strip()
        if not t or len(t) > 6 or t in out:
            continue
        out.append(t)
        if len(out) >= 5:
            break
    return out


def tag(lib: StickerLib, *, hash_: Optional[str] = None, path: Optional[str] = None,
        last: bool = False, tags: str = "") -> Dict[str, Any]:
    """给一条表情写情绪标签（``--hash`` / ``--path`` / ``--last`` 三选一）。"""
    tag_list = normalize_tags(tags)
    if not tag_list:
        return {"ok": False, "reason": "empty_tags"}
    items = lib.load()
    target: Optional[Item] = None
    if last:
        live = [it for it in items if Path(it.path).is_file()]
        target = max(live, key=lambda i: i.last_used) if live else None
    else:
        for it in items:
            if hash_ and it.hash == str(hash_).strip():
                target = it
                break
            if path and it.path == str(path).strip():
                target = it
                break
    if target is None:
        return {"ok": False, "reason": "not_found"}
    lib.set_tags(target.hash, tag_list)
    return {"ok": True, "hash": target.hash, "tags": tag_list, "path": target.path}


# ── CLI ─────────────────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="表情包库（偷图/限量/挑图/打标）")
    ap.add_argument("--root", default=None, help="库目录（默认 <HERMES_HOME>/onebot-stickers）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("stats")
    p_list = sub.add_parser("list")
    p_list.add_argument("--limit", type=int, default=30)
    p_pick = sub.add_parser("pick")
    p_pick.add_argument("--emotion", default=None)
    p_pick.add_argument("--json", action="store_true")
    p_gc = sub.add_parser("gc")
    p_gc.add_argument("--dry-run", action="store_true")
    p_tag = sub.add_parser("tag")
    p_tag.add_argument("--hash", default=None, help="表情 hash（文件名前 16 位）")
    p_tag.add_argument("--path", default=None, help="表情绝对路径")
    p_tag.add_argument("--last", action="store_true", help="最新偷到的那条")
    p_tag.add_argument("--tags", default="", help="情绪标签，逗号分隔，最多 5 个")

    a = ap.parse_args(argv)
    lib = StickerLib(Path(a.root) if a.root else None)

    if a.cmd == "stats":
        print(json.dumps(lib.stats(), ensure_ascii=False, indent=2))
        return 0
    if a.cmd == "list":
        items = sorted(lib.load(), key=lambda i: i.last_used, reverse=True)
        for it in items[: a.limit]:
            print(it.line)
        print(f"# 共 {len(items)} 条（显示前 {min(len(items), a.limit)}）")
        return 0
    if a.cmd == "pick":
        p = lib.pick(a.emotion)
        if a.json:
            print(json.dumps({"path": p}, ensure_ascii=False))
        elif p:
            print(p)
        else:
            print("(库是空的：还没有偷到表情包)", file=sys.stderr)
        return 0 if p else 1
    if a.cmd == "gc":
        print(json.dumps(lib.gc(dry_run=a.dry_run), ensure_ascii=False, indent=2))
        return 0
    if a.cmd == "tag":
        r = tag(lib, hash_=a.hash, path=a.path, last=a.last, tags=a.tags)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r.get("ok") else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
