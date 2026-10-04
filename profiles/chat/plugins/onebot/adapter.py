"""OneBot v11 (NapCat / go-cqhttp) 平台适配器 —— 反向 WebSocket 模式。

设计来源（读，不搬代码）：
  * 连接方式 = **反向 WS**（NapCat 主动连到我们）。AstrBot 同款：
    `/AstrBot/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py:55-62`
    用 `use_ws_reverse=True` + `ws_reverse_host`/`ws_reverse_port`/`ws_reverse_token`。
    选它的理由：已在本机 NapCat 上验证可用；不需要在协议端配 api 地址；token 走 WS 头。
  * 会话标识（**必须稳定**，否则每句新开对话）：见 `_session_umo()`。Hermes 侧真正的
    会话键由 `gateway/session.py::build_session_key`（:654-695）从 SessionSource 算出：
    ``<ns>:<platform>:<chat_type>[:<chat_id>][:<thread_id>][:<user>]``。
    私聊传 chat_id=QQ号 → 键稳定为 ``<ns>:onebot:dm:<QQ>``；
    群聊传 chat_id=群号 + user_id=发送者 → ``<ns>:onebot:group:<群号>:<发送者>``
    （`group_sessions_per_user` 默认 True，见 gateway/run_adapters.py:1542-1546）。

安全默认：``extra.read_only`` 默认 **True** —— 入站照收、出站一律拦截并记日志。
硬约束要求「原型默认只收不发」；要真发必须先由主人把 read_only 改成 false。

群聊（B 阶段 2026-09-23 → C1 同日）：`group_enabled: true` 时群消息**总是**进滚动窗口
（`group_window.py`）；是否**唤醒一轮真实 agent 回合**由三态 `group_wake_mode` 决定
（`group_wake.py`）：

  * ``collect-only``（默认）—— B 阶段：只采集，**0 LLM / 0 记忆写入**
  * ``mention-only``（C1 落地档）—— **被 @ 小号 / 被回复她 / 正文点名** 才唤醒一轮，
    把该群滚动窗口当上下文递给她，她的回复发回该群；其余群消息行为与 B 完全一致
  * ``full`` —— 每条群消息都唤醒（老 `group_wake_enabled: true` 的语义，**未验收，别开**）

**记忆隔离硬前置**（PLAN §4.3）：`group_memory_guard_required`（默认 true）时，
适配器只在 `memory.provider` 指向闸门 provider（`hindsight_guard`）且插件在位时**才唤醒**；
否则拒绝唤醒并计数（取证实验见 `tests/evidence_group_retain.py`）。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import re
import time
from collections import OrderedDict, deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote as _urlquote

from gateway.config import Platform
from gateway.platforms._shared import get_scoped_secret
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.platforms.helpers import cancel_task

from . import debounce as _db
from . import group_wake as _gwk
from . import onebot_time as _clock
from . import proactive as _pro
from . import group_window as _gw
from . import sticker_labeler as _slabel
from . import sticker_lib as _stick

try:  # 群管理是可选件：模块不在 / 没开 → 一切照旧（安全降级，新功能不绑架现役行为）
    from . import group_admin as _gadmin
except Exception:  # noqa: BLE001
    _gadmin = None  # type: ignore[assignment]
from . import health as _health
from . import onebot_proto as _proto
from . import segmentation as _seg
from . import trust as _trust

logger = logging.getLogger(__name__)

# 把配置读法收在一处：env 优先（复用 Hermes profile scoped secret），否则 config.yaml extra。
_ENV_BRIDGE = {
    "host": ("ONEBOT_WS_HOST", "ws_host"),
    "port": ("ONEBOT_WS_PORT", "ws_port"),
    "access_token": ("ONEBOT_ACCESS_TOKEN", "access_token"),
    "self_id": ("ONEBOT_SELF_ID", "self_id"),
}

_WS_PATHS = ("/", "/ws", "/onebot/v11/ws", "/onebot/v11/ws/")


class OneBotAdapter(BasePlatformAdapter):
    """OneBot v11 反向 WS 适配器。"""

    # 出站分段：阈值/正则/清理规则/间隔全部继承 AstrBot 已验证参数。
    SEGMENT_REGEX = _seg.DEFAULT_REGEX
    SEGMENT_CLEANUP = _seg.DEFAULT_CLEANUP_RULE

    # ── 工具面分流：私聊给执行权、群聊不给（2026-10-03 主人拍板）─────────────
    # 机制：网关每轮会问适配器 `toolsets_for_source(source)`（gateway/run_turn.py::
    # `_resolve_enabled_toolsets_for_source`），返回值**替换** `platform_toolsets.<平台>`；
    # 返回值 None = 用 config 里的列表。
    # 策略：**只有 chat_type == "dm" 才拿 config 原表**（其中有 terminal）；
    #       其余一律（group/channel/supergroup/guild/认不出来的）→ 摘掉执行类工具集，fail-closed。
    # 为什么不靠提示词：提示词是软约束，被 @ 唤醒的群回合一样能调工具，所以闸放在代码层。
    _EXECUTION_TOOLSETS = ("terminal",)

    def toolsets_for_source(self, source):
        try:
            chat_type = str(getattr(source, "chat_type", "") or "").strip().lower()
        except Exception:  # noqa: BLE001
            chat_type = ""
        if chat_type == "dm":
            return None  # 私聊：用 config 里的 onebot 表（含 terminal）
        try:
            from hermes_cli.config import load_config_readonly

            cfg = load_config_readonly() or {}
            base = list((cfg.get("platform_toolsets") or {}).get("onebot") or [])
        except Exception:  # noqa: BLE001
            logger.exception("[onebot] 非私聊工具面解析失败 → 回落为最保守的一组（无执行权）")
            base = ["a2a", "file", "memory", "session_search", "skills", "vision", "web"]
        kept = [t for t in base if t not in self._EXECUTION_TOOLSETS]
        logger.info("[onebot] chat_type=%s → 工具面 %s（已摘 %s）",
                    chat_type or "?", kept, [t for t in base if t in self._EXECUTION_TOOLSETS])
        return kept

    def __init__(self, config, **kwargs):
        super().__init__(config=config, platform=Platform("onebot"))
        extra: Dict[str, Any] = getattr(config, "extra", {}) or {}
        self._extra = extra

        def _cfg(key: str, default=None):
            env, extra_key = _ENV_BRIDGE.get(key, ("", key))
            if env:
                v = get_scoped_secret(env)
                if v not in (None, ""):
                    return v
            return extra.get(extra_key, default)

        self.host = str(_cfg("host", "127.0.0.1"))
        try:
            self.port = int(_cfg("port", 6700))
        except (TypeError, ValueError):
            self.port = 6700
        self.access_token = str(_cfg("access_token", "") or "")
        self.self_id = str(_cfg("self_id", "") or "")

        # ── 安全默认 ────────────────────────────────────────────────────────
        self.read_only = _truthy(extra.get("read_only"), True)

        # ── 访问策略（默认 allowlist + 空名单 = 谁都不放行）───────────────
        self.dm_policy = str(extra.get("dm_policy", "allowlist") or "allowlist").strip().lower()
        self.group_enabled = _truthy(extra.get("group_enabled"), False)
        self.allow_from: Set[str] = {str(x) for x in (extra.get("allow_from") or []) if str(x).strip()}

        # ── 信任档（**判据层**，不动准入）────────────────────────────────────
        # 主人 2026-09-23 拍板：私聊准入保持全开（陌生人也回），
        # 但**除主人外一律按「群聊生人」同款对待**。
        # 这里**只解析名单**：不改 `dm_policy` / `allow_from` 的放行结果，
        # 也不接进 `_dispatch` / `send` —— 档位怎么用属于 C 阶段（PLAN §11）。
        # 默认＝主人大号；配置写空/写坏也回落到默认（不许把主人判成生人）。
        self.trust_tier_owner_ids: Tuple[str, ...] = _trust.resolve_owner_ids(
            extra.get(_trust.CONFIG_KEY))

        # ── 群管理（可选件）────────────────────────────────────────────────
        # 默认 **关**：群管理会真禁言、真踢人，没明确开就不许动。
        # 模块缺失（`_gadmin is None`）也一律当关 —— 安全降级。
        self.group_admin_enabled = _truthy(extra.get("group_admin_enabled"), False)
        self.group_admin = None
        self._group_admin_actions = 0
        #: 待审批的加群申请：申请人 QQ → {flag, gid, comment, ts}（2026-10-03）
        self._pending_joins: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        #: 入群申请复审的节流状态（每群冷却 + 每小时上限）与计数
        self._join_review_last_ts: Dict[str, float] = {}
        self._join_review_ts: Dict[str, List[float]] = {}
        self._join_reviews = 0
        #: 群管理只提示不发请求的那些话（如"有人撤回但没原文"）。有上限，别无限长。
        self._group_admin_notes: Any = deque(maxlen=50)
        if self.group_admin_enabled and _gadmin is not None:
            try:
                self.group_admin = _gadmin.GroupAdmin(
                    extra.get("group_admin") or {},
                    owner_ids=self.trust_tier_owner_ids,
                    self_id=self.self_id)
            except Exception:  # noqa: BLE001
                logger.exception("[onebot] 群管理初始化失败 —— 已停用，其余功能不受影响")
                self.group_admin = None
        elif self.group_admin_enabled:
            logger.warning("[onebot] group_admin_enabled=true 但 group_admin 模块不可用 → 已停用")

        # ── 群聊：B 阶段「只看不说」（2026-09-23 主人拍板的三层设计）──────────
        # ① 连贯层：每群一份滚动窗口（有上限、落 profile 数据目录、**绝不入主库**）
        # ② 长期记忆层：默认零入库（群消息根本不到 agent，就没有 auto_retain 可言）
        # ③ B 阶段不唤醒 LLM：group_wake_enabled **默认 false** 是这条红线的唯一开关
        self.group_collect_enabled = _truthy(extra.get("group_collect_enabled"), True)
        # C1 三态唤醒：模式键是唯一权威；配置里只有老的 `group_wake_enabled` 时按老语义映射。
        self.group_wake_mode = _gwk.parse_mode(extra)
        #: 入群申请「唤她来审」的每小时上限（主人 2026-10-03：「申请一小时不要给她发太多」）。
        #: 超限的申请**直接宽松放行**，不唤醒 AI、不在群里说话。
        _ga_conf = extra.get("group_admin") if isinstance(extra.get("group_admin"), dict) else {}
        try:
            self.join_review_max_per_hour = int(
                extra.get("join_review_max_per_hour")
                or (_ga_conf or {}).get("join_review_max_per_hour") or 1)
        except (TypeError, ValueError):
            logger.warning("[onebot] join_review_max_per_hour 非法，回退 1")
            self.join_review_max_per_hour = 1
        self.join_review_max_per_hour = max(1, self.join_review_max_per_hour)
        #: 审申请那一轮的正文不出声（(群号, 时间戳)；主人：「不要反馈到群里」）
        self._silent_group_turn: Optional[Tuple[str, float]] = None

        # ── 主动私聊：按「沉默时长」升概率的纯脚本斜坡（2026-10-03 主人定稿）──────
        # 口径与模拟值写在 `proactive.py` 头注里；配置见 profile config 的
        # `proactive_dm_*`（默认关，线上已开）。
        self.proactive = _pro.ProactiveRamp(
            enabled=_truthy(extra.get("proactive_dm_enabled"), False),
            step_pct=_float_or(extra.get("proactive_dm_step_pct"), 3.0),
            tick_min=_float_or(extra.get("proactive_dm_tick_min"), 10.0),
            ramp_shape=str(extra.get("proactive_dm_ramp_shape") or "quadratic"),
            ramp_hours=_float_or(extra.get("proactive_dm_ramp_hours"), 5.0),
            quiet=_pro.parse_quiet(str(extra.get("proactive_dm_quiet") or "00:30-07:00")))
        # ⚠️ 这里不能用 `self.state_path` —— 它在下面第 ~390 行才赋值（会 AttributeError）。
        #    默认跟状态文件同目录：`<hermes home>/onebot-proactive.json`。
        self.proactive_path = Path(extra.get("proactive_state_path")
                                   or (_hermes_home() / "onebot-proactive.json"))
        #: 会话键（私聊 = 对方 QQ）→ 斜坡状态；只有**有过对话**的私聊才会出现在这里，
        #: 所以冷启动不会凭空给谁发消息。
        self._proactive: Dict[str, _pro.RampState] = {}
        self._proactive_fires = 0
        #: 主动私聊那一轮允许她 `[SILENT]` 闭嘴（普通私聊回合不许装死）
        self._proactive_turn: Optional[Tuple[str, float]] = None
        self._load_proactive()
        self._proactive_seed_owners()
        self._save_proactive()      # 立刻落盘：状态文件一启动就存在，进程再重启也不丢时钟
        # 派生值（不是开关）：心跳/doctor 沿用这个名字表达「她会不会在群里说话」。
        self.group_wake_enabled = self.group_wake_mode != _gwk.MODE_COLLECT
        # gated 档（「让她自己考虑」）：免费前置闸的阈值 + 每群门控状态。
        # 阈值越低越爱说（0＝每条都叫模型＝纯烧钱）；建议 0.30~0.45。
        try:
            self.group_gate_threshold = float(extra.get("group_gate_threshold", _gwk.GATE_DEFAULT_THRESHOLD))
        except (TypeError, ValueError):
            logger.warning("[onebot] group_gate_threshold 非法，回退默认 %.2f", _gwk.GATE_DEFAULT_THRESHOLD)
            self.group_gate_threshold = _gwk.GATE_DEFAULT_THRESHOLD
        self.group_gate_threshold = max(0.0, min(1.0, self.group_gate_threshold))
        self._group_gate_state: Dict[str, Any] = {}
        #: 她主动闭嘴（[SILENT]）的次数 —— 心跳/doctor 看这个判断"闸是不是太松"
        self._silence_count = 0
        #: 吞掉的「Hermes 静默提示」条数（主人 2026-10-04 要求不发这段 ⚠️，只记账）
        self._silence_warn_suppressed = 0
        self._gate_wake_count = 0
        self.group_alias_names = _gwk.parse_alias_names(extra.get("group_alias_names"))
        self.group_reply_to_her_wakes = _truthy(extra.get("group_reply_to_her_wakes"), True)
        # ── 自动引用（2026-10-04 主人：「没必要引用就别引用，每一条都引用怪怪的」）──
        # Hermes 会把「这条响应对应哪条入站消息」当 reply_to 传下来（通用机制），私聊里那就是
        # 他这轮的第一条消息 → 每条回复都挂个引用。默认私聊不自动引用；群聊保留（有上下文价值）。
        # 她真想引用哪条，自己在正文写 `[CQ:reply,id=…]` —— 那条路不受这个开关影响。
        self.auto_quote_dm = _truthy(extra.get("auto_quote_dm"), False)
        self.auto_quote_group = _truthy(extra.get("auto_quote_group"), True)
        self.group_context_inject_msgs = _gwk.parse_int(extra.get("group_context_inject_msgs"), 30, minimum=0)
        #: 上下文注入口径（2026-10-03）：``delta`` = 只发上次之后的新消息（主人拍板，省 token、
        #: 不重复触发压缩）；``window`` = 老口径，每轮发最近 N 条（回滚用）。
        _mode = str(extra.get("group_context_inject_mode") or "delta").strip().lower()
        self.group_context_inject_mode = _mode if _mode in ("delta", "window") else "delta"
        #: 每群"已注入到哪一条"的游标（内存态；重启/首次唤醒自动退化成发一段窗口）
        self._group_inject_cursor: Dict[str, Dict[str, Any]] = {}
        self._group_wake_limiter = _gwk.WakeLimiter(
            per_minute=_gwk.parse_int(extra.get("group_at_max_per_minute"), 2, minimum=1),
            per_hour=_gwk.parse_int(extra.get("group_at_max_per_hour"), 30, minimum=1))
        # 记忆隔离硬前置（默认 **要求** 就位；`false` 只留给排障，别在群里开）
        self.group_memory_guard_required = _truthy(extra.get("group_memory_guard_required"), True)
        self.group_memory_guard_provider = str(
            extra.get("group_memory_guard_provider") or _gwk.GUARD_PROVIDER)
        self._group_own_mids: Dict[str, Any] = {}
        try:
            self.group_window_max_msgs = max(1, int(extra.get("group_window_max_msgs", _gw.DEFAULT_MAX_MSGS)))
        except (TypeError, ValueError):
            self.group_window_max_msgs = _gw.DEFAULT_MAX_MSGS
        try:
            self.group_window_max_bytes = max(1024, int(
                extra.get("group_window_max_bytes", _gw.DEFAULT_MAX_BYTES)))
        except (TypeError, ValueError):
            self.group_window_max_bytes = _gw.DEFAULT_MAX_BYTES
        self.group_window_dir = Path(
            extra.get("group_window_dir") or (_hermes_home() / "onebot-groups"))
        self.group_window = _gw.GroupWindow(
            self.group_window_dir,
            max_msgs=self.group_window_max_msgs, max_bytes=self.group_window_max_bytes)

        # ── 入站媒体落盘（2026-09-23 第二批：图片 → 本地文件）──────────────────
        # 只留 `[图片]` 占位的话她拿不到内容，vision 也没东西可看。这里把图片落到
        # 本 profile 的 onebot-media/<日期>/ 下，正文占位替换成 `[图片:<绝对路径>]`。
        # 只对**会送进 agent 回合**的消息做（私聊 + 唤醒的群消息）—— 群窗口采集那条路
        # 不下载：群里 99% 的消息不唤醒她，先把每条图都拉一遍不划算。
        self.media_download_enabled = _truthy(extra.get("media_download_enabled"), True)
        # 群窗口里的图/表情包也落盘（默认开）。关掉 = 退回"只在她被叫醒时下载"的旧行为。
        self.media_group_download = _truthy(extra.get("media_group_download"), True)
        self.media_dir = Path(extra.get("media_dir") or (_hermes_home() / "onebot-media"))
        try:
            self.media_max_bytes = max(1024, int(extra.get("media_max_bytes", 8 * 1024 * 1024)))
        except (TypeError, ValueError):
            self.media_max_bytes = 8 * 1024 * 1024
        try:
            self.media_max_per_msg = max(0, int(extra.get("media_max_per_msg", 4)))
        except (TypeError, ValueError):
            self.media_max_per_msg = 4
        self._media_saved = 0
        self._media_failed = 0

        # ── 入站语音转写（2026-10-04）──────────────────────────────────────────
        # QQ 语音（record 段）原来只渲染成 "[语音]" 空壳，她一个字都听不到。
        # 链路：协议端动作 get_record(out_format=mp3) 拿 mp3 → 落盘 → 本地 whisper
        # （speaches @ 8090）转写 → 正文里的 "[语音]" 换成 "[语音: 文本]"。
        # 失败一律保留 "[语音]" 占位，绝不打断接收循环。
        self.voice_transcribe_enabled = _truthy(extra.get("voice_transcribe_enabled"), True)
        self.voice_out_format = str(extra.get("voice_out_format") or "mp3")
        self.stt_url = str(extra.get("stt_url")
                           or "http://172.17.0.1:8090/v1/audio/transcriptions")
        self.stt_model = str(extra.get("stt_model") or "Systran/faster-whisper-small")
        try:
            self.stt_timeout = max(5.0, float(extra.get("stt_timeout", 60)))
        except (TypeError, ValueError):
            self.stt_timeout = 60.0
        self._voice_ok = 0
        self._voice_failed = 0

        # ── 上线补投（2026-10-04）──────────────────────────────────────────────
        # 她不在线期间：私聊要等 QQ 上线才推、群消息 QQ 根本不补（窗口上是断档）。
        # 上线后主动去历史 API 捞最近 N 条补给她（默认 5），并带上「你刚上线」的说明。
        # 私聊合成一个回合；群聊追进滚动窗口（复用 delta 注入）+ 过一遍唤醒判定。
        self.offline_backfill_enabled = _truthy(extra.get("offline_backfill_enabled"), True)
        try:
            self.offline_backfill_max = max(1, int(extra.get("offline_backfill_max", 5)))
        except (TypeError, ValueError):
            self.offline_backfill_max = 5
        try:
            self.offline_backfill_delay_s = max(0.0, float(extra.get("offline_backfill_delay_s", 6.0)))
        except (TypeError, ValueError):
            self.offline_backfill_delay_s = 6.0
        #: 查历史失败后的退避基数（测试里设为 0 免得慢）
        self.offline_backfill_retry_wait = 3.0
        self.backfill_path = Path(extra.get("offline_backfill_state_path")
                                  or (_hermes_home() / "onebot-backfill.json"))
        #: {"dm:<uid>": ts, "group:<gid>": ts} —— 每个会话「到过哪」的游标
        self._last_seen: Dict[str, float] = {}
        self._backfill_running = False
        self._backfill_injected = 0
        self._backfill_skipped = 0
        self._backfill_dirty = False
        self._backfill_saved_at = 0.0     # 游标落盘节流（见 _mark_seen）
        self.offline_backfill_save_every = 15.0
        self._load_backfill_cursor()

        # ── 表情包库（偷图 / 上限 / 挑图 / 贴表情回应）──────────────────────
        # 依据 chat-layer/PLAN-v6-表情包.md：判据用协议字段（sub_type/emoji_id），
        # 命名按内容 sha256（天然去重），上限条数 + 总体积、超限 LRU 淘汰。
        self.stickers_enabled = _truthy(extra.get("stickers_enabled"), True)
        self.stickers_dir = Path(extra.get("stickers_dir") or (_hermes_home() / "onebot-stickers"))
        self.stickers_retention_days = _int_or(extra.get("stickers_retention_days"),
                                               _stick.RETENTION_DAYS)
        self.sticker_lib = _stick.StickerLib(
            self.stickers_dir,
            max_items=_int_or(extra.get("stickers_max_items"), _stick.MAX_ITEMS),
            max_total_bytes=_int_or(extra.get("stickers_max_total_bytes"),
                                    _stick.MAX_TOTAL_BYTES),
            max_item_bytes=self.media_max_bytes,
            retention_days=self.stickers_retention_days)
        self._stickers_saved = 0
        self._stickers_dedup = 0
        self._stickers_failed = 0
        # ── 自动打标（2026-10-04 主人：入库排队识别、发的时候按情绪随机挑）──────
        # 排队而不是当场打：视觉模型(8081)是 --parallel 1，还兼着 Hindsight 抽取，
        # 当场同步打会卡住接收循环、还跟记忆抽取抢槽（见 sticker_labeler.py 头注）。
        self.sticker_labeler = _slabel.StickerLabeler(
            self.sticker_lib,
            enabled=_truthy(extra.get("sticker_label_enabled"), True),
            url=str(extra.get("sticker_label_url") or _slabel.DEFAULT_URL),
            model=str(extra.get("sticker_label_model") or _slabel.DEFAULT_MODEL),
            timeout=_float_or(extra.get("sticker_label_timeout"), 600.0),
            max_attempts=_int_or(extra.get("sticker_label_max_attempts"), 3),
            interval_s=_float_or(extra.get("sticker_label_interval_s"), 15.0),
            batch=_int_or(extra.get("sticker_label_batch"), 3),
            boot_scan=_truthy(extra.get("sticker_label_boot_scan"), True),
        )
        # 出站发图：她在正文里写 [表情包:标签] / [表情包:/路径]
        self.sticker_send_enabled = _truthy(extra.get("sticker_send_enabled"), True)
        self._stickers_sent = 0
        self._sticker_send_failed = 0
        # 出站「贴表情回应」（set_msg_emoji_like）：她在正文里写 [贴表情:赞] / [贴表情:76]
        self.emoji_like_enabled = _truthy(extra.get("emoji_like_enabled"), True)
        self._emoji_likes = 0
        self._emoji_like_failed = 0
        self._last_inbound_mid: Dict[str, str] = {}

        # ── 出站分段 ────────────────────────────────────────────────────────
        self.segment_enabled = _truthy(extra.get("segment_enabled"), True)
        # ⚠️ 长度阈值已废弃（2026-09-23 主人拍板去掉）：出现 `⁂` 就切，没有就整段发。
        #    配置里的 `segment_threshold` 不再被读取 —— 别再把它加回来（RUNBOOK 有登记）。
        self.segment_interval = _seg.parse_interval(extra.get("segment_interval"))
        self.segment_leading_delay = _truthy(extra.get("segment_leading_delay"), False)
        # 已知群号：Hermes 的 send(chat_id, …) 不带 chat_type，靠入站学到 + 配置兜底。
        self.group_ids: Set[str] = {str(x) for x in (extra.get("group_ids") or []) if str(x).strip()}

        # ── 入站防抖 ────────────────────────────────────────────────────────
        self.debounce_enabled = _truthy(extra.get("debounce_enabled"), True)
        try:
            self.debounce_seconds = float(extra.get("debounce_seconds", 10))
        except (TypeError, ValueError):
            self.debounce_seconds = 10.0
        try:
            self.debounce_max_seconds = float(extra.get("debounce_max_seconds", 45))
        except (TypeError, ValueError):
            self.debounce_max_seconds = 45.0
        self.debounce_scope = str(extra.get("debounce_scope", _db.SCOPE_PRIVATE) or _db.SCOPE_PRIVATE).strip().lower()
        if self.debounce_scope not in _db.VALID_SCOPES:
            self.debounce_scope = _db.SCOPE_PRIVATE
        self.debounce_wake_prefixes = [str(p) for p in (extra.get("debounce_wake_prefixes") or []) if p]
        self._debounce = _db.Store(self.debounce_seconds, self.debounce_max_seconds)

        # ── 发送重试 ────────────────────────────────────────────────────────
        try:
            self.send_retries = max(1, int(extra.get("send_retries", 3)))
        except (TypeError, ValueError):
            self.send_retries = 3
        self.send_retry_backoff = float(extra.get("send_retry_backoff", 1.0))

        # ── 运行时 ──────────────────────────────────────────────────────────
        self._app = None
        self._runner = None
        self._site = None
        self._ws = None
        self._ws_lock = asyncio.Lock()
        self._echo_seq = 0
        self._pending: Dict[str, asyncio.Future] = {}
        self._ingest_tasks: Set[asyncio.Task] = set()

        # ── 自检 / 告警（红线 2：坏了要能马上知道）─────────────────────────
        # 只记计数与时间戳；**绝不落消息正文、绝不落凭据**。
        # started_ts = 本适配器进程的启动时刻：健康判据用它做「刚启动宽限期」，
        # 免得网关/NapCat 重启后协议端还没重连上就被判成「从未连上」的故障。
        self._started_ts = time.time()
        self._client_connected = False
        self._last_client_ts = 0.0
        self._last_rx_ts = 0.0
        self._last_tx_ts = 0.0
        self._rx_count = 0
        self._tx_count = 0
        self._segments_sent = 0
        self._dropped_count = 0
        # 群聊计数（C1 起是三把量尺）：
        #   group_rx_count   = 收进滚动窗口的群消息条数
        #   group_llm_calls  = 因群消息真正起过 agent 回合的次数（**只在唤醒时增长**）
        #   group_*_count    = 三类唤醒触发（@ / 被回复 / 点名）各命中几次
        #   group_wake_blocked = 因「记忆隔离未就位」被挡下的唤醒（预期恒 0）
        #   group_wake_limited = 被每分钟/每小时上限挡下的唤醒（**有日志，不静默丢**）
        self._group_rx_count = 0
        self._group_llm_calls = 0
        self._group_mention_count = 0
        self._group_reply_count = 0
        self._group_name_count = 0
        self._group_full_count = 0
        self._group_wake_blocked = 0
        self._group_wake_limited = 0
        self._group_last_trigger = ""
        self._consecutive_failures = 0
        self._last_error = ""
        self._alerts_sent = 0
        self._alert_last_at: Dict[str, float] = {}
        self._watchdog_task: Optional[asyncio.Task] = None

        self.health_thresholds = _health.thresholds_from_extra(extra)
        self.health_interval = _float_or(extra.get("health_interval_seconds"), 30.0)
        self.alert_enabled = _truthy(extra.get("alert_enabled"), True)
        self.alert_cooldown = _float_or(extra.get("alert_cooldown_seconds"), 1800.0)
        self.alert_report_url = str(
            extra.get("alert_report_url", "http://172.17.0.1:8098/report") or "")
        # magicpush（:818/api/push/<token>）：主人 2026-10-04 授权把这条告警通道从
        # 已退役的 :8098（AstrBot 时代）改到能用的那个上，见 _build_alert_request。
        self.alert_magicpush = _truthy(extra.get("alert_magicpush"), False)
        self.alert_token_file = str(extra.get(
            "alert_token_file", "/opt/data/chat-layer/plugin/hermes_report/.report_token") or "")
        self.state_path = Path(extra.get("state_path") or (_hermes_home() / "onebot-state.json"))
        self.alert_log_path = Path(extra.get("alert_log_path") or (_hermes_home() / "onebot-alerts.log"))
        # 别把「没人来聊天」这种正常安静误报成故障：入站空闲阈值只管到点后告警一次。
        self.alert_recovery = _truthy(extra.get("alert_recovery"), True)
        self._alerting: Set[str] = set()

    # ── 元信息 ─────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "OneBot11"

    @property
    def enforces_own_access_policy(self) -> bool:
        """只有真正的 allowlist 才算「自己管访问」；open 必须由网关的 ALLOW_ALL 显式放行。"""
        return self.dm_policy == "allowlist"

    def _fail(self, code: str, message: str, *, retryable: bool) -> bool:
        self._set_fatal_error(code, message, retryable=retryable)
        return False

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        is_group = str(chat_id) in self.group_ids
        return {"name": f"{'群' if is_group else 'QQ'}{chat_id}", "type": "group" if is_group else "dm"}

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """OneBot 无 typing 指示器；协议端有 set_input_status 但非通用，不做。"""

    # ── 自检 / 告警 ────────────────────────────────────────────────────────

    def health_state(self) -> Dict[str, Any]:
        """当前运行状态快照。**只有计数与时间戳，无消息正文、无凭据。**"""
        _st = self._sticker_stats()
        return {
            "ts": time.time(),
            "started_ts": self._started_ts,
            "platform": "onebot",
            "host": self.host,
            "port": self.port,
            "listener_up": bool(self._site is not None and self.is_connected),
            "client_connected": bool(self._client_connected),
            "last_client_ts": self._last_client_ts,
            "last_rx_ts": self._last_rx_ts,
            "last_tx_ts": self._last_tx_ts,
            "rx_count": self._rx_count,
            "tx_count": self._tx_count,
            "segments_sent": self._segments_sent,
            "dropped_count": self._dropped_count,
            "group_enabled": bool(self.group_enabled),
            "join_reviews": self._join_reviews,
            "group_collect_enabled": bool(self.group_collect_enabled),
            "group_wake_enabled": bool(self.group_wake_enabled),
            "group_wake_mode": self.group_wake_mode,
            "group_mode": self.group_mode(),
            "group_rx_count": self._group_rx_count,
            "group_llm_calls": self._group_llm_calls,
            "group_mention_count": self._group_mention_count,
            "group_reply_count": self._group_reply_count,
            "group_name_count": self._group_name_count,
            "group_full_count": self._group_full_count,
            "group_wake_blocked": self._group_wake_blocked,
            "group_wake_limited": self._group_wake_limited,
            "group_last_trigger": self._group_last_trigger,
            "group_memory_isolated": bool(self._group_memory_isolation().get("ok")),
            "group_memory_guard_required": bool(self.group_memory_guard_required),
            "group_window_errors": self.group_window.errors,
            "group_window": self.group_window.summary(),
            # 媒体落盘（第二批）：只看计数，不含任何正文/路径
            "media_enabled": bool(self.media_download_enabled),
            "media_group_download": bool(self.media_group_download),
            "media_saved": self._media_saved,
            "media_failed": self._media_failed,
            "stickers_enabled": bool(self.stickers_enabled),
            # 出站被丢掉的表情（非原生小黄脸，协议端必拒）——只做观测，出问题好定位
            "face_dropped": dict(getattr(_proto, "_DROPPED_FACES", {}) or {}),
            "stickers_saved": self._stickers_saved,
            "stickers_dedup": self._stickers_dedup,
            "stickers_failed": self._stickers_failed,
            "stickers_count": _st.get("count"), "stickers_bytes": _st.get("bytes"),
            "stickers_untagged": _st.get("untagged"),
            "emoji_like_enabled": bool(self.emoji_like_enabled),
            "emoji_likes": self._emoji_likes,
            "emoji_like_failed": self._emoji_like_failed,
            # 群管理可观测性：不开、开了但模块缺失、真在跑 —— 光看这两行就能分清
            "group_admin_enabled": bool(self.group_admin_enabled),
            "group_admin_active": self.group_admin is not None,
            "group_admin_actions": self._group_admin_actions,
            "group_admin_notes": len(self._group_admin_notes),
            "consecutive_failures": self._consecutive_failures,
            "last_error": self._last_error[:200],
            "alerts_sent": self._alerts_sent,
            "read_only": bool(self.read_only),
            "debounce": f"{self.debounce_seconds}/{self.debounce_max_seconds}s scope={self.debounce_scope}"
                        if self.debounce_enabled else "off",
            "segmentation": f"sep={_seg.SEP} interval={self.segment_interval}"
                            if self.segment_enabled else "off",
            "access_token_set": bool(self.access_token),
        }

    def health_findings(self) -> List[Dict[str, str]]:
        return _health.judge(self.health_state(), thresholds=self.health_thresholds)

    def group_mode(self) -> str:
        """群聊当前形态（一句话，进心跳 + 日志 + doctor）。

        * ``off``               —— 群开关关着（群消息在准入处被丢）
        * ``collect-only(0 LLM)``—— B 阶段：只进滚动窗口，不进 agent
        * ``mention-wake(付费回合，仅@/回复/点名)`` —— C1：三类命中才唤醒
        * ``collect+wake(付费回合)`` —— ``full``：每条群消息都唤醒（未验收）
        """
        if not self.group_enabled:
            return "off"
        if not self.group_collect_enabled:
            return "wake-only" if self.group_wake_enabled else "off(collect disabled)"
        if self.group_wake_mode == _gwk.MODE_COLLECT:
            return "collect-only(0 LLM)"
        if self.group_wake_mode == _gwk.MODE_MENTION:
            return "mention-wake(付费回合，仅@/回复/点名)"
        return "collect+wake(付费回合)"

    def _group_memory_isolation(self, *, force: bool = False) -> Dict[str, Any]:
        """记忆隔离闸就位没有（缓存 60s：心跳每 30s 会读它，别每次都摸磁盘）。"""
        now = time.time()
        cache = getattr(self, "_group_iso_cache", None)
        if not force and cache and now - cache[0] < 60.0:
            return cache[1]
        st = _gwk.memory_isolation_status(
            _hermes_home(), provider_name=self.group_memory_guard_provider,
            guard_name=self.group_memory_guard_provider)
        self._group_iso_cache = (now, st)
        return st

    def _record_send_failure(self, err: str) -> None:
        """记录一次发送失败，供自检判「连续失败」。**只在状态里留摘要，不留正文。**"""
        self._consecutive_failures += 1
        self._last_error = str(err)[:200]
        self.write_state_file()

    # ── 主动私聊：状态、记账、心跳（2026-10-03）────────────────────────────

    def _load_proactive(self) -> None:
        try:
            if self.proactive_path.is_file():
                raw = json.loads(self.proactive_path.read_text(encoding="utf-8"))
                for k, v in (raw.get("chats") or {}).items():
                    self._proactive[str(k)] = _pro.RampState.from_dict(v)
                self._proactive_fires = int(raw.get("fires") or 0)
        except Exception as e:  # noqa: BLE001 —— 读不出来就按空的算，不许挡住启动
            logger.warning("[onebot] 读主动私聊状态失败（按空算）：%s", e)

    def _save_proactive(self) -> None:
        try:
            self.proactive_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.proactive_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(
                {"fires": self._proactive_fires,
                 "chats": {k: v.to_dict() for k, v in self._proactive.items()}},
                ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.proactive_path)
        except Exception as e:  # noqa: BLE001
            logger.debug("[onebot] 写主动私聊状态失败：%s", e)

    def _proactive_state(self, chat_id: str) -> _pro.RampState:
        """拿这个私聊的斜坡状态；第一次见就以"现在"为起点（不会一上来就触发）。"""
        key = str(chat_id)
        st = self._proactive.get(key)
        if st is None:
            st = _pro.RampState(last_activity_ts=time.time(), last_tick_ts=time.time())
            self._proactive[key] = st
        return st

    def _proactive_seed_owners(self) -> None:
        """冷启动也要有起点（2026-10-03）。

        否则重启后 `_proactive` 是空的 → 得等他先说话才会开始计时，等于「重启一次就静默到下一次对话」。
        给配置里的主人私聊播一个「现在」：时钟从启动那刻算起，不会一上来就触发。
        """
        for owner in (getattr(self, "trust_tier_owner_ids", ()) or ()):
            if str(owner):
                self._proactive_state(str(owner))

    def _proactive_activity(self, chat_id: str, now: Optional[float] = None) -> None:
        """有对话（他说话 / 她说话）→ 斜坡归零（主人：「期间发生对话就重置到 0%」）。"""
        if not self.proactive.enabled:
            return
        self.proactive.on_activity(self._proactive_state(chat_id), now or time.time())

    async def _proactive_check(self) -> None:
        """看门狗里跑的心跳：**纯脚本**，没掷中就不叫模型，一分钱不花。

        静默窗（默认 00:30–07:00）里既不涨概率也不掷骰子，但**概率不清空**
        （主人：「昨天 00:30 到 40%，第二天 7:00 从 40% 开始」）。
        """
        if not self.proactive.enabled or self.read_only:
            return
        now = time.time()
        for chat_id in list(self._proactive.keys()):
            # ★ 主人 2026-10-03：「群的保持原样就好，现在这个主动起头这个功能不往群搞」。
            #   所以这里**挡住群号**：主动起头只走私聊，群聊那条链路（免费闸 / @ 唤醒 /
            #   窗口 / 群记忆）一行都不许被它碰。别把这条删了。
            if str(chat_id) in self.group_ids:
                logger.warning("[onebot] 主动私聊状态里出现了群号 %s —— 已跳过（主动起头不进群）",
                               _mask(chat_id))
                continue
            st = self._proactive_state(chat_id)
            if self.proactive.advance(st, now) <= 0:
                continue
            if self.proactive.in_quiet(now):        # 窗里不触发（概率留着）
                self._save_proactive()
                continue
            if not self.proactive.roll(st, now):
                self._save_proactive()
                continue
            pct = self.proactive.prob(st) * 100.0
            text = self.proactive.build_text(st, now)
            silence_min = (now - st.last_activity_ts) / 60.0
            self.proactive.note_fire(st, now)       # 触发即归零（主人：「触发后就归零」）
            self._proactive_fires += 1
            self._proactive_turn = (str(chat_id), now)
            self._save_proactive()
            logger.info("[onebot] 主动私聊触发（p=%.0f%%，沉默 %.0f 分钟）→ 叫醒她一次",
                        pct, silence_min)
            ev = {"post_type": "message", "message_type": "private",
                  "user_id": _proto._num(chat_id), "sender": {"nickname": "主人"},
                  "message_id": None, "raw_message": ""}
            try:
                await self._dispatch(text, ev, is_group=False, gid="", uid=str(chat_id),
                                     trigger="proactive")
            except Exception:  # noqa: BLE001
                self._proactive_turn = None
                logger.exception("[onebot] 主动私聊回合派发失败")

    def write_state_file(self) -> None:
        """把心跳写到磁盘（doctor / 人肉排查都读它）。失败不影响业务。"""
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.health_state(), ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(self.state_path)
        except Exception as e:
            logger.debug("[onebot] could not write state file: %s", e)

    async def _watchdog_loop(self) -> None:
        """周期性自检 + 主动告警。**只在平台被启用时才会跑。**"""
        try:
            while True:
                await asyncio.sleep(max(5.0, self.health_interval))
                if self._client_connected:
                    self._last_client_ts = time.time()
                self.write_state_file()
                try:
                    await self._check_and_alert()
                except Exception:
                    logger.exception("[onebot] watchdog self-check failed")
                try:
                    await self._proactive_check()
                except Exception:
                    logger.exception("[onebot] proactive check failed")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[onebot] watchdog loop died")

    async def _check_and_alert(self) -> None:
        findings = _health.judge(self.health_state(), thresholds=self.health_thresholds)
        bad = [f for f in findings if f.get("level") != _health.OK]
        bad_keys = {f["key"] for f in bad}

        # 恢复通知（只在之前报过、现在好了时发一次）
        if self.alert_recovery:
            for key in sorted(self._alerting - bad_keys):
                self._alerting.discard(key)
                await self._send_alert(f"QQ 小号(OneBot)线体自检：{key} 已恢复正常",
                                       key=f"recover:{key}", level=_health.OK)

        for f in bad:
            key = f["key"]
            if key in self._alerting:
                continue                      # 同一项只报一次，直到恢复
            self._alerting.add(key)
            await self._send_alert(_health.alert_text([f]), key=key, level=f.get("level", _health.WARN))

    async def _send_alert(self, text: str, *, key: str, level: str = _health.FAIL) -> None:
        """把一条人话告警发出去。**只走已有通道，绝不自建外挂进程。**

        通道 1：`POST http://172.17.0.1:8098/report`（AstrBot 的 hermes_report 接收端，
                token 从 `.report_token` 读；会把告警发到主人私聊）
        通道 2（兜底）：追加进 `<HERMES_HOME>/onebot-alerts.log`（她/人一定能看到）
        """
        now = time.time()
        last = self._alert_last_at.get(key, 0.0)
        if now - last < self.alert_cooldown:
            return
        self._alert_last_at[key] = now
        self._alerts_sent += 1
        logger.warning("[onebot] ALERT: %s", text.replace("\n", " | "))

        # 通道 2 先落盘：HTTP 挂了也不能丢证据
        try:
            self.alert_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.alert_log_path.open("a", encoding="utf-8") as fh:
                fh.write(f"[{_clock.full_stamp()}] {text}\n")
        except Exception as e:
            logger.debug("[onebot] alert log write failed: %s", e)

        if not (self.alert_enabled and self.alert_report_url):
            return
        token = ""
        try:
            p = Path(self.alert_token_file)
            if p.is_file():
                token = \"<SECRET>\"(encoding="utf-8").strip()
        except Exception as e:
            logger.debug("[onebot] alert token unreadable: %s", e)
        if not token:
            \"<SECRET>\"("[onebot] alert channel has no token; only the local alert log was written")
            return

        import aiohttp
        _title = ("QQ 小号(OneBot)线体自检异常" if level == _health.FAIL
                  else "QQ 小号(OneBot)线体自检提醒")
        _status = ("fail" if level == _health.FAIL
                   else ("ok" if level == _health.OK else "partial"))
        url, headers, payload = _build_alert_request(
            self.alert_report_url, token, magicpush=self.alert_magicpush,
            status=_status, title=_title, text=text, now=now)
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            # trust_env/proxy=None：172.17.0.1 是 docker 网关，走代理会静默断链
            async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as sess:
                async with sess.post(url, json=payload,
                                     headers=headers, proxy=None) as resp:
                    body = await resp.text()
                    logger.info("[onebot] alert delivered via report channel (http %s)", resp.status)
                    if resp.status >= 400:
                        logger.warning("[onebot] alert channel returned %s: %s", resp.status, body[:200])
        except Exception as e:
            logger.warning("[onebot] alert delivery failed (logged locally instead): %s", e)

    # ── 连接 ───────────────────────────────────────────────────────────────

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        try:
            from aiohttp import web
        except ImportError as e:  # pragma: no cover
            return self._fail("missing_deps", f"aiohttp unavailable: {e}", retryable=False)

        if not self._acquire_platform_lock(
            "onebot", f"{self.host}:{self.port}", f"OneBot11 reverse-WS listener {self.host}:{self.port}"
        ):
            return False

        app = web.Application()
        for path in _WS_PATHS:
            app.router.add_get(path, self._ws_handler)
        runner = web.AppRunner(app, access_log=None)
        try:
            await runner.setup()
            site = web.TCPSite(runner, self.host, self.port)
            await site.start()
        except OSError as e:
            with contextlib.suppress(Exception):
                await runner.cleanup()
            self._release_platform_lock()
            return self._fail("bind_failed", f"cannot bind {self.host}:{self.port}: {e}", retryable=True)

        self._app, self._runner, self._site = app, runner, site
        self._mark_connected()
        self.write_state_file()
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        # 表情打标 worker：连上再起（离线时没必要占资源）
        self.sticker_labeler.start()
        # 群号名单：连上就跟协议端要一次（权威），别等收到群消息才「学」——
        # 学晚了会把群当成私聊（2026-10-04 事故：群「<GROUP_NAME>」<GROUP_ID2> 混进了主动私聊名单，
        # 她下午给它发「主动私聊」→ 协议端 retcode=1200 无法获取用户信息 → 连发失败 + 告警）。
        asyncio.create_task(self._load_group_ids())
        logger.info(
            "[onebot] reverse-WS listener on %s:%s (auth=%s, read_only=%s, debounce=%ss/%ss scope=%s, group=%s)",
            self.host, self.port, "on" if self.access_token else "OFF",
            self.read_only, self.debounce_seconds, self.debounce_max_seconds, self.debounce_scope,
            self.group_mode(),
        )
        if not self.access_token:
            \"<SECRET>\"("[onebot] no access_token configured — any local client may connect")
        if self.read_only:
            logger.warning("[onebot] read_only=True — inbound accepted, outbound SUPPRESSED (by design)")
        return True

    async def disconnect(self) -> None:
        with contextlib.suppress(Exception):
            self._release_platform_lock()
        await cancel_task(self._watchdog_task)
        self._watchdog_task = None
        await self.sticker_labeler.stop()
        self._client_connected = False
        self._mark_disconnected()
        self.write_state_file()
        self._flush_backfill_cursor()      # 关门前把游标记住（补投不重复的最后一道保险）
        for task in list(self._ingest_tasks):
            await cancel_task(task)
        self._ingest_tasks.clear()
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        if self._ws is not None and not self._ws.closed:
            with contextlib.suppress(Exception):
                await self._ws.close(code=1000, message=b"adapter shutdown")
        self._ws = None
        with contextlib.suppress(Exception):
            if self._runner is not None:
                await self._runner.cleanup()
        self._app = self._runner = self._site = None
        logger.info("[onebot] disconnected")

    # ── 入站：WS handler（**不得阻塞接收循环**）─────────────────────────────

    async def _ws_handler(self, request):
        from aiohttp import web

        if not self._authorized(request):
            logger.warning("[onebot] rejected WS connection from %s (bad/missing token)",
                           _redact_addr(request.remote))
            return web.Response(status=401, text="unauthorized")

        ws = web.WebSocketResponse(heartbeat=30.0, max_msg_size=4 * 1024 * 1024)
        await ws.prepare(request)
        async with self._ws_lock:
            if self._ws is not None and not self._ws.closed:
                with contextlib.suppress(Exception):
                    await self._ws.close(code=1000, message=b"replaced by new connection")
            self._ws = ws
        self._client_connected = True
        self._last_client_ts = time.time()
        self.write_state_file()
        logger.info("[onebot] client connected from %s", _redact_addr(request.remote))
        # 她刚上线 → 排一次补投（延迟几秒等协议端热身；同一时刻只跑一次）
        if self.offline_backfill_enabled and not self._backfill_running:
            task = asyncio.create_task(self._online_backfill())
            self._ingest_tasks.add(task)
            task.add_done_callback(self._ingest_tasks.discard)

        try:
            async for raw in ws:
                if raw.type is not raw.type.TEXT:  # type: ignore[attr-defined]
                    continue
                try:
                    payload = json.loads(raw.data)
                except (ValueError, TypeError):
                    logger.warning("[onebot] dropped non-JSON frame")
                    continue
                if not isinstance(payload, dict):
                    continue
                if "echo" in payload and "post_type" not in payload:
                    self._resolve_echo(payload)
                    continue
                # 每条事件一个独立任务：leader 会 await 防抖窗口，
                # 若同步 await 会卡死接收循环 → follower 永远进不来。
                task = asyncio.create_task(self._ingest(payload))
                self._ingest_tasks.add(task)
                task.add_done_callback(self._ingest_tasks.discard)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("[onebot] WS loop error: %s", e)
        finally:
            async with self._ws_lock:
                if self._ws is ws:
                    self._ws = None
            if self._ws is None:
                self._client_connected = False
                self.write_state_file()
            logger.info("[onebot] client disconnected")
        return ws

    def _authorized(self, request) -> bool:
        if not self.access_token:
            \"<SECRET>\" True
        header = request.headers.get("Authorization", "")
        token = \"<SECRET>\"[7:].strip() if header.lower().startswith("bearer ") else ""
        if not token:
            token = (request.query.get("access_token") or "").strip()
        return token == self.access_token

    # ── 入站：事件归并 ─────────────────────────────────────────────────────

    async def _ingest(self, event: Dict[str, Any]) -> None:
        try:
            self._last_rx_ts = time.time()
            # ① 分流：`notice`（入群/退群/撤回/禁言）与 `request`（加群/加好友）
            #    **绝不能**被 should_ignore 的「不是消息事件」一句话丢掉 ——
            #    群管理（入群欢迎/防撤回/入群审批）全靠这两类当原料。
            ekind = _proto.classify_event(event)
            if ekind in ("notice", "request"):
                await self._route_group_admin(event)
                return
            if ekind != "message":
                self._dropped_count += 1
                return

            is_group = _proto.is_group(event)
            # 群聊走「窗口口径」判空（纯图/纯表情也算有内容），私聊保持原样。
            reason = _proto.should_ignore(event, media_counts_as_text=is_group)
            if reason:
                self._dropped_count += 1
                logger.debug("[onebot] ignored event (%s)", reason)
                return
            if not self._authorized_sender(event):
                self._dropped_count += 1
                return
            self._rx_count += 1

            text = _proto.extract_text(event)
            gid = _proto.group_id(event)
            uid = _proto.sender_id(event)
            if not is_group:
                # 他说话了 → 主动私聊斜坡归零（主人：「期间发生对话就重置到 0%」）
                self._proactive_activity(uid)
            # 补投游标：这条到过哪（上线补投只看游标之后的消息）
            self._mark_seen(gid=gid if is_group else "", uid=uid, ts=event.get("time"))
            # 记住这条入站消息 id —— 出站「贴表情回应」默认贴在它上面
            # （send() 里优先用 reply_to，没有就退到这条）
            mid_in = event.get("message_id")
            if mid_in is not None:
                self._last_inbound_mid[str(gid if is_group else uid)] = str(mid_in)
            if is_group:
                self.group_ids.add(gid)
                # ① 连贯层：把这条收进本群的滚动窗口（只落窗口，不入主库）
                #    **先落盘再入窗**（2026-10-03 改）：窗口是她"这轮能看到什么"的底稿，
                #    只存 `[图片]` 占位的话她永远只能看到俩字（主人亲测："只有个「图片」
                #    俩字，啥也看不到"）。现在窗口里存的是 `[图片:/…/x.jpg]` /
                #    `[表情包:/…/x.gif]`，她被叫醒时翻窗口就能直接看图（含动图）。
                #    代价：群里的图不再"只在她被叫醒时才下载"——上限仍受
                #    `media_max_per_msg`(4)/`media_max_bytes`(8M) 约束；要退回旧行为
                #    把 `media_group_download` 设 false。
                win_text = await self._group_text_with_media(event)
                self._collect_group(event, gid=gid, uid=uid, text=win_text)
                # ② 唤醒判定（C1 三态）：命中 @/被回复/点名（或 full）才往下走；
                #    其余群消息到此为止 —— 与 B 阶段完全一致：0 LLM、0 记忆写入。
                # 语音：判定要用**转写后**的正文，否则 record 段在判定眼里只有 "[语音]"
                # 占位 = 没内容（2026-10-04 主人报「发语音从没叫醒过她」的根因）。
                # 转写不额外花钱：窗口那条路刚做过，结果缓存在 event["_voice_texts"]。
                score_text = self._group_score_text(event)
                trigger = self._group_wake_trigger(event, gid=gid, score_text=score_text)
                # ②.5 判定**之后**才把这条喂给门控状态（2026-10-03 修 bug）：
                #     顺序反了的话，打分时这条消息已经躺在"最近消息"里 → 自己跟自己
                #     100% 相似 → 每条群消息都被恒定扣 −0.25「复读」，等于把门槛从
                #     0.35 悄悄抬到 0.60（主人报"她老不吭声"的直接原因之一）。
                #     喂在这里，唤醒与不唤醒的路径都会走到 —— 复读/闲置才算得准。
                self._note_gate_incoming(event, gid=gid)
                if not trigger:
                    return
            else:
                trigger = ""

            # ③ 媒体落盘（图片 → 本地文件）：放在**唤醒判定之后**。
            #    群聊里绝大多数消息不唤醒她，先下载等于把每条群消息的图都拉一遍
            #    （磁盘 / 带宽 / 时间都不划算）。没落盘的图保留 `[图片]` 占位。
            #    防抖路径也吃到这个结果：`text` 会被 buffer 进 debounce 再合并。
            text = await self._text_with_media(event)

            # 会话标识 —— 必须与 Hermes 的 session_key 同源且稳定。
            umo = self._session_umo(is_group, gid, uid)

            key = _db.build_key(umo, group_id=gid, sender_id=uid, scope=self.debounce_scope)
            if not self.debounce_enabled or key is None or _db.is_command(text, self.debounce_wake_prefixes):
                # 不在防抖范围（群聊默认关 / 是指令）→ 立刻单发一轮。
                await self._dispatch(text, event, is_group=is_group, gid=gid, uid=uid, trigger=trigger)
                return

            rnd, is_leader = self._debounce.decide(key, text)
            if not is_leader:
                logger.debug("[onebot] debounce follower (round now %d msgs)", rnd.count)
                return

            elapsed = await _db.wait_window(rnd)
            self._debounce.close(key, rnd)
            merged = rnd.merged()
            if not merged:
                return
            logger.info(
                "[onebot] debounce released: %d msgs / %d chars / waited %.1fs (%s)",
                rnd.count, len(merged), elapsed, "hard-cap" if rnd.hard_cap_hit() else "quiet-window",
            )
            await self._dispatch(merged, event, is_group=is_group, gid=gid, uid=uid, trigger=trigger)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[onebot] failed to ingest event")

    # ── 入站：群聊采集（B 阶段「只看不说」）────────────────────────────────

    def _collect_group(self, event: Dict[str, Any], *, gid: str, uid: str,
                       text: Optional[str] = None, ts: Optional[float] = None) -> None:
        """把一条群消息收进**该群**的滚动窗口。

        **本方法里没有任何记忆/LLM 调用**（源码级守卫见
        `tests/check_group_window.py::TestNoMemorySink`）：它只做三件事 ——
        取窗口口径文本 → 追进 `GroupWindow` → 记账。窗口有双上限（条数 + 字节），
        写失败只累加 `errors`，绝不影响接收循环、绝不抛。
        """
        if not (self.group_enabled and self.group_collect_enabled):
            return
        text = text if text is not None else _proto.extract_window_text(event)
        if not text:
            return
        self._group_rx_count += 1
        # ⚠️ 这里**不喂门控状态**：喂早了会让打分把这条消息当"复读"（见 `_ingest` ②.5）。
        # 群管理也要一份（防撤回/违禁词要回溯原文）。纯内存，模块没开就是空操作。
        if self.group_admin is not None:
            try:
                self.group_admin.note_message(event)
            except Exception:  # noqa: BLE001
                logger.exception("[onebot] 群管理缓存消息失败（不影响采集）")
        rec = self.group_window.append(
            gid, text=text, uid=uid, name=_proto.sender_name(event),
            ts=ts if ts is not None else event.get("time"))   # ts：上线补投要按历史时刻入窗
        if rec is not None:
            logger.info("[onebot] group collected (%d total): %s | %s",
                        self._group_rx_count, self.group_window.summary(), _brief(text))

    def _gate_state(self, gid: str) -> Any:
        """取（或建）本群的 gated 门控状态 —— 纯内存，进程重启即清零。"""
        st = self._group_gate_state.get(str(gid))
        if st is None:
            st = _gwk.GateState()
            self._group_gate_state[str(gid)] = st
        return st

    # ── 入站：群管理链路（notice / request）──────────────────────────────

    async def _route_group_admin(self, event: Dict[str, Any]) -> None:
        """把 `notice` / `request` 交给群管理，并把产出的动作落成真 OneBot 帧。

        群管理没开或模块不可用 → 只计数、不报错。这是**安全降级**：
        新功能没上线时，notice/request 的行为与以前完全一致（就是被丢掉）。
        """
        admin = self.group_admin
        if admin is None:
            self._dropped_count += 1
            return
        # 「加群申请」记一份待审批队列（2026-10-03）：`flag` 只在事件里出现一次，
        # 她看不到 flag（上下文只给 QQ 号）→ 这里按**申请人 QQ** 存下来，
        # 她写 `[同意入群:QQ号]` / `[拒绝入群:QQ号,理由]` 时再由 `_admin_intents` 取用。
        try:
            # ⚠️ NapCat 的加群申请是 `request_type="group"`（子类型在 `sub_type`），
            #    不是 `"add"` —— 2026-10-03 这里只认 `"add"`，导致「她自己审」的回合
            #    从来没起来过（测试夹具也照着这个错值造，所以一直是绿的）。
            if str(event.get("post_type") or "") == "request" \
                    and str(event.get("request_type") or "") in ("group", "add"):
                qq = str(event.get("user_id") or "").strip()
                flag = str(event.get("flag") or "").strip()
                if qq and flag:
                    gid = str(event.get("group_id") or "")
                    self._pending_joins[qq] = {"flag": flag, "gid": gid,
                                               "comment": str(event.get("comment") or "")[:80],
                                               "ts": time.time()}
                    while len(self._pending_joins) > self.PENDING_JOIN_MAX:
                        self._pending_joins.popitem(last=False)
                    logger.info("[onebot] 收到加群申请（申请人 %s…，群 %s）→ 进待审批队列",
                                qq[:2] + "***", _mask(gid))
                    # 主人 2026-10-03：「让她自己审，不要让别人提醒」→ 直接给她起一个审申请回合
                    await self._review_join_request(
                        gid=gid, qq=qq, comment=str(event.get("comment") or ""))
        except Exception:  # noqa: BLE001
            logger.exception("[onebot] 记录加群申请失败（已跳过）")
        try:
            intents = admin.handle(event)
        except Exception:  # noqa: BLE001
            logger.exception("[onebot] 群管理处理事件失败（已跳过，不影响接收循环）")
            return
        if intents:
            await self._execute_intents(intents)

    async def _execute_intents(self, intents: Any) -> None:
        """执行群管理产出的动作。**一个失败不拖垮其余** —— 逐条独立 try。

        动作是 OneBot action 帧，与发送链路走同一个 `_call_action`（复用重试与记账）。

        ⚠️ `ACTION_INTERNAL`（`internal_noop`）**不是真 action** —— 那类 Intent 只带
        "给她看一眼"的说明（如"有人撤回了消息但缓存里没有原文"）。把它当 action 发出去
        会得到「未知 action」错误，所以必须在这里分流。
        """
        for it in intents:
            params = dict(getattr(it, "params", None) or {})
            action = str(getattr(it, "action", "") or "")
            kind = str(getattr(it, "kind", "") or "?")
            note = str(getattr(it, "note", "") or "")
            if not action:
                continue
            if action == getattr(_gadmin, "ACTION_INTERNAL", "internal_noop"):
                # 只记不留帧：进内存笔记 + 日志，供观察（暂不自动注入 LLM 上下文）
                self._group_admin_notes.append((time.time(), kind, note))
                logger.info("[onebot] 群管理 %s（仅提示，不发请求）：%s", kind, note)
                continue
            # 群管理用 `{at}` 生成的是裸 CQ 码文本。数组格式里文本段内的 CQ 码各协议端
            # 表现不一致 —— 这里统一转成**真的 at/image/face 段**，不用赌 NapCat 会不会解析。
            msg = params.get("message")
            if isinstance(msg, list):
                flat: List[Dict[str, Any]] = []
                for seg in msg:
                    if isinstance(seg, dict) and seg.get("type") == "text":
                        flat.extend(_proto.cq_to_segments(
                            str((seg.get("data") or {}).get("text") or "")))
                    else:
                        flat.append(seg)
                params["message"] = flat
            if self.self_id:
                params.setdefault("self_id", _proto._num(self.self_id))
            try:
                resp = await self._call_action({"action": action, "params": params})
                ok = resp is not None and (
                    str(resp.get("status", "")).lower() in ("ok", "async")
                    or resp.get("retcode") == 0)
                self._group_admin_actions += 1
                logger.info("[onebot] 群管理 %s → %s %s",
                            kind, action, "ok" if ok else "未成功")
            except Exception:  # noqa: BLE001
                logger.exception("[onebot] 群管理动作 %s 执行失败", action)

    # ── 入群申请：让她自己审（2026-10-03）──────────────────────────────────

    #: 每群复审冷却 / 每小时上限 —— 申请洪水（一次几十个）不能起几十个付费回合
    JOIN_REVIEW_COOLDOWN_S = 60.0
    JOIN_REVIEW_MAX_PER_HOUR = 10

    async def _review_join_request(self, *, gid: str, qq: str, comment: str) -> None:
        """有人申请入群 → 她自己审（宽松放行）；批/拒完**不在群里说话**。

        主人 2026-10-03 两次定性：
          * 「加上吧，这个让她自己审不要让别人提醒」→ 申请不再转给主人；
          * 「我想让她能直接自己看到申请并看是不是通过，我更倾向于宽松审核……
             申请一小时不要给她发太多，不要反馈到群里」。

        做法：① 往该群窗口塞一条**系统记录**（含申请人 QQ 与附言，她读得到）——
              每条申请都塞，超频直接放行的也塞，好让她事后能对账/踢人；
              ② 按 ``trigger="join_request"`` 走正常派发链路，她写
              `[同意入群:…]` / `[拒绝入群:…]`，`send()` 里翻成真动作；
              ③ 该轮正文**静默**（`_silent_group_turn`）：只有标记生效，群里看不到她说的话。

        护栏：群功能没开 / 唤醒=collect-only → 不审；每群 60s 冷却；
        每群每小时最多唤醒 ``join_review_max_per_hour`` 次（默认 1，配置可调），
        **超限的直接放行**（宽松，真有问题后面踢），不花 AI 的钱。
        """
        if not gid or not self.group_enabled:
            return
        # ① 先留痕：审不审都要有这条（她下次被叫醒时能看见、能踢人）
        try:
            self.group_window.append(
                gid, uid="", name="[系统]",
                text="有人申请加入本群。申请人 QQ：%s；附言：%s" % (qq, comment.strip() or "（没写）"))
        except Exception:  # noqa: BLE001 —— 写窗口失败不该挡住后面的判断
            logger.exception("[onebot] 入群申请写窗口失败（不影响复审）")
        if self.group_wake_mode == _gwk.MODE_COLLECT:
            logger.info("[onebot] 有人申请入群（%s…）但群唤醒=collect-only → 只记不审",
                        qq[:2] + "***")
            return
        now = time.time()
        if now - float(self._join_review_last_ts.get(gid, 0.0)) < self.JOIN_REVIEW_COOLDOWN_S:
            logger.info("[onebot] 入群申请复审在冷却中（群 %s）→ 不唤醒（申请留着，她手动批也行）",
                        _mask(gid))
            return
        recent = [t for t in (self._join_review_ts.get(gid) or []) if now - t < 3600.0]
        if len(recent) >= int(self.join_review_max_per_hour):
            logger.info("[onebot] 入群申请超过每小时唤醒上限 %d（群 %s）→ 直接放行 %s",
                        int(self.join_review_max_per_hour), _mask(gid), qq[:2] + "***")
            await self._approve_join_silently(qq=qq, why="超过每小时唤醒上限")
            return
        recent.append(now)
        self._join_review_ts[gid] = recent
        self._join_review_last_ts[gid] = now
        ev = {"post_type": "request", "request_type": "add",
              "group_id": _proto._num(gid), "user_id": _proto._num(qq), "comment": comment}
        # ③ 这一轮的正文不发到群里（主人：「不要反馈到群里」）
        self._silent_group_turn = (str(gid), time.time())
        try:
            await self._dispatch("", ev, is_group=True, gid=gid, uid=qq, trigger="join_request")
            self._join_reviews += 1
        except Exception:  # noqa: BLE001
            self._silent_group_turn = None
            logger.exception("[onebot] 入群申请复审回合派发失败")

    async def _approve_join_silently(self, *, qq: str, why: str) -> bool:
        """把「申请洪峰里的那些」直接放行：不发消息、不唤醒 AI，只出一帧审批。

        主人：「我更倾向于宽松审核，反正要入群都会说几句话，有问题踢」
        """
        pend = self._pending_joins.get(qq) or {}
        flag = str(pend.get("flag") or "")
        if not flag:
            logger.warning("[onebot] 想自动放行 %s 但队列里没 flag（申请太老或已被处理）→ 放弃",
                           qq[:2] + "***")
            return False
        await self._execute_intents([SimpleNamespace(
            action="set_group_add_request",
            params={"flag": flag, "sub_type": "add", "approve": True, "comment": ""},
            kind="request_approve_overflow",
            note="入群申请%s → 放行 %s" % (why, qq))])
        self._pending_joins.pop(qq, None)
        logger.info("[onebot] 入群申请%s → 已静默放行 %s（不进群、不唤醒 AI）",
                    why, qq[:2] + "***")
        return True

    # ── 出站：把她的群管理标记翻译成动作（2026-10-03）────────────────────────

    #: 一条消息里最多认几个动作（防提示注入式刷屏；多余的直接丢）
    ADMIN_MAX_PER_MESSAGE = 3
    #: 文案类动作的长度上限：头衔按 QQ 侧硬限制（最多 6 个字），群名/名片各 30，公告 500
    ADMIN_TEXT_LIMITS: Dict[str, int] = {"special_title": 6, "card": 30,
                                         "group_name": 30, "notice": 500}
    #: 待审批队列容量与有效期（加群申请过了就失效，防她拿着旧 QQ 号乱批）
    PENDING_JOIN_MAX = 20
    PENDING_JOIN_TTL = 24 * 3600.0

    def _admin_intents(self, parsed: Any, chat_id: str) -> List[Any]:
        """把 ``extract_admin_markers`` 的解析结果翻成 Intent 列表，**在这里做全部硬护栏**。

        硬护栏（提示词拦不住的，代码必须拦）：
          * 号码必须是数字；**不许对主人动手**（trust tier owner）；
          * 不许对她自己动手（禁言自己 = 把自己关掉）；
          * 时长钳在 60s~``commands.max_ban_seconds``（默认上限 30 天）；
          * 只在群聊里执行（调用方已判），一条消息最多 ``ADMIN_MAX_PER_MESSAGE`` 个。
        """
        admin = self.group_admin
        cfg = dict(getattr(admin, "cfg", None) or {})
        cmd = cfg.get("commands") or {}
        default_s = _as_int_safe(cmd.get("default_ban_seconds"), 300, minimum=60)
        max_s = _as_int_safe(cmd.get("max_ban_seconds"), 2592000, minimum=60)
        owners = {str(x) for x in (getattr(self, "trust_tier_owner_ids", ()) or ())}
        self_id = str(getattr(self, "self_id", "") or "")
        gid = _proto._num(chat_id)
        out: List[Any] = []
        for item in list(parsed or [])[: self.ADMIN_MAX_PER_MESSAGE]:
            kind = str(item.get("kind") or "")
            qq = str(item.get("qq") or "")

            # ① 全体禁言：没有目标，单独处理（她写 [全体禁言:开] / [全体禁言:关]）
            if kind == "whole_ban":
                on = str(item.get("on") or "") == "1"
                logger.info("[onebot] 群管理（她自己发起）全体禁言 → %s，群 %s",
                            "开" if on else "关", _mask(chat_id))
                out.append(getattr(_gadmin, "Intent")(
                    action="set_group_whole_ban",
                    params={"group_id": gid, "enable": bool(on)},
                    kind="her_whole_ban",
                    note="她把全群禁言%s了" % ("开" if on else "关")))
                continue

            # ①b 全群可见的改文案类（她写 [公告:今晚维护] / [改群名:新名字]）——没有目标人，
            #    所以放在"号码校验"前面；长度封顶，防她把整段话糊上去。
            if kind in ("notice", "group_name"):
                text_val = str(item.get("text") or "").strip()
                if not text_val:
                    logger.warning("[onebot] 群管理：%s 内容为空 → 不执行", kind)
                    continue
                limit = self.ADMIN_TEXT_LIMITS.get(kind, 60)
                text_val = text_val[:limit]
                action = "_send_group_notice" if kind == "notice" else "set_group_name"
                params_c: Dict[str, Any] = {"group_id": gid}
                params_c["content" if kind == "notice" else "group_name"] = text_val
                logger.info("[onebot] 群管理（她自己发起）%s → 群 %s", kind, _mask(chat_id))
                out.append(getattr(_gadmin, "Intent")(
                    action=action, params=params_c, kind="her_%s" % kind,
                    note="她改了群里的%s" % ("公告" if kind == "notice" else "群名")))
                continue

            if not qq.isdigit():
                continue
            if qq in owners:
                logger.warning("[onebot] 群管理：目标(%s)是主人 → 拒绝执行 %s",
                               qq[:2] + "***", kind)
                continue
            if self_id and qq == self_id:
                logger.warning("[onebot] 群管理：目标是她自己 → 拒绝执行 %s", kind)
                continue

            # ② 加群审批：没有对应的待审批申请 → 什么也不做（防她凭一个号码乱批人）
            if kind in ("approve_join", "reject_join"):
                pend = self._pending_joins.get(qq)
                if not pend or (time.time() - float(pend.get("ts") or 0)) > self.PENDING_JOIN_TTL:
                    if pend:
                        self._pending_joins.pop(qq, None)
                    logger.warning("[onebot] 群管理：%s 没有有效的待审批申请（%s…）→ 不执行",
                                   qq[:2] + "***", kind)
                    continue
                approve = kind == "approve_join"
                params_j: Dict[str, Any] = {"flag": pend.get("flag"), "sub_type": "add",
                                            "approve": approve}
                reason = str(item.get("reason") or "").strip()
                if not approve and reason:
                    params_j["reason"] = reason
                self._pending_joins.pop(qq, None)     # 一条申请只批一次
                logger.info("[onebot] 群管理（她自己发起）%s → 申请人 %s…",
                            "同意入群" if approve else "拒绝入群", qq[:2] + "***")
                out.append(getattr(_gadmin, "Intent")(
                    action="set_group_add_request", params=params_j,
                    kind="her_approve_join" if approve else "her_reject_join",
                    note="她对一条入群申请做了%s" % ("同意" if approve else "拒绝")))
                continue

            # ③ 撤回：她只有号码，没有 message_id → 从群管理缓存里翻出这个人最近一条
            if kind == "recall":
                cache = getattr(admin, "cache", None)
                mid = cache.last_mid_by_user(chat_id, qq) if cache is not None else None
                if not mid:
                    logger.warning("[onebot] 群管理：撤回 %s… 失败——缓存里没有他最近的消息",
                                   qq[:2] + "***")
                    continue
                logger.info("[onebot] 群管理（她自己发起）撤回 → %s… 最近一条", qq[:2] + "***")
                out.append(getattr(_gadmin, "Intent")(
                    action="delete_msg", params={"message_id": _proto._num(mid)},
                    kind="her_recall", note="她撤回了群里某人的一条消息"))
                continue

            # ③b 精华 / 取消精华：和撤回一样只有号码 → 从缓存换 message_id
            if kind in ("essence", "unessence"):
                cache = getattr(admin, "cache", None)
                mid = cache.last_mid_by_user(chat_id, qq) if cache is not None else None
                if not mid:
                    logger.warning("[onebot] 群管理：%s %s… 失败——缓存里没有他最近的消息",
                                   kind, qq[:2] + "***")
                    continue
                action = "set_essence_msg" if kind == "essence" else "delete_essence_msg"
                logger.info("[onebot] 群管理（她自己发起）%s → %s… 最近一条",
                            kind, qq[:2] + "***")
                out.append(getattr(_gadmin, "Intent")(
                    action=action, params={"message_id": _proto._num(mid)},
                    kind="her_%s" % kind,
                    note="她%s了群里某人的一条消息" % ("加精" if kind == "essence" else "取消加精")))
                continue

            params: Dict[str, Any] = {"group_id": gid, "user_id": _proto._num(qq)}
            if kind == "ban":
                secs = _as_int_safe(item.get("seconds"), default_s, minimum=60)
                secs = min(secs, max_s)
                params["duration"] = secs
                action = "set_group_ban"
            elif kind == "unban":
                params["duration"] = 0
                action = "set_group_ban"
            elif kind == "kick":
                action = "set_group_kick"
            elif kind in ("special_title", "card"):
                # 头衔（QQ 侧最多 6 个字）/ 群名片：文本长度按 ADMIN_TEXT_LIMITS 封顶
                text_val = str(item.get("text") or "").strip()
                if not text_val:
                    logger.warning("[onebot] 群管理：%s 没给内容 → 不执行（她写的是空尾巴）", kind)
                    continue
                text_val = text_val[:self.ADMIN_TEXT_LIMITS.get(kind, 6)]
                if kind == "special_title":
                    action = "set_group_special_title"
                    params["special_title"] = text_val
                else:
                    action = "set_group_card"
                    params["card"] = text_val
            elif kind in ("set_admin", "unset_admin"):
                action = "set_group_admin"
                params["enable"] = kind == "set_admin"
            elif kind == "poke":
                action = "group_poke"
            else:
                continue
            logger.info("[onebot] 群管理（她自己发起）%s → 目标 %s… 群 %s",
                        kind, qq[:2] + "***", _mask(chat_id))
            out.append(getattr(_gadmin, "Intent")(
                action=action, params=params, kind="her_%s" % kind,
                note="她自己决定对群里某人执行了 %s" % kind))
        if len(parsed or []) > self.ADMIN_MAX_PER_MESSAGE:
            logger.warning("[onebot] 群管理：一条消息里给了 %d 个动作，只执行前 %d 个",
                           len(parsed), self.ADMIN_MAX_PER_MESSAGE)
        return out

    # ── 入站：群聊唤醒判定与回合文本（C1「@ 必答」）────────────────────────

    def _note_gate_incoming(self, event: Dict[str, Any], *, gid: str) -> None:
        """把这条群消息喂给门控状态（复读/闲置/自说率的原料）。

        **必须在唤醒判定之后调用**：`gate_score` 会拿这条去和"最近消息"比相似度，
        先喂就会跟它自己比 → 恒定 100% 相似 → 每条都被扣 −0.25「复读」。
        （2026-10-03 修的 bug：线上日志里每条群消息都带 `-0.25 复读/刷屏`。）

        唤醒与不唤醒两条路径都要喂 —— 只喂唤醒的那些，复读判定就废了。
        """
        if not (self.group_enabled and self.group_collect_enabled):
            return
        text = _proto.extract_window_text(event)
        if not text:
            return
        # 语音：门控状态里的「复读/刷屏」裁判也要看真内容（同一个 event 缓存，零额外成本）
        text = _proto.with_voice_texts(text, event.get("_voice_texts") or [])
        self._gate_state(gid).note_incoming(text)

    def _group_score_text(self, event: Dict[str, Any]) -> Optional[str]:
        """唤醒判定该读哪个文本：有语音段就换成**转写后**的正文，否则 None（用原口径）。

        转写不额外花钱 —— 窗口那条路（`_group_text_with_media`）刚做过，结果缓存在
        ``event["_voice_texts"]``（没语音段时是空表，这时返回 None，行为与改动前逐字一致）。
        """
        if not _proto.record_segments(event):
            return None
        return _proto.with_voice_texts(_proto.extract_text(event),
                                       event.get("_voice_texts") or [])

    def _group_wake_trigger(self, event: Dict[str, Any], *, gid: str,
                            score_text: Optional[str] = None) -> str:
        """这一条群消息要不要唤醒她？返回触发原因（``at``/``reply``/``name``/``full``）或 ``""``。

        三道闸（顺序即成本控制，任一不过就到此为止、0 token）：
          ① 三态模式（`collect-only` 直接结束）
          ② **记忆隔离硬前置**（没就位就拒绝唤醒 —— 不拿主人的主记忆库当赌注）
          ③ 命中判定 + 每分钟/每小时上限（限流**必须留痕**，不静默丢）

        ``score_text`` 是**转写后的正文**（语音专用，2026-10-04）：不给的话判定读
        `extract_text()` 里的 "[语音]" 占位 → 内容贡献恒为 0 → 语音永远叫不醒她。
        """
        if self.group_wake_mode == _gwk.MODE_COLLECT:
            return ""
        if self.group_memory_guard_required:
            iso = self._group_memory_isolation()
            if not iso.get("ok"):
                self._group_wake_blocked += 1
                logger.error(
                    "[onebot] 群唤醒被拒：**记忆隔离闸未就位**（memory.provider=%r，应为 %r；"
                    "插件在位=%s）—— 这是 C1 的硬前置，先把闸门装上（RUNBOOK「七」），别裸开群唤醒",
                    iso.get("config_provider"), iso.get("expected_provider"), iso.get("plugin_ok"))
                self.write_state_file()
                return ""
        reason, detail = _gwk.decide(
            event, self_id=self.self_id, alias_names=self.group_alias_names,
            own_message_ids=self._group_own_mids.get(gid, ()),
            reply_wakes=self.group_reply_to_her_wakes, mode=self.group_wake_mode,
            gate_state=self._gate_state(gid), gate_threshold=self.group_gate_threshold,
            text=score_text)
        if reason == "gate":
            self._gate_wake_count += 1
        if reason == "none":
            # ⚠️ 必须留痕（原为 debug → 线上根本看不见"她为什么没醒"）：
            # 主人 2026-10-03 报「不 @ 就没反应」，正是这行看不见导致无从判断。
            logger.info("[onebot] group 未唤醒（群 %s，模式=%s）：%s", _mask(gid), self.group_wake_mode, detail)
            return ""
        if not _proto.extract_text(event).strip():
            # 纯图/纯表情 @ 她：窗口里有占位（她仍能从窗口看到），但这条**不单独唤醒**
            # —— 她看不见图，醒了也只能干瞪眼（C3 表情包阶段再说）。
            logger.info("[onebot] 群唤醒跳过：这条没有可读正文（纯图/纯表情），群 %s", _mask(gid))
            return ""
        ok, why = self._group_wake_limiter.allow(gid)
        if not ok:
            self._group_wake_limited += 1
            logger.warning("[onebot] 群唤醒被限流（群 %s，%s；触发=%s）—— 不是静默丢弃",
                           _mask(gid), why, reason)
            self.write_state_file()
            return ""
        self._group_last_trigger = reason
        # 任何一次唤醒（@/被回复/点名/过闸/全量）都把她放进「对话态」：
        # 之后 3 分钟内群里不 @ 她，后续消息也会叫醒她接着聊（主人 2026-10-03 的要求）。
        self._gate_state(gid).note_conversation()
        if reason == "at":
            self._group_mention_count += 1
        elif reason == "reply":
            self._group_reply_count += 1
        elif reason == "name":
            self._group_name_count += 1
        else:
            self._group_full_count += 1
        logger.info("[onebot] 群唤醒（群 %s，触发=%s，模式=%s）：%s",
                    _mask(gid), reason, self.group_wake_mode, detail)
        return reason

    def _remember_own_mids(self, gid: str, mids: Any) -> None:
        """记住**她自己在群里发出去的**消息 id（T3「被回复」的判据来源）。

        OneBot 的 reply 段只有被引用消息的 id、没有发送者，所以只能靠「这个 id 是不是我发的」来判。
        上限 50 条/群（滚动），只保留 id 本身，不留正文。
        """
        if not mids:
            return
        lst = self._group_own_mids.setdefault(str(gid), [])
        for mid in mids:
            if mid is not None and str(mid) not in lst:
                lst.append(str(mid))
        del lst[:-50]

    def _group_turn_text(self, *, gid: str, trigger: str) -> str:
        """群回合投给她的正文：**标记行 + 该群上下文 + 场合规矩**（见 `group_wake.build_turn_text`）。

        ``delta`` 口径（默认，2026-10-03）：只发"上次被叫醒之后的新消息"，游标按群记在内存里。
        游标为空（首次/重启后）或已被窗口淘汰 → 自动退化成"最近 N 条"，宁可多给也不让她断片。
        注入失败/没东西可给 → 也退回窗口口径；**任何情况下都不许把唤醒整个炸掉**。
        """
        window_text = ""
        mode = self.group_context_inject_mode
        if self.group_context_inject_msgs > 0:
            try:
                recs: List[Dict[str, Any]] = []
                if mode == "delta":
                    recs = self.group_window.delta_since(
                        gid, self._group_inject_cursor.get(gid),
                        limit=self.group_context_inject_msgs)
                if recs:
                    window_text = self.group_window.render_records(gid, recs, delta=(mode == "delta"))
                    if mode == "delta":
                        self._group_inject_cursor[gid] = recs[-1]
                elif mode == "delta":
                    # 没新增（游标刚好停在队尾 / 窗口没采到这条）→ 退回窗口，别让她什么都看不到
                    window_text = self.group_window.render_context(
                        gid, limit=self.group_context_inject_msgs)
                else:
                    window_text = self.group_window.render_context(
                        gid, limit=self.group_context_inject_msgs)
            except Exception as e:  # noqa: BLE001 —— 窗口渲染失败不许炸唤醒
                self.group_window.errors += 1
                logger.warning("[onebot] group window render failed (group %s): %s", _mask(gid), e)
        return _gwk.build_turn_text(gid=gid, trigger=trigger, window_text=window_text,
                                    ctx_mode=mode)

    def trust_tier(self, uid: str, *, is_group: bool = False) -> str:
        """本轮该按哪一档对待（``owner`` / ``stranger``）。

        **判据层**：只看 uid + 场合，**不参与准入、不影响出站、不拦任何消息**
        —— 准入保持全开（`dm_policy: open` / `allow_from: ['*']`）是主人的明确要求。
        真正的接线（把档位告知模型 / 群门控按档分流）属于 C 阶段，见 PLAN §11。
        """
        return _trust.classify(uid, is_group=is_group, owner_ids=self.trust_tier_owner_ids)

    def _authorized_sender(self, event: Dict[str, Any]) -> bool:
        is_group = _proto.is_group(event)
        uid = _proto.sender_id(event)
        if is_group:
            if not self.group_enabled:
                logger.debug("[onebot] group message ignored (group_enabled=false)")
                return False
        else:
            if self.dm_policy == "allowlist" and uid not in self.allow_from:
                logger.info("[onebot] DM from unauthorized user ignored (allowlist, %d entries)",
                            len(self.allow_from))
                return False
            if self.dm_policy not in ("allowlist", "open"):
                return False
        return True

    @staticmethod
    def _session_umo(is_group: bool, gid: str, uid: str) -> str:
        """稳定的会话标识，形如 ``onebot:dm:<QQ>`` / ``onebot:group:<群号>:<发送者>``。

        与 AstrBot 的 ``platform_name:message_type:session_id``（astr_message_event.py:106-108）
        同构；不同的是 Hermes 侧真正的会话键由 build_session_key 从 SessionSource 现算，
        所以这里只用来做防抖分桶 + 日志辨识，不参与会话身份。
        """
        return f"onebot:group:{gid}:{uid}" if is_group else f"onebot:dm:{uid}"

    async def _dispatch(self, text: str, event: Dict[str, Any], *, is_group: bool, gid: str, uid: str,
                        trigger: str = "", internal: bool = False) -> None:
        if is_group and self.group_wake_mode == _gwk.MODE_COLLECT:
            # ★ 纵深防御：即使将来有别的路径把群消息送到这里，也拦得住 ——
            #   「不唤醒」的红线不能只靠 `_ingest` 里那一处 return。
            logger.warning("[onebot] BLOCKED group dispatch (group_wake_mode=collect-only) — "
                           "群消息只采集；要她在群里说话先看 RUNBOOK「六」")
            return
        if not is_group and not text.strip():
            return
        if is_group:
            # 群里投给她的正文 = 标记行 + 该群滚动窗口 + 场合规矩（她自己那条原始消息已在窗口末尾）。
            text = self._group_turn_text(gid=gid, trigger=trigger or self._group_last_trigger or "none")
            # 只有真的走到这一步才算「群消息起了付费回合」——这是成本量尺（`group_llm_calls`）。
            self._group_llm_calls += 1
            logger.info("[onebot] GROUP message -> agent turn (%d so far, group %s, trigger=%s) "
                        "—— 会产生 LLM 调用；群回合记忆隔离 = 走 hindsight_guard",
                        self._group_llm_calls, _mask(gid), trigger or self._group_last_trigger or "?")
        if not text.strip():
            return
        name = _proto.sender_name(event)
        mid = event.get("message_id")
        if is_group:
            source = self.build_source(
                chat_id=gid, chat_name=f"群{gid}", chat_type="group",
                user_id=uid, user_name=name, message_id=str(mid) if mid is not None else None,
            )
        else:
            source = self.build_source(
                chat_id=uid, chat_name=name, chat_type="dm",
                user_id=uid, user_name=name, message_id=str(mid) if mid is not None else None,
            )
        await self.handle_message(MessageEvent(
            text=text, message_type=MessageType.TEXT, source=source,
            user_id=uid, user_name=name,
            message_id=str(mid) if mid is not None else None,
            raw_message=event,
            reply_to_message_id=_proto.reply_message_id(event),
            # internal=True = 「机器回合」（Hermes 只让这种回合在她 `[SILENT]` 时整条不发；
            # 人回合里她装死会被判「该回不回」→ 弹出 ⚠️ 告警给主人）。补投就属于机器回合。
            internal=bool(internal),
        ))

    # ── 出站 ───────────────────────────────────────────────────────────────

    def _is_group_chat(self, chat_id: str, metadata: Optional[Dict[str, Any]]) -> bool:
        if isinstance(metadata, dict):
            ct = str(metadata.get("chat_type") or "").strip().lower()
            if ct:
                return ct in ("group", "channel", "supergroup")
        return str(chat_id) in self.group_ids

    def _auto_quote(self, *, is_group: bool, reply_to: Optional[str]) -> Optional[str]:
        """自动引用闸：私聊默认不引用、群聊默认引用（`auto_quote_dm/_group` 可改）。

        为什么要有：Hermes 通用机制会把触发那条消息的 id 当 `reply_to` 传下来，
        私聊里「触发那条」= 他这轮的第一条消息 → 每条回复都挂引用，很吵。
        显式引用（她自己在正文写 `[CQ:reply,id=…]`）不走这里，永远有效。
        """
        if not reply_to:
            return None
        if (self.auto_quote_group if is_group else self.auto_quote_dm):
            return reply_to
        logger.debug("[onebot] 自动引用关闭（%s）→ 丢掉 reply_to=%s",
                     "群" if is_group else "私聊", reply_to)
        return None

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        if self.read_only:
            logger.warning("[onebot] read_only=True — outbound suppressed (%d chars to %s)",
                           len(content or ""), _mask(chat_id))
            return SendResult(success=False, error="onebot: read_only mode suppresses all outbound sends")

        # 「让她自己考虑」的下半段：过了闸、但她自己判不该开口 → 整条回复只输出 SILENCE_MARKER。
        # 放在连通性检查**之前**：这是她的一个决定，不是投递失败 —— 协议端断线时也该照样记这笔账。
        # 出站「贴表情回应」指令：她在正文里写 [贴表情:赞] / [贴表情:76]。
        # 标记**先剥掉**（主人永远看不到它），等这条消息真发出去之后再贴 ——
        # 贴表情不是消息，不走 send 的分段/静默/分隔符清洗那条路。
        emoji_likes: List[int] = []
        if self.emoji_like_enabled and content:
            content, emoji_likes, unknown_likes = _proto.extract_emoji_like_markers(content)
            if unknown_likes:
                logger.warning("[onebot] 贴表情：认不出的名字 %s（已从正文剥掉，未执行）",
                               unknown_likes)

        # 出站「发表情包」：她在正文里写 [表情包:开心] / [表情包:/路径] —— 先剥掉，
        # 正文照常发，图片段在这条消息的末尾追加（标记绝不能漏给主人看）。
        sticker_wants: List[str] = []
        if self.sticker_send_enabled and content:
            content, sticker_wants = _proto.extract_sticker_markers(content)

        is_group = self._is_group_chat(chat_id, metadata)
        # 贴表情的锚点用「Hermes 原样传下来」的那个 —— 自动引用闸只管引用段，不动它
        # （否则私聊关掉引用会连带把贴表情的目标从「他这条」改成别的，属于越界改动）。
        _emoji_anchor = reply_to
        reply_to = self._auto_quote(is_group=is_group, reply_to=reply_to)

        # 出站「群管理动作」标记（2026-10-03）：她在正文里写 [禁言:QQ号,300] / [解禁:QQ号] / [踢出:QQ号]。
        # 与贴表情同一条规矩 —— 标记先剥掉（主人看不到），真动作单独发帧执行；
        # 动作**不是消息段**，混进 message 数组会让协议端拒掉整条消息。
        if content and self.group_admin is not None:
            content, parsed_admin, unknown_admin = _proto.extract_admin_markers(content)
            if unknown_admin:
                logger.warning("[onebot] 群管理标记：认不出的写法 %s（已从正文剥掉，未执行）",
                               unknown_admin)
            if parsed_admin:
                if is_group:
                    await self._execute_intents(self._admin_intents(parsed_admin, chat_id))
                    # 正文被标记吃干净了（她只写了 `[撤回:123]`）→ 别发一条空消息出去，
                    # 动作已经执行，这条就当"没话可说"处理（细节写进 skill group-admin）。
                    if not (content or "").strip():
                        logger.info("[onebot] 正文只剩群管理标记（群 %s）→ 动作已发，不再发空消息",
                                    _mask(chat_id))
                        self.write_state_file()
                        return SendResult(success=True, message_id=None)
                else:
                    logger.warning("[onebot] 群管理标记出现在私聊（%s）→ 已剥掉、不执行",
                                   _mask(chat_id))

        # 「审入群申请」那一轮：正文不出声（主人：「不要反馈到群里」）——
        # 放在群管理标记**之后**：她的 [同意入群:…] / [拒绝入群:…] 照样生效。
        _sil = getattr(self, "_silent_group_turn", None)
        if is_group and _sil and _sil[0] == str(chat_id) \
                and (time.time() - float(_sil[1])) < 300.0:
            self._silent_group_turn = None
            if (content or "").strip():
                logger.info("[onebot] 审申请回合的正文不出声（群 %s，丢了 %d 字）",
                            _mask(chat_id), len(content or ""))
            self.write_state_file()
            return SendResult(success=True, message_id=None)
        # 主动私聊那一轮：她自己回 `[SILENT]` 就不发（普通私聊回合不许装死 —— 他问了就得答）
        _prt = getattr(self, "_proactive_turn", None)
        if (not is_group) and _prt and _prt[0] == str(chat_id) \
                and (time.time() - float(_prt[1])) < 900.0 and _gwk.silence_of(content):
            self._proactive_turn = None
            self._silence_count += 1
            logger.info("[onebot] 主动私聊回合她选择不说（[SILENT]）→ 不发")
            self.write_state_file()
            return SendResult(success=True, message_id=None)

        if not is_group:
            # 她说话了（回他，或主动开口）→ 斜坡归零
            self._proactive_activity(chat_id)

        if is_group and _gwk.silence_of(content):
            # 她只回了个静默标记 → 群里什么都不发。
            # ⚠️ 私聊**不吞**：主人找她时她不能拿这个挡（`test_silence_marker_does_not_work_in_private`
            # 钉着这条）—— 那个场景下标记会当普通文本发出去，主人一眼能看出她在装死。
            self._silence_count += 1
            logger.info("[onebot] 她选择不开口（%s，累计 %d 次）→ 群 %s 不发消息",
                        _gwk.SILENCE_MARKER, self._silence_count, _mask(chat_id))
            self.write_state_file()
            return SendResult(success=True, message_id=None)

        # 主人 2026-10-04：「把她提示都关了，一步到位」——
        # Hermes 在**人回合**上拦下她的静默标记后，会把正文换成一段 ⚠️ 提示文案
        # （`gateway/run_turn.py` 的 `_UNEXPECTED_SILENCE_REPLY`），原先直接发到主人私聊/群里。
        # 这里按主人要求吞掉：**不发**，只记账 + 记日志（仍可查，不是黑洞）。放在
        # `_proactive_activity` 之后：这段不是"她开口了"，不该把她主动起头的斜坡归零。
        if _is_silence_warning(content):
            self._silence_warn_suppressed += 1
            logger.info("[onebot] 吞掉 Hermes 的静默提示（她这轮没话说）：%s（累计 %d 次）",
                        _mask(chat_id), self._silence_warn_suppressed)
            self.write_state_file()
            return SendResult(success=True, message_id=None)

        ws = self._ws
        if ws is None or ws.closed:
            self._record_send_failure("protocol side not connected")
            return SendResult(success=False, error="onebot: protocol side not connected", retryable=True)

        action = "send_group_msg" if is_group else "send_private_msg"

        # 发表情包（2026-10-04）：剥出来的 [表情包:…] 在这里变成真的图片段，追加在末尾。
        # 放在分段之前 —— 否则图片段会落在分段之外、或把最后一段的清洗吞掉。
        if sticker_wants:
            cq = self._sticker_cq(sticker_wants)
            if cq:
                content = (content or "") + cq

        if not self.segment_enabled:
            segments = [content]
            delays = [0.0]
        else:
            segments = _seg.split_text(content, regex=self.SEGMENT_REGEX,
                                       cleanup=self.SEGMENT_CLEANUP)
            delays = _seg.segment_intervals(len(segments), interval=self.segment_interval,
                                            leading_delay=self.segment_leading_delay)
        # 硬兜底（红线）：分隔符**任何情况下**都不许出现在主人收到的消息里。
        # 覆盖到所有出站路径 —— 分段关闭、正则写坏、模型输出怪东西，一律先洗再发。
        segments = [_seg.scrub(s, cleanup=self.SEGMENT_CLEANUP) for s in segments]
        segments = [s for s in segments if s.strip()]
        delays = delays[:len(segments)] or [0.0] * len(segments)
        if not segments:
            # 正文被剥空（她只输出了贴表情标记）→ 不消息，但把表情贴上
            await self._apply_emoji_likes(chat_id, _emoji_anchor, emoji_likes)
            return SendResult(success=True, message_id=None)

        sent_ids: List[str] = []
        for idx, (segment, delay) in enumerate(zip(segments, delays)):
            if delay > 0:
                await asyncio.sleep(delay)
            ok, mid, err = await self._send_one(
                action, chat_id, segment, is_group,
                # 引用段只挂在**第一条**上（OneBot 的 reply 段是消息级的，挂多了会把后面几条
                # 也变成引用回复）。以前 `send(reply_to=)` 定义了却没人用 —— 入站能解析引用、
                # 出站不会发，方向不对称。
                reply_to=reply_to if idx == 0 else None)
            if not ok:
                logger.error("[onebot] segment %d/%d failed: %s", idx + 1, len(segments), err)
                return SendResult(success=False, error=err, retryable=True,
                                  continuation_message_ids=tuple(sent_ids))
            self._segments_sent += 1
            self._last_tx_ts = time.time()
            if mid:
                sent_ids.append(mid)
            if is_group:
                # 她自己说过的话也进该群窗口 —— 否则「有人回复她」时她看不到自己说了啥，
                # 上下文是断的（C1 的窗口要能支撑「接着聊」）。**只进群窗口，不入主库。**
                if self.group_enabled and self.group_collect_enabled:
                    self.group_window.append(chat_id, text=segment, uid=self.self_id, name="[我]")
                self._remember_own_mids(chat_id, [mid] if mid else [])
        if is_group:
            # 记账：她刚在这个群说过话 → 下一次门控的"闲置压力"重新计时、"自说率"加一。
            self._gate_state(chat_id).note_self_spoke()
            # 同时进入/续期「对话态」：接下来 3 分钟里，群里不 @ 她也能被叫醒接着聊
            # （潜水态靠免费闸筛，对话态直接放行；她不想说话仍可回 [SILENT]）。
            self._gate_state(chat_id).note_conversation()
        self._consecutive_failures = 0
        self._last_error = ""
        self.write_state_file()
        logger.info("[onebot] sent %d segment(s) to %s", len(segments), _mask(chat_id))
        # 消息发出去了 —— 现在才贴表情（贴表情不是消息，失败也不影响已发出的消息）
        await self._apply_emoji_likes(chat_id, _emoji_anchor, emoji_likes)
        return SendResult(
            success=True,
            message_id=sent_ids[-1] if sent_ids else None,
            continuation_message_ids=tuple(sent_ids),
        )

    async def _send_one(self, action: str, chat_id: str, text: str, is_group: bool,
                        reply_to: Optional[str] = None):
        """单条发送 + 重试。绝不静默丢：耗尽重试后返回失败让上层记录/降级。

        ``reply_to``：非空时这条会带上引用段（引用回复的出站实现）。

        **最后一道闸**：这里再洗一次分隔符 —— 不管上面怎么拼出来、走哪条降级路径，
        凡是从这个方法出去的正文都不含 `⁂`/`※`/`⸮`（红线：主人永远看不到符号）。
        """
        text = _seg.scrub(text, cleanup=self.SEGMENT_CLEANUP)
        last_err = "unknown"
        for attempt in range(1, self.send_retries + 1):
            try:
                payload = _proto.build_action(action, chat_id, text, is_group=is_group,
                                              self_id=self.self_id, reply_to=reply_to)
                resp = await self._call_action(payload)
                if resp is None:
                    last_err = "protocol side not connected"
                elif str(resp.get("status", "")).lower() in ("ok", "async") or resp.get("retcode") == 0:
                    return True, _mid_of(resp), None
                else:
                    last_err = f"retcode={resp.get('retcode')} status={resp.get('status')} msg={resp.get('msg') or resp.get('message')}"
            except asyncio.TimeoutError:
                last_err = "action timeout"
            except Exception as e:
                last_err = str(e)
            if attempt < self.send_retries:
                logger.warning("[onebot] send attempt %d/%d failed (%s), retrying", attempt, self.send_retries, last_err)
                await asyncio.sleep(self.send_retry_backoff * attempt)
        self._record_send_failure(last_err)
        return False, None, last_err

    # ── 出站：贴表情回应（NapCat 扩展 set_msg_emoji_like）──────────────────

    async def emoji_like(self, message_id: Any, emoji_id: Any, *,
                         set_: bool = True) -> Tuple[bool, str]:
        """对某条 QQ 消息贴一个表情回应（或取消）。

        Args:
            message_id: 目标消息的 **短 ID**（OneBot 事件里的 ``message_id``）。
            emoji_id: QQ 内置表情编号（**数字**；本机 NapCat 实测传字符串会被 schema 拒）。
            set_: ``True`` 贴，``False`` 取消回应。

        Returns:
            ``(ok, detail)`` —— ``detail`` 是原始响应 JSON 或错误文本，供日志/回执用。

        走与发送链路同一个 ``_call_action``（复用 echo 等待与超时）。**不经分段、
        不含正文** —— 贴表情不是消息，别把它塞进 ``send()``。
        """
        if self.read_only:
            return False, "onebot: read_only 模式拦截出站"
        payload = _proto.build_emoji_like_action(message_id, emoji_id,
                                                 set_=set_, self_id=self.self_id)
        try:
            resp = await self._call_action(payload, timeout=15.0)
        except asyncio.TimeoutError:
            return False, "set_msg_emoji_like timeout"
        except Exception as e:  # noqa: BLE001
            return False, f"set_msg_emoji_like 异常: {e}"
        if resp is None:
            return False, "protocol side not connected"
        ok = (str(resp.get("status", "")).lower() in ("ok", "async")
              or resp.get("retcode") == 0)
        if not ok:
            self._media_failed += 1
        return ok, json.dumps(resp, ensure_ascii=False)

    def _sticker_stats(self) -> Dict[str, Any]:
        """表情库概况（供状态文件用）。**读索引失败只报 0，绝不影响状态写入。**"""
        try:
            return self.sticker_lib.stats()
        except Exception:  # noqa: BLE001
            return {"count": 0, "bytes": 0, "untagged": 0}

    # ── 出站：贴表情回应的接线（把正文里的 [贴表情:x] 落到真消息上）──────────

    def _emoji_like_target(self, chat_id: str, reply_to: Optional[str]) -> Optional[str]:
        """贴表情贴哪条：优先她正在回复的那条（``reply_to``），退到该会话最近一条入站消息。"""
        if reply_to:
            return str(reply_to)
        return self._last_inbound_mid.get(str(chat_id))

    def _sticker_pick(self, want: str) -> Optional[str]:
        """挑一张要发的表情包：空 = 随便一张；带斜杠/图片后缀 = 指定那张；否则按情绪挑。

        挑中顺手 `touch`（帮她自己的 LRU 记住「这张用过」，别让它被当最旧的淘汰）。
        """
        try:
            w = (want or "").strip()
            if not w:
                path = self.sticker_lib.pick(None)
            elif "/" in w or w.lower().endswith((".gif", ".jpg", ".jpeg", ".png", ".webp")):
                p = Path(w)
                path = str(p) if p.is_file() else None
            else:
                path = self.sticker_lib.pick(w)
            if path:
                with contextlib.suppress(Exception):
                    self.sticker_lib.touch(Path(path).stem)
            return path
        except Exception:  # noqa: BLE001
            logger.exception("[onebot] 挑表情包失败")
            return None

    def _sticker_cq(self, wants: List[str], *, max_n: int = 3) -> str:
        """把 ``[表情包:…]`` 变成真正的出站段文本（CQ 码，由 build_action 转段）。"""
        out: List[str] = []
        for want in wants[:max_n]:
            p = self._sticker_pick(want)
            if p:
                out.append(f"[CQ:image,file={p}]")
                self._stickers_sent += 1
                logger.info("[onebot] 发表情包（想要 %r）→ %s", want or "随机", p)
            else:
                self._sticker_send_failed += 1
                logger.info("[onebot] 表情包没挑到（想要 %r）—— 库空/标签没匹配", want)
        return "".join(out)

    async def _apply_emoji_likes(self, chat_id: str, reply_to: Optional[str],
                                 ids: List[int]) -> None:
        """对目标消息逐个贴表情回应。**绝不能**因为贴表情失败影响已发出的消息。"""
        if not ids:
            return
        target = self._emoji_like_target(chat_id, reply_to)
        if not target:
            self._emoji_like_failed += len(ids)
            logger.info("[onebot] 贴表情跳过：找不到可贴的目标消息（%s）", _mask(chat_id))
            return
        for eid in ids:
            try:
                ok, detail = await self.emoji_like(target, eid)
            except Exception as e:  # noqa: BLE001
                self._emoji_like_failed += 1
                logger.warning("[onebot] 贴表情异常（emoji_id=%s）：%s", eid, e)
                continue
            if ok:
                self._emoji_likes += 1
                logger.info("[onebot] 贴表情成功 emoji_id=%s → message_id=%s", eid, target)
            else:
                self._emoji_like_failed += 1
                logger.warning("[onebot] 贴表情失败 emoji_id=%s → message_id=%s：%s",
                               eid, target, detail)
        self.write_state_file()

    # ── 入站：媒体落盘（图片 → 本地文件，供 vision 读）──────────────────────

    async def _text_with_media(self, event: Dict[str, Any]) -> str:
        """正文 + 图片本地路径（``[图片]`` → ``[图片:/…/abc.jpg]``）。

        ``media_download_enabled=false`` 时直接返回原正文（行为与改动前一致）。
        """
        text = _proto.extract_text(event)
        if not self.media_download_enabled:
            return text
        paths = await self._persist_images(event)
        text = _proto.with_media_paths(text, paths)
        return _proto.with_voice_texts(text, await self._voice_texts(event))

    async def _group_text_with_media(self, event: Dict[str, Any]) -> str:
        """**窗口口径**的正文 + 图片本地路径（群窗口专用，2026-10-03）。

        与 `_text_with_media()` 的区别只在口径：这里用 `extract_window_text()`
        （群窗口渲染），并受 `media_group_download` 开关控制。落盘结果缓存在 event
        上，所以稍后 `_text_with_media()` 拿到的是同一批路径，**不会重复下载**。
        """
        base = _proto.extract_window_text(event)
        if not (self.media_download_enabled and self.media_group_download):
            return base
        try:
            paths = await self._persist_images(event)
        except Exception:  # noqa: BLE001 —— 落盘失败绝不打断接收循环
            logger.exception("[onebot] 群窗口图片落盘异常（保留 [图片] 占位）")
            return base
        base = _proto.with_media_paths(base, paths)
        try:
            return _proto.with_voice_texts(base, await self._voice_texts(event))
        except Exception:  # noqa: BLE001 —— 转写失败绝不打断窗口采集
            logger.exception("[onebot] 群窗口语音转写异常（保留 [语音] 占位）")
            return base

    async def _persist_images(self, event: Dict[str, Any]) -> List[Optional[str]]:
        """把这条消息里的图片落到 ``media_dir``，返回**与 image 段同序**的路径列表。

        取值顺序（任一成功即止）：
          ① ``get_image(file=…)`` 响应里的 ``base64`` —— 协议端已经把原图取回来了
          ② 同响应里的 ``file``（协议端本地路径；命中宿主挂载前缀就直接读）
          ③ 段自带的 ``url`` / 响应里的 ``url`` —— 直接 HTTP 下载（不依赖协议端）
        失败的位置是 ``None``（调用方保留 ``[图片]`` 占位，**绝不删**）。

        **每事件只跑一次**：结果缓存在 ``event["_hermes_media_paths"]``，
        同一条消息被多段逻辑复用（群采集 / 回合正文）时不会重复下载。
        """
        cached = event.get("_hermes_media_paths")
        if isinstance(cached, list):
            return cached
        segs = _proto.image_segments(event)
        if not self.media_download_enabled or not segs or self.media_max_per_msg <= 0:
            return [None] * len(segs)
        paths: List[Optional[str]] = []
        for data in segs[: self.media_max_per_msg]:
            p: Optional[str] = None
            try:
                p = await self._persist_one_image(data)
            except Exception:  # noqa: BLE001 —— 落盘失败绝不影响接收循环
                logger.exception("[onebot] 图片落盘失败（保留 [图片] 占位）")
            paths.append(p)
        # 超出上限的尾部补 None，保持与 image 段同序
        paths.extend([None] * max(0, len(segs) - len(paths)))
        event["_hermes_media_paths"] = paths
        return paths

    async def _persist_one_image(self, data: Dict[str, Any]) -> Optional[str]:
        """单张图片：取字节 → 落盘 → 返回绝对路径（失败返回 None）。"""
        blob: Optional[bytes] = None
        resp: Optional[Dict[str, Any]] = None
        fid = data.get("file")
        if fid:
            resp = await self._call_action(
                {"action": "get_image", "params": {"file": str(fid)}}, timeout=20.0)
        d = resp.get("data") if isinstance(resp, dict) and resp.get("retcode") == 0 else None
        if isinstance(d, dict):
            b64 = d.get("base64")
            if b64:
                try:
                    blob = base64.b64decode(b64)
                except Exception:  # noqa: BLE001
                    blob = None
            if blob is None:
                blob = _read_local_file(d.get("file"))
            if blob is None and d.get("url"):
                blob = await self._http_get(str(d["url"]))
        if blob is None:
            url = data.get("url") or data.get("file")
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                blob = await self._http_get(url)
        if not blob:
            self._media_failed += 1
            logger.info("[onebot] 图片未落盘（get_image 与直连都没成），保留 [图片] 占位")
            return None
        path = self._save_blob(blob, data)
        if path:
            # 表情包顺手进库（照片不动）—— 判据在 onebot_proto.is_sticker_image()
            self._register_sticker(blob, data, path)
        return path

    def _register_sticker(self, blob: bytes, data: Dict[str, Any], path: str) -> None:
        """表情包 → 记进她的表情库（去重 + 限量 + LRU 淘汰）。

        判据：``onebot_proto.is_sticker_image(data)``（sub_type ∈ {1,2,7} 或带 emoji_id
        或 summary 命中）。**照片不会进库**。任何异常只记账，绝不影响接收循环。
        """
        if not self.stickers_enabled or not _proto.is_sticker_image(data):
            return
        try:
            r = self.sticker_lib.add(blob, data, path)
        except Exception:  # noqa: BLE001
            self._stickers_failed += 1
            logger.exception("[onebot] 表情包入库异常")
            return
        if not r.get("ok"):
            self._stickers_failed += 1
            logger.info("[onebot] 表情包未入库：%s（%s）", r.get("reason"), path)
            return
        if r.get("dedup"):
            self._stickers_dedup += 1
            logger.info("[onebot] 表情包重复（已有 %s），只更新使用时间", r.get("hash"))
        else:
            self._stickers_saved += 1
            logger.info("[onebot] 表情包入库 %s（库内 %s 条，%s）",
                        r.get("hash"), r.get("count"), _proto.sticker_label(data))
        try:
            # 入库即排队打标（只入队；协议端给的人话 summary 当免费线索一起带上）
            self.sticker_labeler.note_new(r.get("hash"), _proto.sticker_label(data))
        except Exception:  # noqa: BLE001 —— 打标是锦上添花，绝不影响入库
            logger.exception("[onebot] 表情打标入队失败")
        for ev in (r.get("evicted") or []):
            logger.info("[onebot] 表情包超上限，LRU 淘汰 %s", ev.get("path"))

    async def _http_get(self, url: str) -> Optional[bytes]:
        """直接 HTTP 下载（有大小上限）。不依赖协议端，是 get_image 的兜底。"""
        try:
            import aiohttp
        except Exception:  # noqa: BLE001
            return None
        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.get(url) as r:
                    if r.status != 200:
                        return None
                    blob = await r.content.read(self.media_max_bytes + 1)
        except Exception:  # noqa: BLE001
            logger.debug("[onebot] 图片直连下载失败：%s", url[:80], exc_info=True)
            return None
        if len(blob) > self.media_max_bytes:
            logger.warning("[onebot] 图片超过上限 %d 字节，跳过", self.media_max_bytes)
            self._media_failed += 1
            return None
        return blob

    def _save_blob(self, blob: bytes, data: Dict[str, Any],
                   ext: Optional[str] = None) -> Optional[str]:
        """按内容 sha256 命名落盘（天然去重），返回绝对路径。

        ``ext`` 不为空就**用它**（语音：协议端转出来的 mp3 头是 ID3，magic 认不出来，
        认错会落成 .bin —— 她自己的 stt 工具/以后排查都得看扩展名）。
        """
        if len(blob) > self.media_max_bytes:
            self._media_failed += 1
            return None
        out_dir = self.media_dir / _clock.date()
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            p = out_dir / f"{hashlib.sha256(blob).hexdigest()[:16]}{ext or _guess_ext(blob, data)}"
            if not p.exists():
                tmp = p.with_name(p.name + ".tmp")
                tmp.write_bytes(blob)
                tmp.replace(p)
        except Exception:  # noqa: BLE001
            logger.exception("[onebot] 图片写盘失败")
            self._media_failed += 1
            return None
        self._media_saved += 1
        logger.info("[onebot] 图片落盘 %s（%d 字节）", p, len(blob))
        return str(p)

    # ── 上线补投（她刚上线：把漏掉的私聊/群消息补给她）───────────────────────

    def _load_backfill_cursor(self) -> None:
        """读游标文件（坏了就当空表——补投永远不能拦接收）。"""
        try:
            if self.backfill_path.is_file():
                data = json.loads(self.backfill_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._last_seen = {str(k): float(v) for k, v in data.items()
                                       if isinstance(v, (int, float))}
        except Exception:  # noqa: BLE001
            logger.warning("[onebot] 补投游标读不出来（当空表继续）", exc_info=True)

    def _flush_backfill_cursor(self) -> None:
        """强制把游标写盘（断开/停止时调用 —— 重启前那一批必须记住）。"""
        if self._backfill_dirty:
            self._save_backfill_cursor()

    def _save_backfill_cursor(self) -> None:
        try:
            self.backfill_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.backfill_path.with_name(self.backfill_path.name + ".tmp")
            tmp.write_text(json.dumps(self._last_seen, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.backfill_path)
            self._backfill_dirty = False
        except Exception:  # noqa: BLE001
            logger.debug("[onebot] 补投游标写盘失败", exc_info=True)

    @staticmethod
    def _cursor_key(*, gid: str, uid: str) -> str:
        return ("group:" + str(gid)) if gid else ("dm:" + str(uid))

    def _mark_seen(self, *, gid: str, uid: str, ts: Any) -> None:
        """每条入站消息更新游标（补投只看游标之后的消息 → 天然不重复）。"""
        try:
            t = float(ts)
        except (TypeError, ValueError):
            return
        key = self._cursor_key(gid=gid, uid=uid)
        if t > self._last_seen.get(key, 0.0):
            self._last_seen[key] = t
            self._backfill_dirty = True
            # 节流落盘：重启就发生在「刚处理完一批」之后，游标晚了会把已经回过的消息再补一遍
            # （2026-10-04 12:56 实测踩到：补了 5 条她早就看过的）。断开/停止时另有一次强制写。
            now = time.time()
            if now - self._backfill_saved_at >= self.offline_backfill_save_every:
                self._backfill_saved_at = now
                self._save_backfill_cursor()

    async def _online_backfill(self) -> None:
        """她刚上线：按游标把私聊/群聊漏掉的消息补给她（各取最近 N 条）。

        为什么这么实现（别绕远路）：
          * 私聊 = `_dispatch` 合成**一个**回合（不是 N 个回合，省 N-1 次 LLM）；
          * 群聊 = 追进滚动窗口（她下次被叫醒按 delta 看到）+ 过一遍唤醒判定
            （在群里 @ 过她的话，一上线就回）；
          * 已经收到的消息不会被补（按 `time > 游标` 过滤）。
        """
        if not self.offline_backfill_enabled or self._backfill_running:
            return
        self._backfill_running = True
        try:
            if self.offline_backfill_delay_s:
                await asyncio.sleep(self.offline_backfill_delay_s)   # 等协议端热身（也是启动发送失败的解药）
            chats = sorted(self._last_seen.keys())
            if not chats:
                logger.info("[onebot] 上线补投：没有已知会话（首次启动属正常）")
                return
            for key in chats:
                try:
                    if key.startswith("dm:"):
                        await self._backfill_dm(key[3:])
                    elif key.startswith("group:"):
                        await self._backfill_group(key[6:])
                except Exception:  # noqa: BLE001 —— 单个会话失败不影响别的
                    logger.exception("[onebot] 上线补投失败（%s）", key)
            # 无条件记一笔：她要能自己回答「刚才那波补投干了啥」（否则静默无操作没法查）
            logger.info("[onebot] 上线补投完成：补了 %d 条，无新消息的会话 %d 个（共看 %d 个会话）",
                        self._backfill_injected, self._backfill_skipped, len(chats))
            self._save_backfill_cursor()
        finally:
            self._backfill_running = False

    async def _history_messages(self, action: str, **params: Any) -> List[Dict[str, Any]]:
        """拿会话历史（协议端本地查询）。失败**退避重试**，最终失败返回空表。

        为什么要重试：刚上线那几秒协议端还没热身（实测启动后 ~5 秒仍会回
        `retcode=1200 无法获取用户信息`）—— 一次性查询会白白漏掉整批补投。
        重试 5 次（2s/4s/8s/16s 指数退避，共约 30s）覆盖热身完成。
        2026-10-04 实测：重启后第一轮 3 次（约 9s）还不够，`get_group_msg_history` 照样连续失败。
        彻底失败也只记日志，不拖垮接收。
        """
        attempts = 5
        for attempt in range(attempts):
            r = await self._call_action({"action": action, "params": params}, timeout=25.0)
            if isinstance(r, dict) and (r.get("status") == "ok" or r.get("retcode") == 0):
                data = r.get("data") if isinstance(r.get("data"), dict) else r
                msgs = (data or {}).get("messages")
                return [m for m in msgs if isinstance(m, dict)] if isinstance(msgs, list) else []
            # 协议端明确说「这个会话没有记录」（被清过的好友 / 测试遗留的假会话）→ 别重试，
            # 那不是热身问题，重试 5 次只是白白拖 30 秒 + 刷一屏警告（2026-10-04 踩到）。
            _msg = str((r or {}).get("message") or "") if isinstance(r, dict) else ""
            if isinstance(r, dict) and r.get("retcode") == 1200 and "不存在" in _msg:
                logger.info("[onebot] 补投查历史：%s 说「%s」—— 这个会话没记录，本次跳过不重试",
                            action, _msg)
                return []
            if attempt < attempts - 1:
                wait = min(self.offline_backfill_retry_wait * (2 ** attempt), 20.0)
                logger.info("[onebot] 补投查历史失败（第 %d 次，协议端可能还没热身），%.0fs 后重试：%s",
                            attempt + 1, wait, str(r)[:200])
                await asyncio.sleep(wait)
        logger.warning("[onebot] 补投查历史连续失败（%s）—— 本次跳过，最后一次响应：%s", action, str(r)[:200])
        return []

    def _missed(self, msgs: List[Dict[str, Any]], cursor_key: str,
                *, floor: float = 0.0) -> List[Dict[str, Any]]:
        """游标之后的、不是她自己发的消息（新的在后，尾部截断到上限）。

        ``floor``：额外的下限（群补投传窗口里最新那条的时刻）—— 窗口是持久记录，
        它已经有的消息绝不再补一遍，双保险。
        """
        after = max(float(self._last_seen.get(cursor_key, 0.0)), float(floor or 0.0))
        out = []
        for m in msgs:
            try:
                t = float(m.get("time") or 0)
            except (TypeError, ValueError):
                continue
            if t <= after:
                continue
            if str(m.get("user_id") or "") == str(self.self_id):
                continue                      # 她自己发的（含她的回复）不算「漏消息」
            out.append(m)
        out.sort(key=lambda m: float(m.get("time") or 0))
        return out[-self.offline_backfill_max:]

    def _window_last_ts(self, gid: str) -> float:
        """本群窗口里最新一条的时刻（窗口文件是持久的 → 天然的去重下限）。"""
        try:
            tail = self.group_window.tail(str(gid), limit=1)
            return float(tail[-1].get("ts") or 0.0) if tail else 0.0
        except Exception:  # noqa: BLE001
            return 0.0

    @staticmethod
    def _stamp(ts: Any) -> str:
        try:
            return _clock.hhmm(float(ts))
        except Exception:  # noqa: BLE001
            return "??"

    async def _history_text(self, m: Dict[str, Any], *, for_window: bool = False) -> str:
        """历史消息的正文 —— 走**接收侧那条现成链路**（图片/表情包落盘 + 语音转写）。

        2026-10-04 实测教训：第一版只做了语音转写，补回来的表情包是个光秃秃的 `[表情包]`
        （没有本地路径）→ 她看不见图。所以这里直接复用 `_text_with_media()` /
        `_group_text_with_media()`：图片落盘、表情包顺手入库、语音转写，全都有了。
        任何一步炸了都退回占位口径，绝不抛（补投不能拖垮接收）。
        """
        try:
            if for_window:
                return await self._group_text_with_media(m)
            return await self._text_with_media(m)
        except Exception:  # noqa: BLE001
            logger.exception("[onebot] 补投渲染历史消息失败（退回占位口径）")
            return _proto.extract_window_text(m) if for_window else _proto.extract_text(m)

    def _backfill_text(self, msgs: List[Dict[str, Any]], *, rendered: List[str],
                       total: int, chat: str) -> str:
        """她看到的那段正文：抬头把「你刚上线」说清楚（主人要的就是这句）。"""
        head = (f"[你刚上线 · source=qq-backfill]\n"
                f"{chat}你不在线这阵子共 {total} 条，只给你最近 {len(msgs)} 条"
                f"（更早的没补，别当没发生过）：")
        lines = []
        for m, body in zip(msgs, rendered):
            name = _proto.sender_name(m) or "?"
            uid = _proto.sender_id(m) or "?"
            lines.append(f"[{self._stamp(m.get('time'))}] {name}(QQ:{uid}): {body}")
        return head + "\n" + "\n".join(lines)

    async def _backfill_dm(self, uid: str) -> None:
        """私聊：漏掉的消息合成**一个**回合给她（带「你刚上线」抬头）。"""
        key = self._cursor_key(gid="", uid=uid)
        msgs = await self._history_messages("get_friend_msg_history",
                                            user_id=int(uid) if uid.isdigit() else uid, count=30)
        missed = self._missed(msgs, key)
        if not missed:
            self._backfill_skipped += 1
            return
        newest = float(missed[-1].get("time") or 0)
        rendered = [await self._history_text(m) for m in missed]
        text = self._backfill_text(missed, rendered=rendered, total=len(missed),
                                   chat="他给你发了消息：")
        ev = {"post_type": "message", "message_type": "private", "user_id": uid,
              "self_id": self.self_id, "message_id": missed[-1].get("message_id"),
              "sender": missed[-1].get("sender") or {"nickname": uid, "user_id": uid},
              "message": missed[-1].get("message") or [], "time": newest}
        await self._dispatch(text, ev, is_group=False, gid="", uid=str(uid), internal=True)
        self._last_seen[key] = newest
        self._backfill_injected += len(missed)
        logger.info("[onebot] 上线补投（私聊 %s）：%d 条", _mask(uid), len(missed))

    async def _backfill_group(self, gid: str) -> None:
        """群聊：漏掉的消息追进滚动窗口（复用 delta 注入）+ 过一遍唤醒判定。"""
        key = self._cursor_key(gid=gid, uid="")
        msgs = await self._history_messages("get_group_msg_history",
                                            group_id=int(gid) if gid.isdigit() else gid, count=30)
        missed = self._missed(msgs, key, floor=self._window_last_ts(gid))
        if not missed:
            self._backfill_skipped += 1
            return
        newest = float(missed[-1].get("time") or 0)
        # 说明行：她翻窗口时知道这是「刚上线补的」而不是新发生的
        self._collect_group({"message": [], "group_id": gid,
                             "sender": {"nickname": "系统", "user_id": "0"}},
                            gid=gid, uid="0",
                            text=f"（你刚上线：补上不在线期间漏掉的 {len(missed)} 条）")
        for m in missed:
            self._collect_group(m, gid=gid, uid=str(_proto.sender_id(m) or ""),
                                text=await self._history_text(m, for_window=True),
                                ts=float(m.get("time") or 0))
        self._last_seen[key] = newest
        self._backfill_injected += len(missed)
        logger.info("[onebot] 上线补投（群 %s）：%d 条已进窗口", _mask(gid), len(missed))
        # 漏掉的最后一条如果在群里能叫醒她（@/点名/过闸），就现在把她叫起来
        last = missed[-1]
        try:
            voice_texts = await self._voice_texts(last) if _proto.record_segments(last) else []
            score_text = None
            if _proto.record_segments(last):
                score_text = _proto.with_voice_texts(_proto.extract_text(last), voice_texts)
            trigger = self._group_wake_trigger(last, gid=gid, score_text=score_text)
        except Exception:  # noqa: BLE001
            logger.exception("[onebot] 上线补投：群唤醒判定失败（群 %s）", _mask(gid))
            trigger = ""
        if trigger:
            await self._dispatch("", last, is_group=True, gid=gid,
                                 uid=str(_proto.sender_id(last) or ""), trigger=trigger)

    # ── 入站：语音转写（record 段 → 文字）────────────────────────────────────

    async def _voice_texts(self, event: Dict[str, Any]) -> List[Optional[str]]:
        """把语音段转成文字，返回**与 record 段同序**的文本列表（失败处 None）。

        结果缓存在 event 上：群窗口那条路先跑过之后，agent 回合那条路不再重复
        取一遍音频、转一遍写（跟图片落盘同一条规矩）。
        """
        if not (self.media_download_enabled and self.voice_transcribe_enabled):
            return []
        segs = _proto.record_segments(event)
        if not segs:
            return []
        if "_voice_texts" in event:
            return event["_voice_texts"]
        out: List[Optional[str]] = []
        for data in segs[: max(1, self.media_max_per_msg)]:
            try:
                out.append(await self._transcribe_voice(data))
            except Exception:  # noqa: BLE001 —— 单条失败不影响别的段
                logger.exception("[onebot] 语音转写失败（保留 [语音] 占位）")
                self._voice_failed += 1
                out.append(None)
        event["_voice_texts"] = out
        return out

    async def _transcribe_voice(self, data: Dict[str, Any]) -> Optional[str]:
        """一个 record 段 → 文本。先拿 mp3 字节，再落盘，再喂本地 whisper。"""
        blob = await self._voice_blob(data)
        if not blob:
            self._voice_failed += 1
            return None
        path = self._save_blob(blob, {"file": f"voice.{self.voice_out_format}"},
                               ext=f".{self.voice_out_format}")
        if not path:
            self._voice_failed += 1
            return None
        text = await self._stt(path)
        if not text:
            self._voice_failed += 1
            logger.info("[onebot] 语音已落盘但没转出字：%s", path)
            return None
        self._voice_ok += 1
        logger.info("[onebot] 语音转写成功（%d 字节 → %d 字）：%s",
                    len(blob), len(text), path)
        return text

    async def _voice_blob(self, data: Dict[str, Any]) -> Optional[bytes]:
        """record 段 → mp3 字节。

        两条路，按可靠性排序：
          ① 协议端 ``get_record(out_format=mp3)`` —— QQ 那份 ``.amr`` 其实是 **silk**
             换皮，本机 ffmpeg 吃不掉（实测 "Invalid data found"），必须让协议端转；
             它同时回 ``base64``（我们直接用）和落好的本地路径（兜底）。
          ② 上一步给的路径直接读（容器前缀映射，见 ``_read_local_file``）。
        """
        fid = data.get("file") or data.get("path") or data.get("url")
        if isinstance(fid, str) and fid:
            r = await self._call_action(
                {"action": "get_record",
                 "params": {"file": fid, "out_format": self.voice_out_format}},
                timeout=self.stt_timeout + 20.0)
            if isinstance(r, dict):
                payload = r.get("data") if isinstance(r.get("data"), dict) else r
                b64 = payload.get("base64")
                if isinstance(b64, str) and b64:
                    import base64 as _b64
                    try:
                        blob = _b64.b64decode(b64, validate=False)
                    except Exception:  # noqa: BLE001
                        blob = b""
                    if blob and len(blob) <= self.media_max_bytes:
                        return blob
                    if len(blob) > self.media_max_bytes:
                        logger.warning("[onebot] 语音超过上限 %d 字节，跳过",
                                       self.media_max_bytes)
                        return None
                for key in ("file", "url"):
                    blob = _read_local_file(payload.get(key))
                    if blob and len(blob) <= self.media_max_bytes:
                        return blob
        blob = _read_local_file(data.get("path"))
        if blob and len(blob) <= self.media_max_bytes:
            return blob
        return None

    async def _stt(self, path: str) -> Optional[str]:
        """本地 whisper（OpenAI 兼容 /v1/audio/transcriptions）→ 文本。"""
        try:
            import aiohttp
        except Exception:  # noqa: BLE001
            return None
        form = aiohttp.FormData()
        try:
            form.add_field("file", open(path, "rb"),
                           filename=Path(path).name, content_type="audio/mpeg")
        except OSError:
            logger.warning("[onebot] 语音文件打不开：%s", path)
            return None
        form.add_field("model", self.stt_model)
        form.add_field("language", "zh")
        try:
            timeout = aiohttp.ClientTimeout(total=self.stt_timeout)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.post(self.stt_url, data=form) as r:
                    if r.status != 200:
                        logger.warning("[onebot] STT 返回 %s：%s", r.status,
                                       (await r.text())[:200])
                        return None
                    body = await r.json(content_type=None)
        except Exception:  # noqa: BLE001
            logger.warning("[onebot] STT 调用失败：%s", self.stt_url, exc_info=True)
            return None
        text = (body or {}).get("text") if isinstance(body, dict) else None
        return text.strip() if isinstance(text, str) and text.strip() else None

    async def _load_group_ids(self) -> None:
        """把协议端已知的群号灌进 group_ids，并清掉主动私聊名单里混进来的群号。

        为什么值得一个 API 调用：`group_ids` 原先只靠「收到群消息才 add」来学（`_register_inbound`），
        在那之前该群会被当成私聊（`_is_group_chat` 只看这个集合）——2026-10-04 就出了事：
        群「<GROUP_NAME>」被当成私聊记进了主动起头名单，她朝它发「主动私聊」→ 协议端 1200 失败 + 告警。
        """
        data = None
        for attempt in range(3):
            try:
                res = await self._call_action({"action": "get_group_list"}, timeout=20.0)
            except Exception as e:                       # 协议端没起好 → 退避再来
                logger.debug("[onebot] get_group_list 异常：%s", e)
                res = None
            data = (res or {}).get("data")
            if isinstance(data, list):
                break
            await asyncio.sleep(2.0 + 2.0 * attempt)
        if not isinstance(data, list):
            logger.warning("[onebot] 群号名单没拿到（get_group_list 无数据）—— 先继续靠收群消息学")
            return
        ids = {str(g.get("group_id")) for g in data
               if isinstance(g, dict) and g.get("group_id") is not None}
        fresh = ids - set(self.group_ids)
        self.group_ids |= ids
        logger.info("[onebot] 群号名单 %d 个（新学 %d 个：%s）", len(ids), len(fresh),
                    ", ".join(sorted(_mask(x) for x in list(fresh)[:5])) or "无")
        # ★ 主动起头只认**真好友**（主人 2026-10-03：主动起头不进群）。
        #   清两类垃圾：① 群号（改前它们会被当私聊）；② 不是好友的假 id ——
        #   2026-10-04 实测：单测的假会话 `123456` 混进了真状态文件，她 18:18 朝它开口，
        #   白烧一次模型回合 + 连发 1200「无法获取用户信息」失败 + 一条告警。
        friends = await self._friend_ids()
        bad_group = [c for c in list(self._proactive.keys()) if str(c) in self.group_ids]
        bad_other = ([c for c in list(self._proactive.keys())
                      if str(c) not in self.group_ids and str(c) not in friends] if friends else [])
        for c in bad_group:
            self._proactive.pop(c, None)
            logger.warning("[onebot] 主动私聊名单里的群号 %s 已清掉（主动起头不进群）", _mask(c))
        for c in bad_other:
            self._proactive.pop(c, None)
            logger.warning("[onebot] 主动私聊名单里的 %s 不是好友 → 已清掉（防假会话白烧回合）", _mask(c))
        if bad_group or bad_other:
            self._save_proactive()

    async def _friend_ids(self) -> Set[str]:
        """协议端好友名单。拿不到就返回**空集合** = 本次不据此清理任何东西（宁可不清，不可误清）。"""
        for attempt in range(3):
            try:
                res = await self._call_action({"action": "get_friend_list"}, timeout=20.0)
            except Exception as e:                       # 协议端没起好 → 退避再来
                logger.debug("[onebot] get_friend_list 异常：%s", e)
                res = None
            data = (res or {}).get("data")
            if isinstance(data, list):
                return {str(f.get("user_id")) for f in data
                        if isinstance(f, dict) and f.get("user_id") is not None}
            await asyncio.sleep(1.0 + attempt)
        logger.debug("[onebot] 好友名单没拿到 —— 本次不清理主动名单")
        return set()

    async def _call_action(self, payload: Dict[str, Any], timeout: float = 30.0) -> Optional[Dict[str, Any]]:
        """发一条 action 帧并等 echo 回应。反向 WS 上 API 调用与事件共用同一条连接。"""
        ws = self._ws
        if ws is None or ws.closed:
            return None
        self._echo_seq += 1
        echo = f"hermes-onebot-{self._echo_seq}"
        payload = {**payload, "echo": echo}
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[echo] = fut
        try:
            async with self._ws_lock:
                await ws.send_str(json.dumps(payload, ensure_ascii=False))
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(echo, None)

    def _resolve_echo(self, payload: Dict[str, Any]) -> None:
        echo = payload.get("echo")
        fut = self._pending.get(str(echo))
        if fut is not None and not fut.done():
            fut.set_result(payload)


# ── 小工具 ─────────────────────────────────────────────────────────────────

def _build_alert_request(base_url: str, token: str, *, magicpush: bool, status: str,
                         title: str, text: str, now: float):
    """算出「这条告警往哪发、发什么」——纯函数，便于单测。

    · ``magicpush=True``（主人 2026-10-04 起的新通道）：``POST <base>/api/push/<token>``，
      体是 magicpush 的 ``{title, content, type}``；token 走 **URL 路径**，没有自定义 header。
    · ``magicpush=False``（老通道，:8098 AstrBot hermes_report 那套，已退役但保留兼容）：
      ``POST <base>``，体 ``{task_id, status, title, summary, source}``，token 走 ``X-Report-Token`` 头。
    """
    if magicpush:
        # 配置里的 alert_report_url 写**到 /api/push 为止**，这里只补 token
        url = "%s/%s" % (base_url.rstrip("/"), _urlquote(token, safe=""))
        return url, {}, {"title": title, "content": text[:1200], "type": "markdown"}
    payload = {
        "task_id": "onebot-selfcheck-%d" % int(now),
        "status": status,
        "title": title,
        "summary": text[:400],
        "source": "hermes-onebot-adapter",
    }
    return base_url, {"X-Report-Token": token}, payload


#: Hermes 在人回合上把「她只回了静默标记」换成的那段提示文案里的特征串
#: （源头：`gateway/run_turn.py` 的 `_UNEXPECTED_SILENCE_REPLY`）。
#: 主人 2026-10-04：「把她提示都关了，一步到位」→ 这段不再发出去，只记账 + 记日志。
_SILENCE_WARNING_HINT = "The model returned only a silence marker"


def _is_silence_warning(text: str) -> bool:
    """这段正文是不是 Hermes 的「静默提示」文案（用特征串判，别依赖 emoji 前缀）。"""
    return _SILENCE_WARNING_HINT in str(text or "")


def _truthy(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _int_or(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _float_or(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _as_int_safe(value, default: int, *, minimum: int = 0) -> int:
    """``_int_or`` + 下限钳制（群管理时长用：负数/0/写坏的值一律抬到 minimum）。"""
    n = _int_or(value, default)
    return n if n >= minimum else int(minimum)


def _hermes_home() -> Path:
    """当前 profile 的 HERMES_HOME（聊天门 = /opt/data/profiles/chat）。"""
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        import os
        return Path(os.environ.get("HERMES_HOME") or "/opt/data")


#: 协议端自报本地路径 → 本机可见路径的前缀映射（尽力而为，命中不了就放弃）。
#: 依据：NapCat 的 ``.config/QQ`` 落在宿主 ``/opt/data/stack/napcat/ntqq``（挂载），
#: 而它 ``get_image`` / ``fetch_custom_face_detail`` 自报的路径形如
#: ``/app/.config/QQ/nt_qq_<hash>/nt_data/...``。
#: ⚠️ 这条映射是**推断**（未在真实 get_image 响应上验证过），只当兜底，不依赖它。
_PREFIX_MAP: Tuple[Tuple[str, str], ...] = (
    ("/app/.config/QQ", "/opt/data/stack/napcat/ntqq"),
)


def _read_local_file(p: Any) -> Optional[bytes]:
    """把协议端自报的本地路径读成 bytes（含 ``file://`` 与容器前缀映射）。失败返回 None。"""
    if not isinstance(p, str) or not p:
        return None
    cands = [p]
    if p.startswith("file://"):
        cands.append(p[7:])
    for src, dst in _PREFIX_MAP:
        for c in list(cands):
            if c.startswith(src):
                cands.append(dst + c[len(src):])
    for c in cands:
        try:
            path = Path(c)
            if path.is_file():
                return path.read_bytes()
        except Exception:  # noqa: BLE001
            continue
    return None


