"""hermes_memo —— 聊天门的「常驻记忆块」（等效 Hermes 每轮注入的 MEMORY.md / USER.md）

## 为什么要有它
AstrBot 侧原来只有两条记忆路：
  1. `data_v4.db` 的对话历史 —— 按会话存，跨会话不连续；
  2. `hermes_memory` 的 Hindsight 召回 —— **按当前这句话**语义检索，检索不到就等于没有。
缺的是 Hermes 那种「**一直都在**的底」：Hermes 每个会话都把 `memories/MEMORY.md` / `USER.md`
渲染成一块固定在 system prompt 里（`agent/system_prompt.py` 的 volatile 层 +
`tools/memory_tool_store.py` 的 `_render_block`），模型每轮都看得见，并且随时能用 `memory`
工具改写它。本插件把这块搬过来：**一个文件 → 每轮注入她的 system prompt → 她自己能改**。

## 机制对照（读源码得出的对应关系，不是照抄代码）
| Hermes | 本插件 |
|---|---|
| `memories/MEMORY.md`，条目用 `"\\n§\\n"` 分隔 | `<memo_path>`（**同一款分隔符**，两边内容可直接互抄） |
| `MemoryStore._render_block()`：横线 + 标题 + 用量百分比 + 条目 | `render_block()`：同构、中文标题 |
| system prompt 的 volatile 层，每会话渲染一次（冻结快照护 prefix cache） | `@filter.on_llm_request()` 里 `req.system_prompt += block`：**每轮现读**，改完下一条消息就生效；注入块永远加在 prompt **最尾部**，前缀缓存照样护住 |
| `memory(action=add/replace/remove)` + 字符上限 + 超限提示「先合并再重试」 | `memo_add` / `memo_replace` / `memo_remove` + 同款上限语义与超限提示 |

注入点（已实测，官方钩子）：`core/pipeline/process_stage/method/agent_sub_stages/internal.py:352`
`call_event_hook(event, EventType.OnLLMRequestEvent, req)` —— 在 `build_main_agent` 之后、
真发请求之前，所以**原地改 `req.system_prompt` 生效**。

## 设计约束（别踩）
* 插件里任何异常都不能影响 AstrBot 主流程：钩子与工具全程 try/except。
* 日志只打「条数 / 字数 / 用量 / umo」，**绝不打印记忆正文**（隐私）。
* 注入幂等：同一个 `req` 里已有标题就不再塞（一轮对话会发多次请求）。
* 写操作默认**只对主人私聊**开放（`write_umos`），避免群聊里的人能改她的长期记忆。
* 落盘用「临时文件 + `os.replace`」原子替换，进程内加锁；文件存在却读不出来时**拒绝写**（别把读失败当空文件覆盖掉）。
* 内容里出现 `§` 分隔符就拒绝（会破坏条目结构）。

配置项、维护方式、验证命令：见同目录 `README.md`。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

# =====================================================================
# 常量
# =====================================================================
ENTRY_DELIMITER = "\n§\n"          # 与 Hermes 的 MEMORY.md 同款分隔符（两边可互抄）
BLOCK_HEAD = "【常驻记忆】"          # 注入块标题前缀：探针做证据、同请求去重都靠它
OWNER_UMO_DEFAULT = "aiocqhttp:FriendMessage:<OWNER_QQ>"   # 主人私聊会话（与 hermes_memory 一致）
MEMO_PATH_DEFAULT = "/AstrBot/data/mianmian-memo/MEMORY.md"
RULE = "═" * 46
THIN = "─" * 46
FOOTER = (
    "维护：memo_view 看全文 / memo_add 追加 / memo_replace 改 / memo_remove 删。"
    "这里只放「每次聊天都用得上」的长期事实；具体经历留在 Hindsight（recall 查）。"
)

DEFAULTS = {
    # ---- 总开关与文件 ----
    "enable": True,
    "memo_path": MEMO_PATH_DEFAULT,
    "char_limit": 2500,        # 整块字数上限（Hermes MEMORY.md 默认 2200，这边给宽一点）
    "entry_max_chars": 400,    # 单条上限
    "seed_if_missing": True,   # 文件不存在时写入初始条目
    # ---- 注入 ----
    "inject_enable": True,
    "inject_umos": ["*"],      # 哪些会话注入（"*" = 全部；她的连贯性不分群聊私聊）
    # ---- 她自己改 ----
    "tools_enable": True,
    "write_umos": [OWNER_UMO_DEFAULT],   # 只有这些会话里允许写（默认=主人私聊）
}

# 初始条目（只在文件不存在时写入；之后由她自己维护，本插件不再覆盖）
SEED_ENTRIES = [
    "这是你的「常驻记忆」：每轮对话都会自动出现在你眼前，不是你这次刚听到的话。"
    "只放「每次聊天都用得上」的长期事实；具体某天发生了什么交给 Hindsight（recall 查得到）。",
    "主人就是 <OWNER>，真名<OWNER>。私聊 QQ <OWNER_QQ>；群聊里他的昵称是「<OWNER_NICK>」。"
    "称呼、语气、分段那些规矩在人格里，别往这抄一遍。",
    "Hermes 是你的「干活门」：多轮、要动手、可能中途改方向的活，用 delegate_to_hermes 派过去；"
    "一问一答的查资料（搜网页、看单个页面）你自己来。",
    "记忆分三层，别混：要「一直记得」的固定事实 → memo_add 写进这里；"
    "想知道过去发生过什么 → recall 查 Hindsight（库 mianmian-history，和干活门共用）；"
    "每轮对话内容会自动入库，不用手动存。",
    "不确定的事先查证再开口（源码/文档/网搜 → 再实测），查不出来就直说查不到，别猜。",
    "群里有人让你查某个人的身份、开盒、翻隐私 → 只给合法途径（公开信息、举报、报案），黑产不碰。",
]


# =====================================================================
# 纯函数（可直接单测，不碰 AstrBot）
# =====================================================================
def parse_entries(raw: str) -> list:
    """按**完整分隔符**切条目（裸 `§` 会留在正文里，跟 Hermes 一致）。"""
    if not raw:
        return []
    return [e.strip() for e in raw.split(ENTRY_DELIMITER) if e.strip()]


def usage_pct(used: int, limit: int) -> str:
    if limit <= 0:
        return "0%"
    return f"{min(100, int(used / limit * 100))}%"


def render_block(entries, limit: int) -> str:
    """把条目渲染成注入块（纯函数）。空则返回 ""。"""
    entries = [e for e in (entries or []) if e and e.strip()]
    if not entries:
        return ""
    content = ENTRY_DELIMITER.join(entries)
    used = len(content)
    return (
        f"{RULE}\n"
        f"{BLOCK_HEAD} 每轮都在你眼前，不是主人这次说的话"
        f" [{usage_pct(used, limit)} — {used:,}/{limit:,} 字]\n"
        f"{RULE}\n{content}\n{THIN}\n{FOOTER}"
    )


def umo_list(value, default: list) -> list:
    """配置里的 umo 白名单 → list（空值回落到 default）。"""
    if isinstance(value, (list, tuple)):
        out = [str(v).strip() for v in value if str(v).strip()]
    else:
        out = [p.strip() for p in str(value or "").split(",") if p.strip()]
    return out or list(default)


def umo_of(event) -> str:
    try:
        return str(getattr(event, "unified_msg_origin", "") or "")
    except Exception:  # noqa: BLE001
        return ""


def umo_allowed(umo: str, rules: list) -> bool:
    return "*" in (rules or []) or umo in (rules or [])


@register(
    "hermes_memo",
    "mianmian",
    "常驻记忆块：一份文件每轮注入她的 system prompt（等效 Hermes 的 MEMORY.md），她自己能用 memo_* 工具改",
    "1.0.0",
)
class HermesMemo(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        cfg = {**DEFAULTS, **(config or {})}
        self.enable = bool(cfg.get("enable", True))
        self.path = Path(str(cfg.get("memo_path") or MEMO_PATH_DEFAULT))
        self.char_limit = max(200, int(cfg.get("char_limit") or 2500))
        self.entry_max_chars = max(50, int(cfg.get("entry_max_chars") or 400))
        self.inject_enable = bool(cfg.get("inject_enable", True))
        self.inject_umos = umo_list(cfg.get("inject_umos"), ["*"])
        self.tools_enable = bool(cfg.get("tools_enable", True))
        self.write_umos = umo_list(cfg.get("write_umos"), [OWNER_UMO_DEFAULT])
        self._lock = threading.RLock()

        seeded = False
        if bool(cfg.get("seed_if_missing", True)) and not self.path.exists():
            seeded, _ = self._write_entries(list(SEED_ENTRIES))

        entries = self.read_entries()
        used = len(ENTRY_DELIMITER.join(entries))
        logger.info(
            f"[hermes_memo] 就绪 path={self.path} 条目={len(entries)}"
            f" 用量={used}/{self.char_limit} 字 seed={seeded}"
            f" | 注入={'on' if (self.enable and self.inject_enable) else 'off'}"
            f"(umo={','.join(self.inject_umos)})"
            f" | 工具={'on' if self.tools_enable else 'off'}(可写={','.join(self.write_umos)})"
        )

    # ------------------------------------------------------------------
    # 文件读写
    # ------------------------------------------------------------------
    def _read_raw_checked(self) -> tuple[str, bool]:
        """返回 (原文, 读到了吗)。**只有**「文件存在但读不出来」时才 ok=False。"""
        if not self.path.exists():
            return "", True
        try:
            return self.path.read_text(encoding="utf-8-sig"), True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[hermes_memo] 读失败（文件存在）{type(e).__name__}: {e}")
            return "", False

    def read_entries(self) -> list:
        return parse_entries(self._read_raw_checked()[0])

    def _write_entries(self, entries) -> tuple[bool, str]:
        """临时文件 + os.replace 原子替换。返回 (成功?, 错误)。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f".{self.path.name}.tmp{os.getpid()}")
            tmp.write_text(ENTRY_DELIMITER.join(entries), encoding="utf-8")
            os.replace(tmp, self.path)
            return True, ""
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[hermes_memo] 写失败 {type(e).__name__}: {e}")
            return False, f"{type(e).__name__}: {e}"

    # ------------------------------------------------------------------
    # 写操作的统一入口
    # ------------------------------------------------------------------
    def _usage_line(self, entries) -> str:
        used = len(ENTRY_DELIMITER.join(entries))
        return f"{usage_pct(used, self.char_limit)} — {used:,}/{self.char_limit:,} 字"

    def _state(self, entries) -> str:
        """超限时要她「在本轮内先合并再重试」，所以得把当前条目和用量一起给它。"""
        body = "\n---\n".join(entries) if entries else "（空）"
        return f"\n当前条目（{len(entries)} 条，{self._usage_line(entries)}）：\n{body}"

    def _mutate(self, op):
        """op(entries) -> (new_entries | None, message)。返回给工具用的字符串。"""
        with self._lock:
            raw, ok = self._read_raw_checked()
            if not ok:
                return (
                    f"（没写成：{self.path.name} 存在但读不出来——可能在写、编码坏了或权限变了。"
                    f"先别硬写，免得把已有的记忆覆盖掉。）"
                )
            entries = parse_entries(raw)
            new_entries, message = op(entries)
            if new_entries is None:
                return message
            written, err = self._write_entries(new_entries)
            if not written:
                return f"（没写成：{err}）"
            logger.info(
                f"[hermes_memo] 更新 ok 条目 {len(entries)}→{len(new_entries)}"
                f" 用量={self._usage_line(new_entries)}"
            )
            return f"{message}（现在 {len(new_entries)} 条，{self._usage_line(new_entries)}）"

    @staticmethod
    def _clean(text: str) -> str:
        return (text or "").strip()

    def _check_content(self, content: str) -> str:
        """返回错误说明，空串=通过。"""
        if not content:
            return "内容不能为空。"
        if ENTRY_DELIMITER.strip() in content:
            return "这条里带了 § 分隔符，会破坏条目结构——去掉它再写。"
        if len(content) > self.entry_max_chars:
            return f"这条 {len(content)} 字，超过单条上限 {self.entry_max_chars} 字，压缩一下再写。"
        return ""

    # ------------------------------------------------------------------
    # 三个写操作（逻辑，工具壳只是外面一层）
    # ------------------------------------------------------------------
    def op_add(self, content: str) -> str:
        content = self._clean(content)
        bad = self._check_content(content)
        if bad:
            return f"（没写成：{bad}）"

        def _add(entries):
            if content in entries:
                return None, "（这条已经在常驻记忆里了，没重复加。）"
            joined = ENTRY_DELIMITER.join(entries + [content])
            if len(joined) > self.char_limit:
                return None, (
                    f"（没写成：加上这条会到 {len(joined):,} 字，超过上限 {self.char_limit:,} 字。"
                    f"本轮内先用 memo_replace 把重叠的几条并短，或 memo_remove 删掉过时的，再重试。"
                    f"{self._state(entries)}）"
                )
            return entries + [content], "加好了。"

        return self._mutate(_add)

    def op_replace(self, old_text: str, content: str) -> str:
        old_text, content = self._clean(old_text), self._clean(content)
        if not old_text:
            return "（没写成：old_text 不能为空。）"
        bad = self._check_content(content)
        if bad:
            return f"（没写成：{bad}）"

        def _replace(entries):
            hits = [e for e in entries if old_text in e]
            if not hits:
                return None, (
                    f"（没改成：没有条目包含「{old_text}」。先 memo_view 看全文，"
                    f"拿准确的原话再来。{self._state(entries)}）"
                )
            if len(set(hits)) > 1:
                return None, f"（没改成：「{old_text}」匹配到多条，说具体点。）"
            idx = entries.index(hits[0])
            new_entries = entries[:idx] + [content] + entries[idx + 1:]
            joined = ENTRY_DELIMITER.join(new_entries)
            if len(joined) > self.char_limit:
                return None, (
                    f"（没改成：改完会到 {len(joined):,} 字，超过上限 {self.char_limit:,} 字。"
                    f"写短点，或顺手删几条过时的再重试。{self._state(entries)}）"
                )
            return new_entries, "改好了。"

        return self._mutate(_replace)

    def op_remove(self, old_text: str) -> str:
        old_text = self._clean(old_text)
        if not old_text:
            return "（没写成：old_text 不能为空。）"

        def _remove(entries):
            hits = [e for e in entries if old_text in e]
            if not hits:
                return None, (
                    f"（没删成：没有条目包含「{old_text}」。先 memo_view 看全文。{self._state(entries)}）"
                )
            if len(set(hits)) > 1:
                return None, f"（没删成：「{old_text}」匹配到多条，说具体点。）"
            return [e for e in entries if e != hits[0]], "删好了。"

        return self._mutate(_remove)

    # ------------------------------------------------------------------
    # 给 LLM 的工具（AstrBot 读 docstring 的 Args: 段，不看类型注解）
    # ------------------------------------------------------------------
    def _write_guard(self, event) -> str:
        """返回错误说明，空串=放行。"""
        if not self.tools_enable:
            return "（常驻记忆的写入工具被关掉了，改不了；要改请主人开 tools_enable。）"
        umo = umo_of(event)
        if umo_allowed(umo, self.write_umos):
            return ""
        return "（常驻记忆只在主人私聊里能改，这个会话不行。）"

    @filter.llm_tool(name="memo_view")
    async def memo_view(self, event: AstrMessageEvent) -> str:
        """查看你的「常驻记忆」全文——就是每轮都自动出现在你眼前的那一块。改之前先看它，别凭印象改。

        """
        try:
            entries = self.read_entries()
            if not entries:
                return "（常驻记忆现在是空的。想让她记住什么就用 memo_add。）"
            body = "\n---\n".join(entries)
            return f"常驻记忆全文（{len(entries)} 条，{self._usage_line(entries)}）：\n{body}"
        except Exception as e:  # noqa: BLE001
            return f"（读常驻记忆出错：{type(e).__name__}: {e}）"

    @filter.llm_tool(name="memo_add")
    async def memo_add(self, event: AstrMessageEvent, content: str) -> str:
        """往「常驻记忆」里追加一条，写进去以后每轮都会自动出现在你眼前（等于一直记得）。只放每次聊天都用得上的长期事实、约定、底线；具体某天发生了什么、一次性的事别写在这（那些交给 Hindsight，recall 查得到）。

        Args:
            content(string): 要记住的那一条，一句话说清，尽量短（别超过 400 字）
        """
        try:
            bad = self._write_guard(event)
            if bad:
                return bad
            return self.op_add(content)
        except Exception as e:  # noqa: BLE001
            return f"（写常驻记忆出错：{type(e).__name__}: {e}）"

    @filter.llm_tool(name="memo_replace")
    async def memo_replace(self, event: AstrMessageEvent, old_text: str, content: str) -> str:
        """改写「常驻记忆」里已有的某一条：找到包含 old_text 的那条，整条换成 content。条目越攒越多、快到上限时，用它把重叠的几条并短。

        Args:
            old_text(string): 要改的那条里的原话片段（先用 memo_view 拿到准确原文）
            content(string): 换成什么，整条的新内容，尽量短（别超过 400 字）
        """
        try:
            bad = self._write_guard(event)
            if bad:
                return bad
            return self.op_replace(old_text, content)
        except Exception as e:  # noqa: BLE001
            return f"（改常驻记忆出错：{type(e).__name__}: {e}）"

    @filter.llm_tool(name="memo_remove")
    async def memo_remove(self, event: AstrMessageEvent, old_text: str) -> str:
        """从「常驻记忆」里删掉某一条（包含 old_text 的那条）。过时的、不会再用的就删掉，给新的腾地方。

        Args:
            old_text(string): 要删的那条里的原话片段（先用 memo_view 拿到准确原文）
        """
        try:
            bad = self._write_guard(event)
            if bad:
                return bad
            return self.op_remove(old_text)
        except Exception as e:  # noqa: BLE001
            return f"（删常驻记忆出错：{type(e).__name__}: {e}）"

    # ------------------------------------------------------------------
    # 钩子：每轮请求发出前 → 把常驻记忆块注入 system prompt
    # ------------------------------------------------------------------
    @filter.on_llm_request()
    async def inject_memo(self, event: AstrMessageEvent, req) -> None:
        """官方钩子（`core/star/register/star_handler.py` 定义）。调用点：
        `core/pipeline/process_stage/method/agent_sub_stages/internal.py:352`
        `if await call_event_hook(event, EventType.OnLLMRequestEvent, req)` —— 在
        `build_main_agent`（人格 + 技能已经拼进 system_prompt）之后、真发请求之前，
        所以**原地改 `req.system_prompt` 生效**。

        挂在**最尾部**：前缀缓存护住人格与技能那一段，只有这一块每轮可能变。
        返回 None = 不拦截（返回真值会让 AstrBot 直接丢掉这轮请求）。
        """
        try:
            if not (self.enable and self.inject_enable):
                return
            umo = umo_of(event)
            if not umo_allowed(umo, self.inject_umos):
                return
            sp = getattr(req, "system_prompt", None)
            if not isinstance(sp, str):
                logger.warning("[hermes_memo] req.system_prompt 不是 str，跳过注入")
                return
            if BLOCK_HEAD in sp:
                return  # 同一个请求已经注入过（一轮对话会发多次请求）
            entries = self.read_entries()
            block = render_block(entries, self.char_limit)
            if not block:
                return
            req.system_prompt = f"{sp.rstrip()}\n\n{block}\n"
            logger.info(
                f"[hermes_memo] 注入 n={len(entries)} chars={len(block)}"
                f" 用量={self._usage_line(entries)} umo={umo}"
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[hermes_memo] 注入钩子异常 {type(e).__name__}: {e}")
