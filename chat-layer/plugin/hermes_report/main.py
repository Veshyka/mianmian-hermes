"""hermes_report —— AstrBot 侧的「主动回报」接收端（干活门 → 聊天门/主人）。

## 为什么存在（要解决的事）

干活门（Hermes default profile）收到跨门任务书后，A2A 调用一返回就结束；**长任务**
（>3 分钟）只能走两段式：秒回「已接单」+ 报告落文件，但**跑完之后干活门不会主动回来**，
派活方（聊天门/猫猫）只能再派一趟问进度。

本插件在 AstrBot 里开一个**只绑回环**的小 HTTP 口，干活门跑完长任务就 POST 回来，
插件立刻把结果发到主人私聊，并写一条 Hindsight 记忆（tag `worker-report`），
于是「跑完主动回来」不再依赖谁记得去问。

## 接口（只绑 127.0.0.1，外部碰不到）

    POST /report        Header: X-Report-Token: <token>
    body: {"task_id":"t-...", "status":"ok|fail|partial",
           "title":"一句话", "summary":"≤400 字结论",
           "path":"/opt/data/reports/xxx.md"(可空), "source":"worker-door"(可空)}
    → {"ok":true,"sent":true,"duplicate":false,"umo":"...","text":"..."}

    GET /health  → {"ok":true}

## 回执怎么进 pipeline（两条路，一条主一条兜底）

### 主路：以「入站消息」推进 pipeline，跑一轮完整 agent（proactive_inject_enable）

回执到达时，插件**自己合成一条 AstrMessageEvent**，用平台适配器的
`create_event()` + `commit_event()` 塞进 AstrBot 的事件队列（`EventBus` 里那条
`asyncio.Queue`）。从这一刻起它和「NapCat 收到主人私聊消息」走的是**同一条路**：
`EventBus.dispatch` → `PipelineScheduler.execute` → WakingCheck → Whitelist →
SessionStatus → RateLimit → ContentSafety → PreProcess → **ProcessStage（完整
agent + 函数工具）** → ResultDecorate → Respond。

所以不需要主人在场：回执自己会触发一轮带工具的 agent，猫猫可以分析回执、决定下一步
（例如再调 delegate_to_hermes 继续干活）。

源码证据（AstrBot 4.28.1）：

- `astrbot/core/platform/platform.py:147-149`  `Platform.commit_event()` →
  `self._event_queue.put_nowait(event)`（所有适配器的「收到消息」都是走这里，
  见 `sources/aiocqhttp/aiocqhttp_platform_adapter.py:510`
  `self.commit_event(self.create_event(message))`）。
- `astrbot/core/star/star_tools.py:104-134` `StarTools.create_event()`：官方给插件的
  便捷封装，内部就是 `adapter = platform_manager.get_insts()` 里按 id/name 找 →
  `adapter.create_event(abm)` → `adapter.commit_event(event)`，且 `is_wake` 参数注释
  写着「Only wake events receive LLM responses」。它由 `astrbot.api.star` 公开导出
  （`astrbot/api/star/__init__.py:1,7`），是**公开 API**。
- `astrbot/core/event_bus.py:36-49` `dispatch()` 从队列取事件 → `scheduler.execute(event)`。
- `astrbot/core/pipeline/stage_order.py` 阶段顺序（ProcessStage = 完整 agent）。
- `astrbot/core/pipeline/waking_check/stage.py:149-157`：私聊且
  `friend_message_needs_wake_prefix=false`（本机配置 `cmd_config.json:41` 就是 false）
  → 直接 `is_wake = True; is_at_or_wake_command = True`，**不需要唤醒前缀**。
- `astrbot/core/pipeline/process_stage/stage.py:60-69`：`event.is_at_or_wake_command`
  为真 → 走 `agent_sub_stage.process(event)`（带工具的 agent，不是纯聊天）。
- `astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py:492-506`
  `create_event()` 会把 `self.bot`（CQHttp 客户端）挂到事件上，所以**回复能真的发出去**
  （`aiocqhttp_message_event.py:184-196` → `bot.send_private_msg`）。

插件自己调 `context.get_platform_inst(platform_id)` + `adapter.create_event()` +
`adapter.commit_event()` 而不用 `StarTools.create_event()` 的唯一原因：
要拿到 event 句柄好打防循环标记 `set_extra("_hermes_report_injected")`。
两者做的事完全相同（`star_tools.py:126-134`）。

### 兜底：注入「下一轮 LLM 请求」（inject_enable，保留不动）

万一主路失败（平台没连上 / 不是 aiocqhttp / commit 抛错），回执仍然**留在待注入队列**
里；由官方钩子 `@filter.on_llm_request()`（源码 astrbot/core/star/register/
star_handler.py:433，调用点 pipeline/process_stage/method/agent_sub_stages/
internal.py:352）在**下一轮 LLM 请求前**把这批未读回执作为一条 context 消息 append
进 `req.contexts`，追加后立刻标记已读并落盘，避免重复注入。
注入只对「主人私聊那个 umo」生效（target_umo / 最近一次私聊 / default_umo 三者之一）。
主路成功时会把该条回执立刻标记已读，兜底不会重复投递。

## 防循环

1. 注入的入站消息正文里带死标记 `INBOUND_MARK`（「这是自动回执，不是主人发的」），
   纯函数 `is_injected_text()` 一眼能认出来。
2. 注入事件上打 `set_extra("_hermes_report_injected", True)`；
   `@filter.on_llm_request()` 钩子看到这个标记就**直接返回**，不会把同一批回执
   再塞一遍 context。
3. 主路成功后立刻把该条回执标记已读并落盘 → 兜底钩子没东西可注入。
4. 闸门 `proactive_gate()`：两次注入最小间隔 `proactive_min_interval_s`（默认 60s）
   + 每小时上限 `proactive_max_per_hour`（默认 12 次），防「干活门回执 → 猫猫又派活 →
   又来回执」的连环风暴。

## 配置（AstrBot 插件配置 `data/config/hermes_report_config.json`，可留空用默认值）

    listen_host   默认 172.17.0.1（只绑回环）
    listen_port   默认 8098
    target_umo    默认空 → 用「最近一次私聊的 umo」，再兜底 default_umo
    default_umo   默认 aiocqhttp:FriendMessage:<OWNER_QQ>（主人私聊）
    hindsight_url / bank_id   回执记忆写哪（默认本机 Hindsight）
    max_summary   回执摘要截断，默认 400
    max_pending   待注入回执队列上限，默认 20（仅在①开着时有意义）
    inject_enable             ①兜底：是否「回执排队 + 注入下一轮 LLM 请求」，**默认 false（主人决定）**
                              false 时不排队、不注入，只「推主人 QQ + 走②」
    proactive_inject_enable   ②主路：是否把回执当入站消息推进 pipeline，默认 true
    proactive_min_interval_s  ②两次注入最小间隔秒数，默认 60
    proactive_max_per_hour    ②每小时最多注入几次，默认 12

## 设计约束（别踩）

- 插件里任何异常都不能影响 AstrBot 主流程：服务器起不来只记日志，钩子全程 try/except。
- 幂等：同一个 task_id 只发一次（`plugin_data/hermes_report/reports.jsonl` 留痕 + 去重）。
- 隐私：日志只打「条数 / 字数 / umo」，**绝不打印回执正文**。
- 送信证据：`context.send_message()` 的返回值只表示「找到平台」，不保证到达；真正的
  到达证据要去 NapCat 侧查 `get_friend_msg_history`（见 scripts/report_to_chat.sh --verify）。
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.request
import uuid
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register

try:  # v4 里 MessageChain 在这个模块
    from astrbot.core.message.message_event_result import MessageChain
except Exception:  # noqa: BLE001  # pragma: no cover
    from astrbot.api.message_components import MessageChain  # type: ignore

DEFAULTS = {
    "listen_host": "172.17.0.1",
    "listen_port": 8098,
    "target_umo": "",
    "default_umo": "aiocqhttp:FriendMessage:<OWNER_QQ>",
    "hindsight_url": "http://172.17.0.1:8888",
    "bank_id": "mianmian-history",
    "max_summary": 400,
    "max_pending": 20,
    "inject_enable": False,           # 兜底（主人 2026-09-23 决定：关）：把回执注入「下一轮 LLM 请求」
    "proactive_inject_enable": True,  # 主路：把回执当「入站消息」推进 pipeline（跑完整 agent）
    "proactive_min_interval_s": 60,   # 主路两次注入的最小间隔（秒），防连环
    "proactive_max_per_hour": 12,     # 主路每小时最多注入几次，防风暴雨
}
TOKEN_FILE = ".report_token"          # 与插件同目录（宿主 chat-layer/plugin/hermes_report/.report_token）
STATE_DIR = Path("/AstrBot/data/plugin_data/hermes_report")
QUEUE_FILENAME = "pending_reports.jsonl"   # 待注入回执队列（落盘，重启不丢）

# 合成入站消息的固定标记：正文必须以此开头、必须带「自动、非主人」声明。
INBOUND_HEAD = "【干活门回执】"
INBOUND_MARK = "（这是自动回执，不是主人发的）"
# 打在合成事件上的 extra key，供 on_llm_request 钩子识别「这一轮就是注入出来的」，防循环。
INBOUND_EXTRA_KEY = "_hermes_report_injected"
# 合成消息的发送者昵称（session/sender_id 仍是主人，否则 umo 对不上会话）
INBOUND_NICK = "干活门回执(自动)"


def _short(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


# ---------------- 待注入回执队列（纯函数，可脱离 AstrBot 单测） ----------------
# 记录格式（每行一个 JSON）：{"id","task_id","ts","status","title","text","read"}


def queue_load(path: Path | str, max_items: int = 20) -> list[dict]:
    """从 jsonl 读回队列；文件缺失/损坏都返回能读到的部分，绝不抛给调用方。"""
    items: list[dict] = []
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except Exception:  # noqa: BLE001
        return []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(rec, dict) and str(rec.get("text") or "").strip():
            if not rec.get("id"):
                rec["id"] = str(rec.get("task_id") or uuid.uuid4().hex)
            items.append(rec)
    return queue_trim(items, max_items)


def queue_trim(items: list[dict], max_items: int = 20) -> list[dict]:
    """只保留最近 max_items 条（max_items<=0 表示不限制）。"""
    items = list(items)
    if max_items > 0 and len(items) > max_items:
        return items[-max_items:]
    return items


def queue_save(path: Path | str, items: list[dict]) -> None:
    """原子落盘（临时文件 + replace），避免半截文件。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        "".join(json.dumps(it, ensure_ascii=False) + "\n" for it in items),
        encoding="utf-8",
    )
    tmp.replace(path)