def _guess_ext(blob: bytes, data: Dict[str, Any]) -> str:
    """按 magic 猜扩展名；猜不出再看原路径/URL 后缀；再不行 ``.bin``。"""
    if blob.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if blob.startswith(b"GIF8"):
        return ".gif"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return ".webp"
    if blob.startswith(b"BM"):
        return ".bmp"
    hint = str(data.get("file") or data.get("url") or "")
    m = re.search(r"\.(jpg|jpeg|png|gif|webp|bmp|heic)\b", hint, re.I)
    return "." + m.group(1).lower() if m else ".bin"


def _mid_of(resp: Dict[str, Any]) -> Optional[str]:
    data = resp.get("data")
    if isinstance(data, dict) and data.get("message_id") is not None:
        return str(data["message_id"])
    return None


def _mask(chat_id: str) -> str:
    """日志里 QQ 号脱敏（保头 3 尾 2）。"""
    s = str(chat_id)
    if len(s) <= 6:
        return s[:1] + "*" * max(0, len(s) - 1)
    return f"{s[:3]}***{s[-2:]}"


def _redact_addr(addr: Any) -> str:
    return str(addr or "?")


def _brief(text: str, limit: int = 40) -> str:
    """日志里给一句摘要（只用于证明「这条真的收进来了」），长了就截断。"""
    s = (text or "").replace("\n", " ").strip()
    return s if len(s) <= limit else s[:limit] + "…"


