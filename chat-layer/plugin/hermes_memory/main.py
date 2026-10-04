"""hermes_memory：AstrBot ⇄ Hindsight 记忆桥（**一个模块、两个方向**）

    retain（自动回填）  一轮对话结束后 → 把「主人/群友说的 + 棉棉回的」写进 Hindsight
    recall（自动召回）  一轮对话开始前 → 按**当前这条消息**语义检索 → 注入本轮上下文

两个方向共用同一份东西（改一处即可，别复制粘贴出第二套）：
- 端点 / bank / 超时 / 请求头（`HINDSIGHT_URL_DEFAULT`、`BANK_ID_DEFAULT`、`HTTP_HEADERS`）
- HTTP 客户端 `HermesMemory._post_json()`（唯一发请求的地方，retain 与 recall 都走它）
- 守卫 `inspect_round()` + `guard()`（谁的话、多短算噪声、是不是「回执注入轮」）
- 配置合成 `DEFAULTS`（retain_* 与 recall_* 并列），启动日志一行打两边状态

设计约束（别踩）
- 插件里任何异常都不能影响 AstrBot 主流程：钩子全程 try/except，召回失败/超时 → **降级为不注入**。
- 召回只对「主人私聊」生效（`recall_umos`，默认 OWNER_UMO）；群聊/他人不注入。
- 「回执注入轮」不做召回（hermes_report 合成的入站消息不是主人发的，按它对记忆召回无意义）。
- 隐私：日志只打「条数 / 字数 / 耗时 / umo」，**绝不打印记忆正文、也不打印主人原话**。
- 注入角色默认 `user`（与同仓 hermes_report 生产验证过的注入形状一致）；
  召回超时默认 3s（本机 Hindsight budget=low 实测 0.6~0.8s），超时即放弃，宁可这轮没记忆也不拖住回复。

数据流、配置项、降级行为、验证命令：见同目录 `README.md`。
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star, register

# =====================================================================
# 共享常量（retain / recall 只写一处）
# =====================================================================
HINDSIGHT_URL_DEFAULT = "http://172.17.0.1:8888"   # 容器视角：宿主 docker 网关
BANK_ID_DEFAULT = "mianmian-history"                # 与 Hermes 两个门共用同一个 bank
OWNER_UMO_DEFAULT = "aiocqhttp:FriendMessage:<OWNER_QQ>"   # 主人私聊会话
RETAIN_PATH = "/v1/default/banks/{bank}/memories"
RECALL_PATH = "/v1/default/banks/{bank}/memories/recall"
# 少了 Accept-Encoding: identity 时 Hindsight 会走 gzip，容器里偶发读不干净 → 统一带上
HTTP_HEADERS = {"Content-Type": "application/json", "Accept-Encoding": "identity"}
COMMAND_PREFIXES = ("/", "#")                       # 命令类消息不入库、不召回
# 与 hermes_report 约定的防循环标记（只读它的值，不 import 它的模块）
REPORT_INBOUND_EXTRA = "_hermes_report_injected"    # hermes_report 打在合成事件上的 extra key
REPORT_INBOUND_MARK = "（这是自动回执，不是主人发的）"   # hermes_report 合成正文里的死标记
RECALL_EXTRA_KEY = "_hermes_memory_recalled"        # 本轮已注入过（一个回合会有多次 LLM 请求）
RECALL_BLOCK_HEAD = "【自动召回·相关记忆】"
RECALL_PREAMBLE_DEFAULT = "以下是系统按你这条消息从长期记忆库里语义检索到的片段（不是主人这次说的话）。"
RECALL_TAIL_DEFAULT = "用法：自然融进回复，不要复述本段、不要说「我查了记忆」；要更精确的自己用 recall 工具查。"

DEFAULTS = {
    # ---- 共享 ----
    "hindsight_url": HINDSIGHT_URL_DEFAULT,
    "bank_id": BANK_ID_DEFAULT,
    "timeout": 30,                  # 写入（retain）请求超时；异步不等结果，给足即可
    # ---- retain（自动回填） ----
    "retain_enable": True,
    "retain_mode": "both",          # both | private_only（兼容老键 mode）
    "retain_min_chars": 4,          # 主人原话短于这个字数不入库（"。。"这种不入）
    "retain_max_chars": 2000,       # 单条上限，超了截断
    "retain_skip_report_inbound": False,   # 回执注入轮是否也不入库（默认照旧入库，不动老行为）
    # ---- recall（自动召回） ----
    "recall_enable": True,
    "recall_umos": [OWNER_UMO_DEFAULT],    # 只对这些会话注入；留空=用默认主人 umo；["*"]=全部（不推荐）
    "recall_min_chars": 2,          # 短于这个字数不做召回（"在吗"就不查了）
    "recall_budget": "low",         # Hindsight 检索力度 low|mid|high（本机实测 low 已 0.6~0.8s）
    "recall_top_k": 8,              # 最多注入几条
    "recall_min_score": 0.0,        # 低于这个 scores.final 丢掉；0=不过滤（本机实测 final 量级 0.6~1.6）
    "recall_max_tokens": 1024,      # 传给 Hindsight 的结果预算
    "recall_max_chars": 1200,       # 注入块「条目部分」字数上限，超了丢尾
    "recall_query_max_chars": 800,  # 查询串截断（对齐 Hermes 的 recall_max_input_chars）
    "recall_timeout_s": 3.0,        # 硬超时：超了这轮就不注入（降级，不拖回复）
    "recall_role": "user",          # 注入进 req.contexts 的角色
    "recall_preamble": "",          # 留空用 RECALL_PREAMBLE_DEFAULT
    "recall_tags": "",              # 逗号分隔；留空=不过滤（过滤会漏掉 legacy/其它门写的记忆）
    "recall_tags_match": "any",
    "recall_types": "",             # 逗号分隔；留空=不限类型（observation/world/experience 都要）
}


# =====================================================================
# 共享工具：一轮对话的体检 + 守卫（retain 与 recall 都走这里）
# =====================================================================
def is_noise(text: str, min_chars: int) -> bool:
    """噪声判定：空 / 太短 / 命令。retain 与 recall 共用。"""
    t = (text or "").strip()
    if len(t) < max(1, int(min_chars)):
        return True
    return t.startswith(COMMAND_PREFIXES)


def inspect_round(event) -> dict:
    """把一轮对话里两个方向都要用的字段一次性取出来（纯读，绝不抛）。"""
    info = {
        "umo": "", "text": "", "is_group": False, "gid": "",
        "sender": "", "is_report_inbound": False,
    }
    try:
        info["umo"] = str(getattr(event, "unified_msg_origin", "") or "")
    except Exception:  # noqa: BLE001
        pass
    try:
        info["text"] = str(getattr(event, "message_str", "") or "").strip()
    except Exception:  # noqa: BLE001
        pass
    try:
        get_gid = getattr(event, "get_group_id", None)
        if callable(get_gid):
            info["gid"] = str(get_gid() or "")
            info["is_group"] = bool(info["gid"])
    except Exception:  # noqa: BLE001
        pass
    try:
        get_sender = getattr(event, "get_sender_name", None)
        if callable(get_sender):
            info["sender"] = str(get_sender() or "").strip()
    except Exception:  # noqa: BLE001
        pass
    info["sender"] = info["sender"] or ("群里某人" if info["is_group"] else "主人")
    # 「回执注入轮」：双保险认（extra 标记 + 正文死标记），因为 hermes_debounce 会把事件重造一遍、extra 会丢
    try:
        getter = getattr(event, "get_extra", None)
        if callable(getter) and getter(REPORT_INBOUND_EXTRA):
            info["is_report_inbound"] = True
    except Exception:  # noqa: BLE001
        pass
    if not info["is_report_inbound"] and REPORT_INBOUND_MARK in info["text"]:
        info["is_report_inbound"] = True
    return info


def guard(
    info: dict,
    *,
    enable: bool = True,
    allow_umos=(),
    enforce_umos: bool = True,
    allow_groups: bool = True,
    min_chars: int = 4,
    skip_report_inbound: bool = False,
) -> tuple[bool, str]:
    """共享守卫（纯函数）：返回 (是否放行, 原因)。retain 与 recall 语义不同的地方用参数表达。

    enforce_umos=False 表示这一路不按会话白名单限（retain 要存群聊，就传 False）。
    """
    if not enable:
        return False, "disabled"
    if skip_report_inbound and info.get("is_report_inbound"):
        return False, "report_inbound"
    if is_noise(info.get("text", ""), min_chars):
        return False, "noise"
    if info.get("is_group") and not allow_groups:
        return False, "group_not_allowed"
    if not enforce_umos:
        return True, "ok"
    rules = [str(u).strip() for u in (allow_umos or []) if str(u).strip()]
    if not rules:
        rules = [OWNER_UMO_DEFAULT]           # 留空 = 用默认主人 umo
    if "*" in rules:
        return True, "any_umo"
    if str(info.get("umo", "")) not in rules:
        return False, "umo_not_allowed"
    return True, "ok"


def render_recall_block(items: list, preamble: str = "", max_chars: int = 1200) -> str:
    """把召回结果拼成注入块（纯函数）。items = [(text, score[, type]), ...]，已按分数降序。"""
    lines, used = [], 0
    for item in items or []:
        text = str((item[0] if isinstance(item, (list, tuple)) and item else item) or "").strip()
        if not text:
            continue
        line = f"- {text}"
        if lines and used + len(line) + 1 > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    if not lines:
        return ""
    head = (preamble or "").strip() or RECALL_PREAMBLE_DEFAULT
    return f"{RECALL_BLOCK_HEAD}\n{head}\n" + "\n".join(lines) + f"\n{RECALL_TAIL_DEFAULT}"


def _plain(resp: LLMResponse) -> str:
    rc = getattr(resp, "result_chain", None)
    if rc is None:
        return ""
    for meth in ("get_plain_text", "get_message_str", "get_plain_text_str"):
        fn = getattr(rc, meth, None)
        if callable(fn):
            try:
                return (fn() or "").strip()
            except Exception:  # noqa: BLE001
                continue
    return ""


def _csv(value) -> list:
    """配置里的逗号分隔串 → list（也接受真 list）。"""
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [p.strip() for p in str(value or "").split(",") if p.strip()]


@register("hermes_memory", "mianmian", "对话与 Hindsight 记忆库双向自动同步（retain 回填 + recall 召回注入）", "1.1.0")
class HermesMemory(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        cfg = {**DEFAULTS, **(config or {})}
        # ---- 共享 ----
        self.api = str(cfg["hindsight_url"]).rstrip("/")
        self.bank = str(cfg["bank_id"])
        self.timeout = int(cfg["timeout"])
        # ---- retain（兼容老键 min_chars / max_chars） ----
        self.retain_enable = bool(cfg.get("retain_enable", True))
        self.mode = str(cfg.get("retain_mode", cfg.get("mode", "both")))
        self.retain_min_chars = int(cfg.get("retain_min_chars", cfg.get("min_chars", 4)))
        self.retain_max_chars = int(cfg.get("retain_max_chars", cfg.get("max_chars", 2000)))
        self.retain_skip_report_inbound = bool(cfg.get("retain_skip_report_inbound", False))
        # ---- recall ----
        self.recall_enable = bool(cfg.get("recall_enable", True))
        self.recall_umos = _csv(cfg.get("recall_umos")) or [OWNER_UMO_DEFAULT]
        self.recall_min_chars = int(cfg.get("recall_min_chars", 2))
        self.recall_budget = str(cfg.get("recall_budget", "low"))
        self.recall_top_k = int(cfg.get("recall_top_k", 8))
        self.recall_min_score = float(cfg.get("recall_min_score", 0.0))
        self.recall_max_tokens = int(cfg.get("recall_max_tokens", 1024))
        self.recall_max_chars = int(cfg.get("recall_max_chars", 1200))
        self.recall_query_max_chars = int(cfg.get("recall_query_max_chars", 800))
        self.recall_timeout_s = float(cfg.get("recall_timeout_s", 3.0))
        self.recall_role = str(cfg.get("recall_role", "user"))
        self.recall_preamble = str(cfg.get("recall_preamble", "") or "")
        self.recall_tags = _csv(cfg.get("recall_tags"))
        self.recall_tags_match = str(cfg.get("recall_tags_match", "any"))
        self.recall_types = _csv(cfg.get("recall_types"))
        self._inflight: set[asyncio.Task] = set()
        # 一行把两个方向的状态都打出来（复核用）
        logger.info(
            f"[hermes_memory] 就绪 bank={self.bank} api={self.api} timeout={self.timeout}s"
            f" | retain={'on' if self.retain_enable else 'off'}"
            f"(scope={self.mode},min={self.retain_min_chars},max={self.retain_max_chars},"
            f"skip_report={self.retain_skip_report_inbound})"
            f" | recall={'on' if self.recall_enable else 'off'}"
            f"(umo={len(self.recall_umos)},budget={self.recall_budget},top_k={self.recall_top_k},"
            f"min_score={self.recall_min_score},chars={self.recall_max_chars},"
            f"timeout={self.recall_timeout_s}s,role={self.recall_role})"
        )

    # ---------- 唯一发请求的地方（retain 与 recall 共用） ----------
    def _post_json(self, path_tpl: str, body: dict, timeout: float) -> tuple[bool, str, dict]:
        """POST 到 Hindsight。返回 (成功?, 人类可读结果/错误, 解析后的 JSON)。绝不抛。"""
        url = f"{self.api}{path_tpl.format(bank=self.bank)}"
        data = json.dumps(body, ensure_ascii=False).encode()
        req = urllib.request.Request(url, data=data, headers=dict(HTTP_HEADERS), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                try:
                    return True, str(r.status), json.loads(raw or b"{}")
                except Exception:  # noqa: BLE001
                    return True, str(r.status), {}
        except urllib.error.HTTPError as e:
            try:
                detail = e.read()[:200]
            except Exception:  # noqa: BLE001
                detail = b""
            return False, f"HTTP {e.code} {detail!r}", {}
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}", {}

    # ---------- 方向一：retain（写） ----------
    def _retain(self, content: str, doc_id: str, tags: list, meta: dict) -> None:
        body = {
            "items": [{
                "content": content,
                "document_id": doc_id,
                "metadata": meta,
                "tags": tags,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }],
            "async": True,   # 不阻塞：本地抽取要几十秒~几分钟
        }
        ok, msg, _ = self._post_json(RETAIN_PATH, body, self.timeout)
        if ok:
            logger.info(f"[hermes_memory] 入队 {msg} doc={doc_id} len={len(content)}")
        else:
            logger.warning(f"[hermes_memory] 入队失败 {msg}")

    # ---------- 方向二：recall（读） ----------
    def _recall(self, query: str) -> tuple[list, str]:
        """同步召回一次。返回 ([(text, score), ...], 说明/错误)。绝不抛。"""
        body = {"query": query, "budget": self.recall_budget, "max_tokens": self.recall_max_tokens}
        if self.recall_tags:
            body["tags"] = self.recall_tags
            body["tags_match"] = self.recall_tags_match
        if self.recall_types:
            body["types"] = self.recall_types
        ok, msg, data = self._post_json(RECALL_PATH, body, max(1.0, self.recall_timeout_s))
        if not ok:
            return [], msg
        items = []
        for r in (data.get("results") or []):
            text = str(r.get("text") or "").strip()
            if not text:
                continue
            scores = r.get("scores") or {}
            try:
                score = float(scores.get("final", scores.get("semantic", 0.0)))
            except Exception:  # noqa: BLE001
                score = 0.0
            if self.recall_min_score > 0 and score < self.recall_min_score:
                continue
            items.append((text, score, str(r.get("type") or "")))
        items.sort(key=lambda x: x[1], reverse=True)
        return items[: self.recall_top_k], "ok"

    # ---------- 钩子①：一轮结束 → 自动回填 ----------
    @filter.on_agent_done()
    async def on_done(self, event: AstrMessageEvent, run_context, response: LLMResponse) -> None:
        try:
            info = inspect_round(event)
            ok, why = guard(
                info,
                enable=self.retain_enable,
                enforce_umos=False,                             # retain 不按会话白名单限（群聊也存）
                allow_groups=(self.mode != "private_only"),
                min_chars=self.retain_min_chars,
                skip_report_inbound=self.retain_skip_report_inbound,
            )
            if not ok:
                return
            reply = _plain(response)
            if not reply:
                return

            head = f"[群 {info['gid']}] {info['sender']} 说：" if info["is_group"] else "主人说："
            content = (
                f"{head}{info['text'][: self.retain_max_chars]}\n"
                f"棉棉回：{reply[: self.retain_max_chars]}"
            )
            tags = ["hermes", "chat"] + (["group"] if info["is_group"] else [])
            meta = {
                "source": "hermes-chat",
                "platform": "aiocqhttp",
                "chat_type": "group" if info["is_group"] else "private",
                "chat_id": info["gid"] or info["umo"],
                "speaker": info["sender"],
                "via": "astrbot",
            }
            doc_id = f"astrbot-{info['umo']}-{int(time.time())}"
            task = asyncio.create_task(asyncio.to_thread(self._retain, content, doc_id, tags, meta))
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[hermes_memory] retain 钩子异常 {type(e).__name__}: {e}")

    # ---------- 钩子②：请求发出前 → 自动召回 + 注入本轮上下文 ----------
    @filter.on_llm_request()
    async def inject_recalled(self, event: AstrMessageEvent, req) -> None:
        """官方钩子（astrbot/core/star/register/star_handler.py 定义，调用点
        core/pipeline/process_stage/method/agent_sub_stages/internal.py 的
        `call_event_hook(event, EventType.OnLLMRequestEvent, req)`，位于 build_main_agent 之后、
        真发请求之前）——此时原地 append `req.contexts` 生效，且它在「当前这条用户消息」之前。

        返回 None 表示不拦截（返回真值会让 AstrBot 直接 return 掉这轮请求）。
        """
        try:
            if not self.recall_enable:
                return
            if self._already_recalled(event):
                return
            info = inspect_round(event)
            ok, why = guard(
                info,
                enable=True,
                allow_umos=self.recall_umos,
                allow_groups=False,                 # 召回只给主人私聊
                min_chars=self.recall_min_chars,
                skip_report_inbound=True,           # 回执注入轮不召回
            )
            if not ok:
                logger.debug(f"[hermes_memory] 召回跳过 reason={why}")
                return

            query = info["text"][: self.recall_query_max_chars]
            t0 = time.time()
            try:
                items, note = await asyncio.wait_for(
                    asyncio.to_thread(self._recall, query), timeout=self.recall_timeout_s
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"[hermes_memory] 召回超时 >{self.recall_timeout_s}s，本轮降级为不注入"
                    f" umo={info['umo']}"
                )
                return
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[hermes_memory] 召回异常 {type(e).__name__}: {e}")
                return
            if note != "ok":
                logger.warning(f"[hermes_memory] 召回失败 {note}（本轮降级为不注入）")
                return
            if not items:
                logger.info(f"[hermes_memory] 召回 0 条，不注入 umo={info['umo']}")
                return

            block = render_recall_block(items, self.recall_preamble, self.recall_max_chars)
            if not block:
                return
            contexts = getattr(req, "contexts", None)
            if contexts is None:
                req.contexts = []
                contexts = req.contexts
            if not isinstance(contexts, list):
                logger.warning("[hermes_memory] req.contexts 非 list，跳过注入")
                return
            if any(RECALL_BLOCK_HEAD in str(c.get("content", "")) for c in contexts if isinstance(c, dict)):
                self._mark_recalled(event)          # 已经在上下文里了（同回合的后续请求），别重复塞
                return
            contexts.append({"role": self.recall_role, "content": block})
            self._mark_recalled(event)
            types = {}
            for _t, _s, _k in items:
                types[_k] = types.get(_k, 0) + 1
            logger.info(
                f"[hermes_memory] 召回注入 n={len(items)} chars={len(block)}"
                f" top={items[0][1]:.2f} types={types} 用时={time.time() - t0:.2f}s umo={info['umo']}"
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[hermes_memory] recall 钩子异常 {type(e).__name__}: {e}")

    # ---------- 一个回合只注入一次（工具循环里钩子会被反复调用） ----------
    @staticmethod
    def _already_recalled(event) -> bool:
        try:
            getter = getattr(event, "get_extra", None)
            return bool(callable(getter) and getter(RECALL_EXTRA_KEY))
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _mark_recalled(event) -> None:
        try:
            setter = getattr(event, "set_extra", None)
            if callable(setter):
                setter(RECALL_EXTRA_KEY, True)
        except Exception:  # noqa: BLE001
            pass