def queue_add(items: list[dict], rec: dict, max_items: int = 20) -> list[dict]:
    """入队（同 task_id 已在队里则不重复入队），并按上限裁剪。"""
    tid = str(rec.get("task_id") or "")
    if tid:
        for it in items:
            if str(it.get("task_id") or "") == tid:
                return list(items)
    return queue_trim(list(items) + [rec], max_items)


def queue_unread(items: list[dict]) -> list[dict]:
    """未读（未注入）的回执。"""
    return [it for it in items if not it.get("read")]


def queue_mark_read(items: list[dict], ids: list[str]) -> list[dict]:
    """把指定 id 标记为已读。"""
    want = {str(i) for i in ids}
    out: list[dict] = []
    for it in items:
        if str(it.get("id") or "") in want:
            it = {**it, "read": True, "read_ts": int(time.time())}
        out.append(it)
    return out


def render_injection(items: list[dict]) -> str:
    """把未读回执渲染成注入用的那一段文本（纯函数，便于单测）。"""
    n = len(items)
    head = "【干活门回执·未读 1 条】" if n == 1 else f"【干活门回执·未读 {n} 条】"
    lines = [head]
    for it in items:
        text = str(it.get("text") or "").strip()
        if text:
            lines.append(text)
    lines.append(
        "这是你之前派给干活门（Hermes）的任务回执，主人可能还不知道，"
        "你决定是否告知主人、或继续推进后续步骤（可用你的工具继续干活）。"
        "本条只是系统注入，不必向主人复述它。"
    )
    return "\n".join(lines)