# ── 插件注册 ───────────────────────────────────────────────────────────────

def check_requirements() -> bool:
    """被动探针：依赖可导入即可，**绝不安装**（状态展示会随意调用它）。"""
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        return False
    return True


def validate_config(config) -> bool:
    """能不能连：至少要有个监听端口（token 强烈建议但不强制，本机回环可无）。"""
    extra = getattr(config, "extra", {}) or {}
    try:
        int(extra.get("ws_port", 6700))
    except (TypeError, ValueError):
        return False
    return True


def is_connected(config) -> bool:
    """已配置 = 有端口（+ 有 token 更佳）。不要求 token，避免回环测试场景卡住。"""
    return validate_config(config)


def _env_enablement() -> Optional[Dict[str, Any]]:
    """env-only 配置也要能出现在 `hermes gateway status` 里。"""
    extra: Dict[str, Any] = {}
    for env, key in (("ONEBOT_WS_HOST", "ws_host"), ("ONEBOT_WS_PORT", "ws_port"),
                     ("ONEBOT_SELF_ID", "self_id")):
        v = get_scoped_secret(env)
        if v:
            extra[key] = v
    token = \"<SECRET>\"("ONEBOT_ACCESS_TOKEN")
    if token:
        extra["access_token"] = token
    return extra or None


def register(ctx) -> None:
    """插件入口：`ctx.register_platform()`，**零改动 Hermes 核心**。"""
    ctx.register_platform(
        name="onebot",
        label="OneBot v11 (QQ)",
        adapter_factory=OneBotAdapter,
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["ONEBOT_ACCESS_TOKEN"],
        install_hint="No extra packages needed (aiohttp ships with Hermes)",
        env_enablement_fn=_env_enablement,
        allowed_users_env="ONEBOT_ALLOWED_USERS",
        allow_all_env="ONEBOT_ALLOW_ALL_USERS",
        max_message_length=4500,
        emoji="🐧",
        pii_safe=False,
        platform_hint=(
            "You are on QQ via an OneBot v11 bridge. Messages are plain text — no markdown "
            "rendering. To split a reply into several separate chat bubbles, end each intended "
            "bubble with the separator `⁂` (it is stripped before sending and never visible). "
            "Keep each bubble short and conversational; avoid newlines as separators."
        ),
    )

