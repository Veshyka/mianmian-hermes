#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""表情包自动打标：入库排队 → 本地视觉模型 → 写回标签。

## 主人定的（2026-10-04，推翻 2026-10-03 的「不做 VLM 自动打标」）

> 「入库自动就等着排队识别表情打标签吧，这个让本地就应该能做，这个模型能力应该是够的，
>  至于发的时候可以随机选，比如开心，就从几个里面随机一个就好了」

## 为什么必须是「排队」而不是当场打

本地视觉模型 = 8081 的 Bonsai 27B（`--parallel 1`，还兼着 Hindsight 抽取）。
当场同步打标会卡住接收循环、还会跟记忆抽取抢唯一那个槽。所以：

* 入库只做一件事：**入队**（`note_new`），立刻返回；
* 后台 worker 慢慢消化，每条之间留 `interval_s` 间隔（默认 15s），让抽取插得进来；
* 一条打完再打下一批（`batch` 条一轮），永远不并发。

## 边界（都是硬要求）

* 只处理「未标」的条目；打不出来就留在未标。
* 同一条最多试 `max_attempts` 次，超了就本次进程内放弃（重启会再扫一遍）。
* 任何异常都不许冒到接收循环；失败只记账，`status()` 能查。
* 视觉端点连不上时**静默退让**（不报警、不重试风暴）——「排队识别」本来就是空闲活。

## 标签从哪来

① 模型看画面 + 图上的字 → 受控情绪词表里挑一个（`TAGS`），另有 `word` 字段抄图上的文字；
② 协议端给的人话（`summary`，如「[笑]」）作为**免费线索**一起入标签。
两边都进索引，挑图时 `StickerLib.pick(emotion)` 做近义扩展。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

try:                                                    # 被适配器当包导入时
    from . import sticker_lib as _lib
except ImportError:                                     # 单独导入（测试/CLI）
    import sticker_lib as _lib  # type: ignore

logger = logging.getLogger(__name__)

#: 受控情绪词表（挑图时她说「开心」就落在这里的某一格；模型只能从里面挑）。
TAGS: List[str] = [
    "开心", "笑死", "喜欢", "害羞", "得意", "无语", "疑惑", "惊讶",
    "难过", "委屈", "生气", "累了", "求安慰", "鼓励", "赞", "其他",
]

DEFAULT_URL = "http://172.17.0.1:8081/v1/chat/completions"
DEFAULT_MODEL = "bonsai2-27b"

PROMPT = """你在给 QQ 表情包分类。情绪看**发的人想表达什么**，不是画面里人物笑不笑。
判据：眯眼/被黑线挡眼/冷淡脸/托腮发呆 → 无语；大笑/眼睛弯 → 开心；皱眉瞪眼 → 生气；
流泪/委屈脸/垂头 → 难过；问号脸/歪头 → 疑惑；跪地/倒下/瘫 → 累了；脸红/捂脸/躲 → 害羞。
只输出一个 JSON 对象，不要解释、不要多余字段：
{"see":"一句话描述画面与人物的脸色/姿态","tag":"从这些里挑一个：%s","word":"画面里若有文字就照抄，没有就空字符串"}""" % "、".join(TAGS)

_FENCE = re.compile(r"^\s*```(?:json)?|```\s*$")


def _log() -> logging.Logger:
    return logger