def render_inbound(text: str) -> str:
    """把回执正文渲染成一条「入站消息」的文本（纯函数，便于单测）。

    硬要求：以 INBOUND_HEAD 开头、必须含 INBOUND_MARK 声明「自动回执、不是主人发的」。
    """
    body = (text or "").strip()
    if not body.startswith(INBOUND_HEAD):
        body = INBOUND_HEAD + body
    return f"{body}\n{INBOUND_MARK}"


def is_injected_text(text: str) -> bool:
    """这条文本是不是我们自己合成的入站消息（防循环识别用，纯函数）。"""
    return INBOUND_MARK in (text or "")


def proactive_gate(
    now: float,
    last_ts: float,
    recent_ts: list[float],
    enable: bool = True,
    min_interval_s: int = 60,
    max_per_hour: int = 12,
    window_s: int = 3600,
) -> tuple[bool, str]:
    """主路闸门（纯函数）：返回 (是否允许注入, 原因)。

    - enable=False         → 不开主路
    - 距上次注入 < min_interval_s → 太密，跳过（防「回执→派活→回执」连环）
    - 近 window_s 秒内已注入 >= max_per_hour 次 → 防风暴雨
    """
    if not enable:
        return False, "disabled"
    if min_interval_s > 0 and last_ts and (now - last_ts) < min_interval_s:
        return False, f"too_soon({int(now - last_ts)}s<{min_interval_s}s)"
    hits = [t for t in (recent_ts or []) if now - t < window_s]
    if max_per_hour > 0 and len(hits) >= max_per_hour:
        return False, f"hourly_cap({len(hits)}>={max_per_hour})"
    return True, "ok"


def split_umo(umo: str) -> tuple[str, str, str]:
    """'aiocqhttp:FriendMessage:<OWNER_QQ>' → (platform_id, message_type, session_id)。

    非法 umo 返回 ("", "", "")，绝不抛异常（纯函数）。
    """
    try:
        p, t, s = str(umo).split(":", 2)
        if not p or not t or not s:
            return "", "", ""
        return p, t, s
    except Exception:  # noqa: BLE001
        return "", "", ""


@register("hermes_report", "mianmian", "干活门长任务完成后的主动回报接收端", "0.1.0")
class HermesReport(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        cfg = {**DEFAULTS, **(config or {})}
        self.host = str(cfg["listen_host"])
        self.port = int(cfg["listen_port"])
        self.target_umo = str(cfg["target_umo"] or "")
        self.default_umo = str(cfg["default_umo"] or "")
        self.hindsight = str(cfg["hindsight_url"]).rstrip("/")
        self.bank = str(cfg["bank_id"])
        self.max_summary = int(cfg["max_summary"])
        self.max_pending = max(1, int(cfg.get("max_pending") or 20))
        self.inject_enable = bool(cfg.get("inject_enable", True))
        self.proactive_enable = bool(cfg.get("proactive_inject_enable", True))
        self.proactive_min_interval = int(cfg.get("proactive_min_interval_s") or 0)
        self.proactive_max_per_hour = int(cfg.get("proactive_max_per_hour") or 0)
        self._inject_ts: list[float] = []   # 最近几次主路注入的时间戳（内存即可，重启清零）

        self._token = \"<SECRET>\"()
        self._last_umo = self._read_last_umo()
        self._runner = None
        self._seen: set[str] = set()
        self._pending: list[dict] = []
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[hermes_report] 建状态目录失败 {exc}")
        self._load_seen()
        if self.inject_enable:
            self._pending = queue_load(self._pending_file(), self.max_pending)
        else:
            # 主人明确关掉①：不排队、不注入下一轮 → 队列整体停用，
            # 并把历史队列挪到一边，免得日后重新打开时把陈年回执塞进她的对话里。
            self._pending = []
            self._archive_stale_queue()
        logger.info(
            f"[hermes_report] 就绪 port={self.port} token={'有' if self._token else '无'}"
            f" last_umo={self._last_umo or '(未记录)'}"
            f" 待注入回执={len(queue_unread(self._pending))} 条"
        )
        logger.info(
            f"[hermes_report] 开关① inject_enable={'开' if self.inject_enable else '关'}"
            f"（{'回执排队落盘 + 注入下一轮 LLM 请求' if self.inject_enable else '不排队、不注入下一轮，只推主人 QQ + 走②' }）"
        )
        logger.info(
            f"[hermes_report] 开关② proactive_inject_enable={'开' if self.proactive_enable else '关'}"
            f"（{'回执以入站消息推进 pipeline，跑一轮完整 agent' if self.proactive_enable else '不触发 agent'}）"
            f" 间隔={self.proactive_min_interval}s 上限={self.proactive_max_per_hour}次/小时"
        )

    # ---------------- 状态与令牌 ----------------
    def _read_token(self) -> str:
        try:
            p = Path(__file__).resolve().parent / TOKEN_FILE
            return p.read_text(encoding="utf-8").strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[hermes_report] 读不到 {TOKEN_FILE}：{exc}（将不校验令牌）")
            return ""

    def _umo_file(self) -> Path:
        return STATE_DIR / "last_umo.txt"

    def _read_last_umo(self) -> str:
        try:
            return self._umo_file().read_text(encoding="utf-8").strip()
        except Exception:  # noqa: BLE001
            return ""

    def _log_file(self) -> Path:
        return STATE_DIR / "reports.jsonl"

    def _pending_file(self) -> Path:
        return STATE_DIR / QUEUE_FILENAME

    def _archive_stale_queue(self) -> None:
        """开关①关着时，把历史待注入队列改名归档（只改名不删除，可人工恢复）。"""
        try:
            f = self._pending_file()
            if not f.exists():
                return
            n = len(queue_load(f, 0))
            dst = f.with_name(f"{QUEUE_FILENAME}.disabled-{int(time.time())}")
            f.replace(dst)
            logger.info(
                f"[hermes_report] 开关①关着 → 历史待注入队列 {n} 条已归档为 {dst.name}（不会被注入）"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[hermes_report] 归档历史待注入队列失败 {type(exc).__name__}")

    def _load_seen(self) -> None:
        try:
            for line in self._log_file().read_text(encoding="utf-8").splitlines():
                try:
                    tid = json.loads(line).get("task_id")
                except Exception:  # noqa: BLE001
                    continue
                if tid:
                    self._seen.add(str(tid))
        except Exception:  # noqa: BLE001
            pass

    def _append_log(self, rec: dict) -> None:
        try:
            with self._log_file().open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[hermes_report] 写留痕失败 {exc}")

    # ---------------- 钩子：记住「最近一次私聊」 ----------------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def remember_umo(self, event: AstrMessageEvent):
        """记录最近一次私聊的 umo —— 主动发消息要指定会话，群里不记。"""
        try:
            gid = ""
            get_gid = getattr(event, "get_group_id", None)
            if callable(get_gid):
                gid = str(get_gid() or "")
            if gid:
                return
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            if not umo or umo == self._last_umo:
                return
            self._last_umo = umo
            self._umo_file().write_text(umo, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[hermes_report] 记录 umo 失败 {exc}")

    # ---------------- 钩子：把未读回执注入「下一轮 LLM 请求」 ----------------
    @filter.on_llm_request()
    async def inject_pending_reports(self, event: AstrMessageEvent, req) -> None:
        """AstrBot 官方钩子：LLM 请求发出前调用。

        把「未读的干活门回执」作为一条 context 消息 append 进 req.contexts，
        注入后立即标记已读并落盘，保证只注入一次。
        证据：astrbot/core/star/register/star_handler.py:433（钩子定义）、
        astrbot/core/pipeline/process_stage/method/agent_sub_stages/internal.py:352（调用点，
        在 build_main_agent 之后、真正的 LLM 请求之前）。
        """
        try:
            # 防循环（双保险）：
            #  a) 事件上带我们打的 extra 标记；
            #  b) 事件正文里带「这是自动回执，不是主人发的」这个死标记。
            # 加 (b) 是因为别的插件可能把我们推进去的事件**重新造**一遍
            #（实测 hermes_debounce 会把私聊里这批消息合并成一条新事件，extra 就丢了）。
            if self._is_injected_round(event):
                self._log_inbound_tools(req)
                return
            if not self.inject_enable:
                return
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            if not self._should_inject(umo):
                return
            unread = queue_unread(self._pending)
            if not unread:
                return
            block = render_injection(unread)
            contexts = getattr(req, "contexts", None)
            if contexts is None or not isinstance(contexts, list):
                if contexts is None:
                    req.contexts = []
                    contexts = req.contexts
                else:
                    logger.warning("[hermes_report] req.contexts 非 list，跳过注入")
                    return
            contexts.append({"role": "user", "content": block})
            self._pending = queue_mark_read(
                self._pending, [str(it.get("id") or "") for it in unread]
            )
            try:
                queue_save(self._pending_file(), self._pending)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[hermes_report] 回执队列落盘失败 {type(exc).__name__}")
            # 隐私：只记条数 / 长度 / umo，不记回执正文
            logger.info(
                f"[hermes_report] 已注入未读回执 {len(unread)} 条 / {len(block)} 字"
                f"（umo={umo}），剩余未读 {len(queue_unread(self._pending))} 条"
            )
        except Exception as exc:  # noqa: BLE001 注入失败绝不影响主流程
            logger.warning(f"[hermes_report] 注入回执失败（忽略）{type(exc).__name__}: {exc}")

    @staticmethod
    def _is_injected_round(event) -> bool:
        """这一轮 LLM 请求是不是「回执入站注入」触发的（防循环判定，双保险）。"""
        try:
            getter = getattr(event, "get_extra", None)
            if callable(getter) and getter(INBOUND_EXTRA_KEY):
                return True
        except Exception:  # noqa: BLE001
            pass
        try:
            msg = getattr(event, "get_message_str", None)
            text = msg() if callable(msg) else getattr(event, "message_str", "")
            return is_injected_text(str(text or ""))
        except Exception:  # noqa: BLE001
            return False

    def _log_inbound_tools(self, req) -> None:
        """自证：把「这次入站注入触发的那一轮 agent 实际带了哪些函数工具」记一条日志。

        只记工具**名字**和条数，不记正文（隐私）。名字来自 AstrBot 在
        `build_main_agent` 里挂到 `req.func_tool` 的完整工具集
        （astrbot/core/astr_main_agent.py:599-617，同一处也被
        process_stage/.../internal.py:392 的 trace `astr_agent_prepare` 记录）。
        """
        try:
            ft = getattr(req, "func_tool", None)
            names = list(ft.names()) if ft is not None and callable(getattr(ft, "names", None)) else []
            logger.info(
                f"[hermes_report] 入站注入那一轮的 agent 带了 {len(names)} 个函数工具：{names}"
            )
        except Exception as exc:  # noqa: BLE001 诊断失败不影响任何东西
            logger.warning(f"[hermes_report] 读函数工具清单失败 {type(exc).__name__}")

    def _should_inject(self, umo: str) -> bool:
        """只往「主人那个私聊会话」注入（target_umo / 最近私聊 / default_umo）。"""
        if not umo:
            return False
        allowed = {u for u in (self.target_umo, self._last_umo, self.default_umo) if u}
        return umo in allowed

    # ---------------- 主路：把回执合成「入站消息」推进 pipeline ----------------
    async def push_inbound(self, text: str, umo: str) -> dict:
        """把回执当成「平台收到的一条消息」提交进 AstrBot 事件队列。

        用的是每个适配器自己收消息时用的同一对方法（`Platform.create_event` /
        `Platform.commit_event`，见 astrbot/core/platform/platform.py:147-149 与
        astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py:492-510），
        等价于官方给插件的 StarTools.create_event（astrbot/core/star/star_tools.py:104-134，
        由 astrbot.api.star 公开导出）。

        返回 {"ok": bool, "reason": str, "chars": int}，**不含正文**（隐私）。
        """
        from astrbot.api.platform import AstrBotMessage, MessageMember, MessageType

        platform_id, mtype, session_id = split_umo(umo)
        if not platform_id or not session_id:
            return {"ok": False, "reason": f"bad_umo({umo})", "chars": 0}

        adapter = self.context.get_platform_inst(platform_id) if self.context else None
        if adapter is None:
            return {"ok": False, "reason": f"platform_not_found({platform_id})", "chars": 0}

        inbound = render_inbound(text)
        # 私聊：sender 就是会话主人（否则 on_llm_request 钩子 / umo 对不上）；
        # 群聊：session_id 是群号，sender 需要用真实群成员 —— 本场景只支持私聊。
        if mtype != "FriendMessage":
            return {"ok": False, "reason": f"unsupported_type({mtype})", "chars": 0}

        abm = AstrBotMessage()
        abm.type = MessageType(mtype)
        abm.self_id = self._platform_self_id(adapter)
        abm.session_id = session_id
        abm.message_id = uuid.uuid4().hex
        abm.sender = MessageMember(user_id=session_id, nickname=INBOUND_NICK)
        abm.message = [Plain(inbound)]
        abm.message_str = inbound
        abm.raw_message = None
        abm.timestamp = int(time.time())

        event = adapter.create_event(abm)
        # 防循环标记：on_llm_request 钩子看到它就跳过（这一轮本身就是注入出来的）
        try:
            event.set_extra(INBOUND_EXTRA_KEY, True)
            event.set_extra("_hermes_report_inbound", True)
        except Exception:  # noqa: BLE001
            pass
        # 唤醒检查阶段会自己再判一次；这里先摆好，兼容不走 pipeline 的阶段
        event.is_wake = True
        event.is_at_or_wake_command = True

        adapter.commit_event(event)   # → self._event_queue.put_nowait(event)
        return {"ok": True, "reason": "committed", "chars": len(inbound)}

    @staticmethod
    def _platform_self_id(adapter) -> str:
        """尽量取机器人自己的 QQ；取不到就空串（不影响私聊唤醒与回复）。"""
        for obj in (getattr(adapter, "bot", None), adapter):
            try:
                sid = getattr(obj, "self_id", None)
                if sid:
                    return str(sid)
            except Exception:  # noqa: BLE001
                continue
        return ""

    async def try_proactive_inject(self, text: str, umo: str) -> tuple[bool, str]:
        """闸门 + 主路注入。返回 (是否已注入, 原因)；任何异常都被吞掉并记 reason。"""
        ok, why = proactive_gate(
            time.time(),
            self._inject_ts[-1] if self._inject_ts else 0.0,
            self._inject_ts,
            enable=self.proactive_enable,
            min_interval_s=self.proactive_min_interval,
            max_per_hour=self.proactive_max_per_hour,
        )
        if not ok:
            return False, why
        try:
            res = await self.push_inbound(text, umo)
        except Exception as exc:  # noqa: BLE001 主路失败绝不能影响推送
            return False, f"error({type(exc).__name__})"
        if res.get("ok"):
            self._inject_ts.append(time.time())
            self._inject_ts = self._inject_ts[-64:]
            # 隐私：只打字数 / 条数 / umo，绝不打正文
            logger.info(
                f"[hermes_report] 回执已作为入站消息推进 pipeline：{res['chars']} 字"
                f"（umo={umo}），等她跑完这一轮 agent"
            )
            return True, "ok"
        logger.warning(f"[hermes_report] 入站注入未成行：{res.get('reason')}（umo={umo}）")
        return False, str(res.get("reason"))

    # ---------------- 钩子：启动 HTTP 接收端 ----------------
    @filter.on_astrbot_loaded()
    async def start_server(self) -> None:
        try:
            from aiohttp import web
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[hermes_report] 缺 aiohttp，接收端未启动：{exc}")
            return
        try:
            app = web.Application()
            app.router.add_post("/report", self._handle_report)
            app.router.add_get("/health", self._handle_health)
            self._runner = web.AppRunner(app)
            await self._runner.setup()
            site = web.TCPSite(self._runner, self.host, self.port)
            await site.start()
            logger.info(f"[hermes_report] 接收端已监听 http://{self.host}:{self.port}/report")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[hermes_report] 接收端起不来（不影响 AstrBot）：{exc}")

    # ---------------- HTTP 处理 ----------------
    async def _handle_health(self, request):  # noqa: ANN001
        from aiohttp import web

        return web.json_response(
            {"ok": True, "last_umo": self._last_umo, "seen": len(self._seen),
             "pending_unread": len(queue_unread(self._pending)),
             "pending_total": len(self._pending),
             "inject_enable": bool(self.inject_enable),
             "proactive_inject_enable": bool(self.proactive_enable)}
        )

    async def _handle_report(self, request):  # noqa: ANN001
        from aiohttp import web

        if self._token and request.headers.get("X-Report-Token", "") != self._token:
            \"<SECRET>\" web.json_response({"ok": False, "error": "bad token"}, status=403)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"ok": False, "error": "bad json"}, status=400)
        res = await self.deliver(body)
        return web.json_response(res, status=200 if res.get("ok") else 500)

    # ---------------- 核心：渲染 + 发送 + 留痕 ----------------
    def render(self, body: dict) -> str:
        status = str(body.get("status") or "ok").lower()
        icon = {"ok": "✅", "fail": "❌", "partial": "⚠️"}.get(status, "ℹ️")
        title = _short(str(body.get("title") or "任务"), 80)
        summary = _short(str(body.get("summary") or ""), self.max_summary)
        path = str(body.get("path") or "").strip()
        tid = str(body.get("task_id") or "").strip()
        lines = [f"【干活门回执】{icon} {title}"]
        if summary:
            lines.append(summary)
        if path:
            lines.append(f"长报告：{path}")
        tail = " · ".join(x for x in (f"task {tid}" if tid else "", time.strftime("%H:%M")) if x)
        lines.append(f"（{tail}）")
        return "\n".join(lines)

    async def deliver(self, body: dict) -> dict:
        tid = str(body.get("task_id") or "").strip()
        if tid and tid in self._seen:
            return {"ok": True, "sent": False, "duplicate": True, "text": ""}
        text = self.render(body)
        # 开关①开着才排队（排队是为了「注入下一轮 LLM 请求」这条兜底路）。
        # 关掉时**不排队**，免得 pending 无意义地一直涨。
        if self.inject_enable:
            try:
                n = self.enqueue_report(body, text)
                logger.info(
                    f"[hermes_report] 回执入待注入队列，待注入 {n} 条 / "
                    f"{sum(len(str(i.get('text') or '')) for i in self._pending)} 字"
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[hermes_report] 回执排队失败（不影响推送）{type(exc).__name__}")
        else:
            logger.info(
                f"[hermes_report] 开关①关着 → 回执不进队列（{len(text)} 字），只推主人 QQ + 走②"
            )
        umo = self.target_umo or self._last_umo or self.default_umo
        sent = False
        err = ""
        if umo:
            try:
                sent = bool(await self.context.send_message(umo, MessageChain([Plain(text)])))
            except Exception as exc:  # noqa: BLE001
                err = f"{type(exc).__name__}: {exc}"
                logger.error(f"[hermes_report] 发送失败 {err}")
        else:
            err = "没有可用的会话（target_umo/last_umo/default_umo 都为空）"
        # —— 主路：把回执作为「入站消息」推进 pipeline，触发一轮完整 agent（带工具）——
        injected, inject_why = False, "no_umo"
        if umo:
            injected, inject_why = await self.try_proactive_inject(text, umo)
            if injected:
                # 已经以入站消息送进 pipeline 了，兜底钩子不必再投递这一条
                try:
                    ids = [
                        str(it.get("id") or "")
                        for it in queue_unread(self._pending)
                        if str(it.get("task_id") or "") == tid
                    ]
                    if ids:
                        self._pending = queue_mark_read(self._pending, ids)
                        queue_save(self._pending_file(), self._pending)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[hermes_report] 标记已读失败 {type(exc).__name__}")
        if tid and sent:
            self._seen.add(tid)
        self._append_log(
            {
                "ts": int(time.time()),
                "task_id": tid,
                "status": body.get("status"),
                "title": body.get("title"),
                "path": body.get("path"),
                "umo": umo,
                "sent": sent,
                "error": err,
                "injected": injected,
                "inject_reason": inject_why,
                "text": text,
            }
        )
        if sent:
            asyncio.create_task(asyncio.to_thread(self._remember, text))
        return {"ok": sent, "sent": sent, "duplicate": False, "umo": umo, "text": text,
                "error": err, "injected": injected, "inject_reason": inject_why}

    def enqueue_report(self, body: dict, text: str) -> int:
        """把一条回执放进待注入队列并落盘；返回当前队列条数。

        同 task_id 已在队列里则不重复入队；队列只保留最近 max_pending 条。
        """
        rec = {
            "id": str(body.get("task_id") or "").strip() or uuid.uuid4().hex,
            "task_id": str(body.get("task_id") or "").strip(),
            "ts": int(time.time()),
            "status": str(body.get("status") or "ok"),
            "title": str(body.get("title") or ""),
            "text": text,
            "read": False,
        }
        self._pending = queue_add(self._pending, rec, self.max_pending)
        try:
            queue_save(self._pending_file(), self._pending)
        except Exception as exc:  # noqa: BLE001 落盘失败不影响内存队列与推送
            logger.warning(f"[hermes_report] 回执队列落盘失败 {type(exc).__name__}: {exc}")
        return len(self._pending)

    def _remember(self, text: str) -> None:
        """把回执写一条 Hindsight 记忆（tag worker-report）—— 聊天门 recall 得到。"""
        payload = json.dumps(
            {
                "items": [
                    {
                        "content": text,
                        "document_id": f"worker-report-{int(time.time())}",
                        "tags": ["worker-report", "hermes", "chat"],
                        "metadata": {"source": "worker-door", "via": "astrbot"},
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    }
                ],
                "async": True,
            },
            ensure_ascii=False,
        ).encode()
        req = urllib.request.Request(
            f"{self.hindsight}/v1/default/banks/{self.bank}/memories",
            data=payload,
            headers={"Content-Type": "application/json", "Accept-Encoding": "identity"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                logger.info(f"[hermes_report] 回执入记忆库 {r.status}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[hermes_report] 回执入记忆库失败 {exc}")