class StickerLabeler:
    """入库排队 + 后台打标。所有对外方法都不抛异常。"""

    def __init__(self, lib: "_lib.StickerLib", *,
                 enabled: bool = True,
                 url: str = DEFAULT_URL,
                 model: str = DEFAULT_MODEL,
                 tags: Optional[List[str]] = None,
                 timeout: float = 600.0,
                 max_attempts: int = 3,
                 interval_s: float = 15.0,
                 batch: int = 3,
                 max_queue: int = 400,
                 boot_scan: bool = True,
                 post: Optional[Callable[..., Any]] = None) -> None:
        self.lib = lib
        self.enabled = bool(enabled)
        self.url = str(url or DEFAULT_URL)
        self.model = str(model or DEFAULT_MODEL)
        self.tags = list(tags or TAGS)
        self.timeout = float(timeout)
        self.max_attempts = int(max_attempts)
        self.interval_s = max(0.0, float(interval_s))
        self.batch = max(1, int(batch))
        self.max_queue = max(1, int(max_queue))
        self.boot_scan = bool(boot_scan)
        self._post = post                      # 测试注入；None = 真的打 HTTP
        self._queue: List[str] = []            # hash 队列（去重，先进先出）
        self._attempts: Dict[str, int] = {}
        self._hints: Dict[str, List[str]] = {}   # hash → 协议端给的人话标签
        self._task: Optional[asyncio.Task] = None
        self._wake = asyncio.Event()
        self.done = 0
        self.failed = 0
        self.skipped = 0
        self.last_error = ""
        self.last_see = ""        # 模型那句「看到什么」——只用于排障，不进索引

    # ── 入队 ────────────────────────────────────────────────────────────────
    def note_new(self, hash_: Optional[str], hint: Optional[str] = None) -> None:
        """入库后调用（只入队，绝不阻塞、绝不打网络）。

        ``hint``：协议端给的人话（`summary`，如「笑」）—— 免费线索，跟模型标签一起写进索引。
        """
        if not self.enabled or not hash_:
            return
        h = str(hash_)
        if h in self._queue or self._attempts.get(h, 0) >= self.max_attempts:
            return
        if len(self._queue) >= self.max_queue:
            self.skipped += 1
            return
        if hint:
            self._hints[h] = _lib.normalize_tags(hint)[:1]
        self._queue.append(h)
        self._wake.set()

    def scan_untagged(self) -> int:
        """启动时扫一遍库里「未标」的条目并入队（她偷图时不在场那些）。"""
        if not (self.enabled and self.boot_scan):
            return 0
        try:
            items = self.lib.load()
        except Exception:  # noqa: BLE001
            return 0
        n = 0
        for it in items:
            if it.tags:
                continue
            self.note_new(it.hash)
            n += 1
        if n:
            _log().info("[onebot] 表情打标：启动扫描发现 %d 条未标，排队消化", n)
        return n

    # ── worker ──────────────────────────────────────────────────────────────
    def start(self) -> None:
        if not self.enabled or self._task is not None:
            return
        self.scan_untagged()
        try:
            self._task = asyncio.create_task(self._run())
        except RuntimeError:                   # 没有运行中的 loop（测试场景）
            self._task = None

    async def stop(self) -> None:
        t, self._task = self._task, None
        if t is not None:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _run(self) -> None:
        """后台循环：每次醒来打 `batch` 条，然后歇 `interval_s`（给抽取让路）。"""
        while True:
            try:
                if not self._queue:
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=60.0)
                    except asyncio.TimeoutError:
                        pass
                    continue
                for _ in range(self.batch):
                    if not self._queue:
                        break
                    h = self._queue.pop(0)
                    await self._label_one(h)
                    if self.interval_s:
                        await asyncio.sleep(self.interval_s)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 —— worker 不许死
                _log().exception("[onebot] 表情打标 worker 异常（继续跑）")
                await asyncio.sleep(5.0)

    def _find(self, hash_: str) -> Optional[Any]:
        try:
            for it in self.lib.load():
                if it.hash == hash_:
                    return it
        except Exception:  # noqa: BLE001
            return None
        return None

    async def _label_one(self, hash_: str) -> Optional[List[str]]:
        it = self._find(hash_)
        if it is None or it.tags:
            return None
        p = Path(it.path)
        if not p.is_file():
            self.failed += 1
            self.last_error = "file_missing"
            return None
        self._attempts[hash_] = self._attempts.get(hash_, 0) + 1
        try:
            blob = p.read_bytes()
            raw = await self._ask(blob, p.suffix.lower())
            tags = self._parse(raw)
        except Exception as e:  # noqa: BLE001
            self.failed += 1
            self.last_error = f"{type(e).__name__}: {e}"
            _log().info("[onebot] 表情打标失败 %s（第 %d 次）：%s",
                        hash_, self._attempts[hash_], self.last_error)
            return None
        if not tags:
            self.failed += 1
            self.last_error = "no_tag"
            return None
        try:
            self.lib.set_tags(hash_, tags + self._protocol_hint(hash_))
        except Exception:  # noqa: BLE001
            self.failed += 1
            self.last_error = "index_write_failed"
            return None
        self.done += 1
        _log().info("[onebot] 表情打标 %s → %s", hash_, tags)
        return tags

    def _protocol_hint(self, hash_: str) -> List[str]:
        """入库时协议端给的人话线索（`summary`，如「[笑]」）—— 免费、零调用。"""
        return list(self._hints.get(str(hash_), []))

    # ── 模型调用 ────────────────────────────────────────────────────────────
    async def _ask(self, blob: bytes, suffix: str) -> str:
        b64 = base64.b64encode(blob).decode()
        mime = "image/gif" if suffix == ".gif" else (
            "image/png" if suffix == ".png" else "image/jpeg")
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ]}],
            "temperature": 0.1,
            "max_tokens": 200,
        }
        post = self._post or _default_post
        # ⚠️ 必须丢线程跑：这是**阻塞** urllib 调用，超时给到 600s ——
        # 直接在事件循环里调 = 她的网关（接收循环、所有回合）被卡死几分钟。
        # （这个 bug 是 2026-10-04 自查时抓到的：回调本身写得对，但调用方式是同步的。）
        if asyncio.iscoroutinefunction(post):
            out = await post(self.url, body, self.timeout)
        else:
            out = await asyncio.to_thread(post, self.url, body, self.timeout)
        return out or ""

    def _parse(self, raw: str) -> List[str]:
        """从模型输出里取标签：优先 JSON 的 tag/word，退化时按整句揉。"""
        text = _FENCE.sub("", str(raw or "")).strip()
        cand = ""
        extra = ""
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                d = json.loads(m.group(0))
                cand = str(d.get("tag") or "")
                extra = str(d.get("word") or "")
                self.last_see = str(d.get("see") or "")[:120]
            except Exception:  # noqa: BLE001
                cand = ""
        if not cand:
            cand = text
        # 只认词表里的那个（模型可能写「开心（笑）」→ 先找词表命中）
        tags: List[str] = []
        for t in self.tags:
            if t in cand:
                tags.append(t)
                break
        if not tags:
            tags = _lib.normalize_tags(cand)[:1]        # 不在词表也认，但要短
        if extra:
            tags += [t for t in _lib.normalize_tags(extra)[:2] if t not in tags]
        return tags[:3]

    def status(self) -> Dict[str, Any]:
        return {"ok": True, "enabled": self.enabled, "queued": len(self._queue),
                "done": self.done, "failed": self.failed, "skipped": self.skipped,
                "url": self.url, "model": self.model, "last_error": self.last_error}


def _default_post(url: str, body: Dict[str, Any], timeout: float) -> str:
    """同步 HTTP（由 asyncio.to_thread 调起）—— 不引第三方依赖。"""
    import urllib.request

    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310
        d = json.loads(r.read().decode("utf-8"))
    msg = ((d.get("choices") or [{}])[0].get("message")) or {}
    # 思考型模型的正文可能在 reasoning_content 里（本机 Bonsai 生产跑 --reasoning off，双读保险）
    return str(msg.get("content") or msg.get("reasoning_content") or "")
