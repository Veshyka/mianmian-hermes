"""OneBot v11 协议解析（纯函数，可离线单测）。

参照 AstrBot `/AstrBot/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py`
的事件→内部对象映射写法（读，不搬代码）：
  * post_type 分流：`:131-141`（message / notice / request）
  * message_type 分流：`:215-226`（group / private）
  * session_id：`:222-226`  群→group_id，私聊→sender.user_id   ← 会话稳定性的关键
  * 文本段拼接：`:243-252`  itertools.groupby(type) 后 join
  * 屏蔽 QQ 管家：`:133-135`  （sender.user_id == 2854196310）
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

_log = logging.getLogger(__name__)

#: QQ 原生小黄脸的编号白名单（`face_ids.py`，从 NapCat 自带的 `qq_emoji_list.QQ_FACE` 生成）。
#: 读不到就当没有（**不校验**）——绝不因为表缺失而挡住正常发送。
try:
    from .face_ids import FACE_NAMES, NATIVE_FACE_IDS
except Exception:  # pragma: no cover - 只在包结构异常时走到
    try:
        from face_ids import FACE_NAMES, NATIVE_FACE_IDS  # type: ignore
    except Exception:
        NATIVE_FACE_IDS = frozenset()  # type: ignore
        FACE_NAMES = {}  # type: ignore

# QQ 管家 —— AstrBot 明确屏蔽（aiocqhttp_platform_adapter.py:133）。照抄这个已知噪声源。
QQ_GUANJIA_ID = "2854196310"

_CQ_CODE_RE = re.compile(r"\[CQ:[^\]]*\]")


#: 各消息段在「给人看的正文」里怎么呈现 —— **唯一真相表**。
#: 提取链和窗口链共用它。以前两条链各写一份 if/elif，结果一边加了 `image` 另一边
#: 忘了加 `marketface`，超级表情就被**静默丢掉**了（既不入窗口也不进正文，还不报错）。
MEDIA_PLACEHOLDER: Dict[str, str] = {
    "image": "[图片]",
    "record": "[语音]",
    "video": "[视频]",
    "face": "[表情]",
    "mface": "[表情]",          # NapCat 上报的小表情（带 emoji_id）
    "marketface": "[超级表情]",  # ← QQ「超级表情/大表情」，原来漏的就是它
    "bface": "[表情]",
    "dice": "[骰子]",
    "rps": "[猜拳]",
    "shake": "[窗口抖动]",
    "poke": "[戳一戳]",
    "music": "[音乐]",
    "json": "[卡片]",
    "xml": "[卡片]",
    "markdown": "[富文本]",
    "forward": "[合并转发]",
    "node": "[合并转发]",
}


#: 表情类段的 id 候选字段名 —— 各段类型/各协议端命名不一致，按顺序取第一个非空。
#:   face       → ``id``（OneBot v11 标准，QQ 内置表情编号）
#:   mface      → ``emoji_package_id`` + ``emoji_id``（NapCat 上报的商城小表情）
#:   marketface → ``id`` / ``face_id``（超级表情，协议端间不稳定，兜底 summary）
_FACE_ID_KEYS: Tuple[str, ...] = ("id", "emoji_id", "face_id")

#: 走「带 id 渲染」的表情段类型
_FACE_TYPES: Tuple[str, ...] = ("face", "bface", "mface", "marketface")


def _big_face_name(data: Dict[str, Any]) -> str:
    """大表情（QQ 内置超表情）的名字：从 ``data.raw.faceText`` 取，取不到给空串。

    判据（一手：2026-10-03 抓的真实入站报文）：大表情的 ``face`` 段带 ``raw``，
    里面有 ``faceType=3 / packId / stickerId / faceText="/秋秋赏月"``；原生小黄脸没有 raw。
    ``faceText`` 形如 ``/秋秋赏月``，去掉前导斜杠和方括号就是名字。
    """
    raw = data.get("raw")
    if not isinstance(raw, dict):
        return ""
    txt = str(raw.get("faceText") or "").strip().lstrip("/").strip("[]")
    if not txt:
        return ""
    # 名字里可能有空格/换行，压成单行免得把占位符撑破
    return " ".join(txt.split())[:24]


def _face_label(stype: str, data: Dict[str, Any]) -> str:
    """表情段 → **带 id** 的占位文本（如 ``[表情:123]``）。

    为什么必须带 id：以前只渲染 ``[表情]``，模型分不清主人发的是「微笑」还是「流泪」，
    等于把表情当噪声。QQ 的 face 段 data 里本来就带编号（``{"id": "123"}``），
    白白丢掉没有道理。id 缺失时才回退成无 id 占位（**绝不返回空串**）。
    """
    if stype == "mface":
        pkg, eid = data.get("emoji_package_id"), data.get("emoji_id")
        if pkg not in (None, "") and eid not in (None, ""):
            return f"[表情包:{pkg}/{eid}]"
        if eid not in (None, ""):
            return f"[表情包:{eid}]"
    if stype == "marketface":
        mid = next((data[k] for k in _FACE_ID_KEYS if data.get(k) not in (None, "")), None)
        if mid is not None:
            return f"[超级表情:{mid}]"
        summary = data.get("summary") or data.get("face_name")
        if summary:
            return f"[超级表情:{str(summary).strip('[]')}]"
        return "[超级表情]"
    fid = next((data[k] for k in _FACE_ID_KEYS if data.get(k) not in (None, "")), None)
    if fid is not None:
        # ★ 2026-10-03 新增：**大表情**（QQ 内置的超表情，如 faceIndex=500「秋秋赏月」）
        # 入站是 ``face`` + ``data.raw``（faceType/packId/stickerId/faceText…），
        # 原生小黄脸（faceType=1）没有 raw。大表情**发不回去**（见
        # reports/onebot-marketface-roundtrip-2026-10-03.md），所以至少把**名字**带上，
        # 她才能：① 说得出是哪张 ② 区分两张不同的大表情。
        # 兼容：原生小黄脸渲染**一字不变**，仍是 ``[表情:14]``。
        name = _big_face_name(data)
        if name:
            return f"[表情:{fid} {name}]"
        return f"[表情:{fid}]"
    return MEDIA_PLACEHOLDER.get(stype, f"[未支持的消息段:{stype}]")


# ── 表情包 vs 照片：判据（2026-10-03，依据见 chat-layer/PLAN-v6-表情包.md §0.1）────
#
# NapCat 把「收藏/自定义表情」（QQ 里那种图片式表情包，含动图）上报成 ``type=image``，
# 但 image 段的 data 里带 ``sub_type``（= QQ NT 的 picSubType）：
#     0=KNORMAL(普通照片) 1=KCUSTOM(收藏/自定义表情 ← 主人说的那种) 2=KHOT(热图)
#     3=KDIPPERCHART 4=KSMART 5=KSPACE 6=KUNKNOW 7=KRELATED
# 「商城/超级表情」也上报成 image，但带 ``emoji_id``/``emoji_package_id``/``key``，
# ``file`` 形如 ``xx-<emojiId>.gif``，且**不带 sub_type**。
# 这些字段不需要协议端开任何配置（napcat-onebot/api/msg.ts 的转换器无条件写入）。

#: 判为表情包的 sub_type 取值（1 收藏表情 / 2 热图 / 7 相关表情）
STICKER_SUBTYPES: Tuple[int, ...] = (1, 2, 7)

#: summary 里出现这些词也算表情包（协议端/版本差异时的补充判据）
_STICKER_SUMMARY_HINTS: Tuple[str, ...] = ("表情", "动画", "热图", "贴纸", "emoji")


def _as_int(v: Any) -> Optional[int]:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def is_sticker_image(data: Dict[str, Any]) -> bool:
    """image 段的 data → 是不是**表情包**（而不是照片）。

    命中任一即算：``sub_type ∈ (1,2,7)`` / 带 ``emoji_id``（商城超级表情）/ ``summary``
    含「表情·动画·热图·贴纸」。``sub_type=0`` 是明确的普通照片，直接 False。
    **判不出就按照片处理**（保守：照片只描述，不往表情库里塞）。
    """
    if not isinstance(data, dict):
        return False
    st = _as_int(data.get("sub_type"))
    if st == 0:
        return False
    if st in STICKER_SUBTYPES:
        return True
    for k in ("emoji_id", "emoji_package_id", "key"):
        if data.get(k) not in (None, "", 0, "0"):
            return True
    summary = str(data.get("summary") or "")
    return any(h in summary for h in _STICKER_SUMMARY_HINTS)


def sticker_label(data: Dict[str, Any]) -> str:
    """表情包正文里附带的一句人话标签（summary 优先）。方括号一律剥掉。"""
    summary = str(data.get("summary") or "").strip().strip("[]")
    if summary:
        return summary
    eid = data.get("emoji_id")
    if eid not in (None, ""):
        return f"超级表情{eid}"
    return "表情包"


def _media_placeholder(stype: str, data: Dict[str, Any]) -> str:
    """媒体/未知段 → 占位文本。**绝不返回空串** —— 空串会让整条消息被当成"没内容"丢掉。"""
    if stype == "image":
        # 表情包与照片分开标注：她看到 [表情包] 就不该去描述画面，只读情绪
        return "[表情包]" if is_sticker_image(data) else MEDIA_PLACEHOLDER["image"]
    if stype == "file":
        name = data.get("file") or data.get("file_name") or data.get("name") or ""
        return f"[文件:{name}]" if name else "[文件]"
    if stype in _FACE_TYPES:
        return _face_label(stype, data)
    if stype in MEDIA_PLACEHOLDER:
        return MEDIA_PLACEHOLDER[stype]
    # 兜底（这次的病根）：不认识也要留痕，别让它无声无息地消失
    return f"[未支持的消息段:{stype}]"


def _render(event: Dict[str, Any]) -> str:
    """把一整条消息渲染成正文文本（唯一实现，两个公开函数都走它）。"""
    msg = event.get("message")
    if isinstance(msg, list):
        parts: List[str] = []
        for seg in msg:
            if not isinstance(seg, dict):
                continue
            stype = str(seg.get("type") or "")
            data = seg.get("data") or {}
            if stype == "text":
                txt = data.get("text") or ""
                if txt.strip():
                    parts.append(txt)
            elif stype == "at":
                qq = str(data.get("qq") or "")
                parts.append("[@全体成员]" if qq == "all" else f"[@{qq}]")
            elif stype == "reply":
                continue  # 引用在单独的通道处理（reply_message_id）
            else:
                # 媒体段 + **任何**没见过的段，一律落占位，永不静默丢
                parts.append(_media_placeholder(stype, data))
        return "".join(parts).strip()
    if isinstance(msg, str):
        return _CQ_CODE_RE.sub("", msg).strip()
    return ""


def extract_text(event: Dict[str, Any]) -> str:
    """从 OneBot v11 消息事件里取正文文本（投给 LLM 的那一份）。

    at 段渲染成 ``[@qq]`` 交给模型自己判断；媒体段（图片/语音/表情/**超级表情**）
    渲染成占位符；不认识的段也留 ``[未支持的消息段:x]``。

    **2026-09-23 行为变更**：以前 `face`/`mface` 是 `continue` 直接扔掉，导致
    「主人私聊只发一个超级表情」→ 正文为空 → `should_ignore` 判 `no_text` → 整条消息
    消失，她连"有个表情"都不知道。现在表情会渲染成占位符，纯表情消息也能到她面前。
    """
    return _render(event)


def is_private(event: Dict[str, Any]) -> bool:
    return str(event.get("message_type") or "") == "private"


def extract_window_text(event: Dict[str, Any]) -> str:
    """群消息**滚动窗口**用的文本。

    历史上有两处差异（`face`/`mface` 在这边渲染成 ``[表情]``、在 `extract_text` 那边丢弃）。
    现在两条链**已经收敛成同一个 `_render`** —— 差异消失，改名只为保留调用点的可读性，
    不再有「改了一边忘了另一边」的空间。
    """
    return _render(event)


def is_group(event: Dict[str, Any]) -> bool:
    return str(event.get("message_type") or "") == "group"


def sender_id(event: Dict[str, Any]) -> str:
    sender = event.get("sender") or {}
    uid = sender.get("user_id", event.get("user_id"))
    return str(uid) if uid is not None else ""


def sender_name(event: Dict[str, Any]) -> str:
    sender = event.get("sender") or {}
    return str(sender.get("card") or sender.get("nickname") or sender_id(event) or "N/A")


def group_id(event: Dict[str, Any]) -> str:
    gid = event.get("group_id")
    return str(gid) if gid is not None else ""


def reply_message_id(event: Dict[str, Any]) -> Optional[str]:
    """引用回复：``reply`` 段的 id（用来把被引用文本带进上下文）。"""
    msg = event.get("message")
    if isinstance(msg, list):
        for seg in msg:
            if isinstance(seg, dict) and seg.get("type") == "reply":
                rid = (seg.get("data") or {}).get("id")
                if rid is not None:
                    return str(rid)
    return None


def classify_event(event: Dict[str, Any]) -> str:
    """事件大类 —— 给适配器做**路由**用（决定走消息链路还是群管理链路）。

    返回值：
      * ``"message"`` 普通消息（私聊/群聊）
      * ``"notice"``  通知（入群/退群、撤回、禁言、戳一戳…）—— 群管理要用
      * ``"request"`` 请求（加好友、加群）—— 入群审批要用
      * ``"meta"``    心跳/生命周期
      * ``"other"``   其它

    以前 `should_ignore` 第一句就把 `post_type != "message"` 整条丢掉，导致
    **入群通知、撤回、禁言、加群申请全都进不来**，入群欢迎/防撤回/入群审批根本没有原料。
    现在先分类再决定丢弃，而不是一律丢。
    """
    pt = str(event.get("post_type") or "")
    if pt == "message":
        return "message"
    if pt == "notice":
        return "notice"
    if pt == "request":
        return "request"
    if pt in ("meta_event", "meta"):
        return "meta"
    return "other"


def notice_type(event: Dict[str, Any]) -> str:
    """通知子类型（如 ``group_increase`` / ``group_recall`` / ``group_ban``）。"""
    return str(event.get("notice_type") or "")


def request_type(event: Dict[str, Any]) -> str:
    """请求子类型（如 ``group`` 加群 / ``friend`` 加好友）。"""
    return str(event.get("request_type") or "")


def should_ignore(event: Dict[str, Any], *, media_counts_as_text: bool = False) -> Optional[str]:
    """返回忽略原因（字符串）或 None（表示应当处理）。

    只负责**消息**链路；`notice`/`request` 由适配器先经 `classify_event` 分流到群管理链路，
    不走这里（否则入群/撤回/审批会被"不是消息事件"一句话丢掉）。

    ``media_counts_as_text``：群窗口采集（B 阶段）用 —— 纯媒体消息**也要收进窗口**
    （不然「群里只发图不说话」这段完全没有痕迹）。
    """
    if classify_event(event) != "message":
        return "not_a_message_event"
    if str(event.get("message_type") or "") not in ("private", "group"):
        return "unsupported_message_type"
    if sender_id(event) == QQ_GUANJIA_ID:
        return "qq_guanjia"
    if not extract_text(event):
        if not (media_counts_as_text and extract_window_text(event)):
            return "no_text"
    return None


def build_action(action: str, chat_id: str, text: str, *, is_group: bool,
                 self_id: str = "", echo: str = "",
                 reply_to: Optional[str] = None) -> Dict[str, Any]:
    """构造一条 OneBot v11 action 帧（反向 WS 上原样回传）。

    参数名照 OneBot v11 标准：群 ``group_id`` / 私聊 ``user_id``（AstrBot 同款，见
    aiocqhttp_message_event.py:107-124）。

    ``reply_to``：非空则**在正文前插一个 `reply` 段** —— 这是引用回复的出站实现。
    （入站一直能解析引用，出站以前是断的：`send(reply_to=)` 那个形参定义了但从没被用过。）
    """
    message: List[Dict[str, Any]] = []
    if reply_to:
        message.append({"type": "reply", "data": {"id": str(reply_to)}})
    # 正文里出现的 `[CQ:xxx,k=v]`（如 `[CQ:image,file=/abs/path,sub_type=1]`）
    # 解析成**真正的消息段** —— 这是她「用正文发图/发表情包」的唯一通道。
    # 没有 CQ 码时 cq_to_segments 返回的就是一个等价 text 段，行为与以前逐字一致。
    segs = cq_to_segments(text)
    if segs:
        message.extend(segs)
    else:
        message.append({"type": "text", "data": {"text": text}})
    params: Dict[str, Any] = {"message": message}
    if is_group:
        params["group_id"] = _num(chat_id)
    else:
        params["user_id"] = _num(chat_id)
    if self_id:
        params["self_id"] = _num(self_id)
    frame: Dict[str, Any] = {"action": action, "params": params}
    if echo:
        frame["echo"] = echo
    return frame


def _num(value: Any) -> Any:
    """OneBot 的数字型 id 用 int；非数字保持字符串（AstrBot 也这么兜，:99-102）。"""
    s = str(value)
    return int(s) if s.lstrip("-").isdigit() else s


# ── 入站媒体：抽段 + 回填本地路径 ─────────────────────────────────────────

#: CQ 码形式的图片（``[CQ:image,file=…,url=…]``）—— 字符串格式消息用
_CQ_IMAGE_RE = re.compile(r"\[CQ:image((?:,[^\[\]]*)?)\]")


def image_segments(event: Dict[str, Any]) -> List[Dict[str, Any]]:
    """按顺序取出事件里所有 ``image`` 段的 data（供适配器落盘用）。

    **纯函数**：只读 event，不做任何 IO。适配器拿它去 ``get_image`` / 下载，
    再按同样的顺序把正文里的 ``[图片]`` 换成 ``[图片:<本地路径>]``
    （见 ``with_media_paths``）。

    字符串格式消息（``message`` 是 str）也认：从 CQ 码里把 ``file``/``url`` 抠出来，
    免得「协议端配成 string 就完全拿不到图」。
    """
    out: List[Dict[str, Any]] = []
    msg = event.get("message")
    if isinstance(msg, list):
        for seg in msg:
            if isinstance(seg, dict) and str(seg.get("type") or "") == "image":
                data = seg.get("data")
                out.append(data if isinstance(data, dict) else {})
    elif isinstance(msg, str):
        for m in _CQ_IMAGE_RE.finditer(msg):
            data: Dict[str, Any] = {}
            for pair in m.group(1).lstrip(",").split(","):
                key, _, val = pair.partition("=")
                if key.strip():
                    data[key.strip()] = val.strip()
            out.append(data)
    return out


#: 正文里与 image 段一一对应的两种占位（顺序 = image 段顺序）
_MEDIA_PLACEHOLDER_RE = re.compile(r"\[(图片|表情包)\]")


def with_media_paths(text: str, paths: List[Optional[str]],
                     placeholder: Optional[str] = None) -> str:
    """把正文里的 ``[图片]`` / ``[表情包]`` 按顺序回填成本地绝对路径。

    ``paths`` 与 ``image_segments()`` 同序（一个 image 段一个占位）；某项是 ``None``
    （没落盘/超上限/下载失败）时**保留原占位** —— 绝不删占位，少一个会让模型以为
    那条消息里根本没有图。

    ``placeholder`` 传了就退回旧的「只认一种占位」行为（兼容既有调用）。
    """
    if not text or not paths:
        return text
    if placeholder is None:
        idx = 0

        def _sub(m: "re.Match[str]") -> str:
            nonlocal idx
            kind = m.group(1)                      # 图片 / 表情包
            p = paths[idx] if idx < len(paths) else None
            idx += 1
            return f"[{kind}:{p}]" if p else f"[{kind}]"

        return _MEDIA_PLACEHOLDER_RE.sub(_sub, text)
    parts = text.split(placeholder)
    if len(parts) < 2:                     # 正文里没有图片占位，不动
        return text
    # split 会把 `]` 一起吃掉 → 回填时要自己补回右括号（`[图片:` + 路径 + `]`）
    head = placeholder[:-1] if placeholder.endswith("]") else placeholder
    out: List[str] = [parts[0]]
    for i, tail in enumerate(parts[1:]):
        p = paths[i] if i < len(paths) else None
        out.append(f"{head}:{p}]" if p else placeholder)
        out.append(tail)
    return "".join(out)


# ── 出站「贴表情回应」的指令语法（她在正文里写，适配器剥出来执行）──────────────
#
# 她的正文里没有「工具调用」这一说，所以用**文本标记**触发：
#     [贴表情:赞]   /  [贴表情:76]
# 适配器在发送前把标记从正文里剥掉（主人永远看不到它），等这条消息真发出去之后，
# 再对「她正在回复的那条消息」调 `set_msg_emoji_like`（贴表情不是消息，不走 send 的分段）。
#
# id 表来源（两源交叉核对，2026-10-03）：
#   ① MaiBot-Napcat-Adapter 的 `qq_emoji_list.QQ_FACE`（id → 名称）
#   ② AstrBot `astrbot_plugin_admin_emoji_reply` 的中文名 → id 映射表
# **只有两源一致的名字才收进来**（不一致的如「抱抱=222」「强=78」已剔除，
# 78 在①里是「握手」）。要加新表情先跑同样的核对，别凭印象写数字。
EMOJI_LIKE_IDS: Dict[str, int] = {
    "赞": 76, "点赞": 201, "爱心": 66, "比心": 319, "微笑": 14, "呲牙": 13,
    "笑哭": 182, "捂脸": 264, "吃瓜": 271, "大哭": 9, "流泪": 5, "得意": 4,
    "调皮": 12, "可爱": 21, "酷": 16, "害羞": 6, "偷笑": 20, "白眼": 22,
    "鼓掌": 99, "生气": 326, "doge": 179, "问号脸": 268, "摸鱼": 285,
    "牛啊": 299, "喵喵": 307,
}

#: 正文里的贴表情标记（``[贴表情:赞]`` / ``[贴表情：76]``）
EMOJI_LIKE_RE = re.compile(r"\[贴表情[:：]\s*([^\[\]]{1,12})\s*\]")


def extract_emoji_like_markers(text: str) -> Tuple[str, List[int], List[str]]:
    """把正文里的 ``[贴表情:x]`` 剥出来 → ``(干净正文, [emoji_id...], [认不出的词...])``。

    * 名字在 ``EMOJI_LIKE_IDS`` 里 → 换成 id；纯数字 → 直接用。
    * 认不出的**剥掉但记进第三个返回值**（调用方打日志）—— 不让标记漏给主人，
      也不静默吞掉她的意图。
    """
    ids: List[int] = []
    unknown: List[str] = []
    if not text:
        return text, ids, unknown

    def _sub(m: "re.Match[str]") -> str:
        raw = m.group(1).strip()
        if raw.isdigit():
            ids.append(int(raw))
            return ""
        eid = EMOJI_LIKE_IDS.get(raw)
        if eid is not None:
            ids.append(int(eid))
        else:
            unknown.append(raw)
        return ""

    return EMOJI_LIKE_RE.sub(_sub, text), ids, unknown


def build_emoji_like_action(message_id: Any, emoji_id: Any, *, set_: bool = True,
                            self_id: str = "", echo: str = "") -> Dict[str, Any]:
    """构造「贴表情回应」的 action 帧 —— NapCat 扩展 ``set_msg_emoji_like``。

    参数名照 NapCat 源码
    （``packages/napcat-onebot/action/msg/SetMsgEmojiLike.ts``）：
    ``message_id`` + ``emoji_id``（必填）+ ``set``（可选，``false`` 即取消回应）。

    ⚠️ **本机 NapCat 4.18.28 实测**：``message_id`` / ``emoji_id`` 传**数字或字符串
    都被接受**（8 种组合全过 schema）。这里统一走 ``_num`` 归一成数字，只是为了和
    本文件其它 action 的 id 处理保持一致；非数字 id（如 mface 那种）原样保留。

    ⚠️ ``message_id`` 用**短 ID**（OneBot 事件里的 ``message_id``），不是 ``real_id``。

    ⚠️ 真正「贴成功」需要**真实存在的 message_id**；对一个不存在的 id，NapCat 回
    ``{"retcode":200,"message":"msg not found"}``（说明 action 与参数都已通过，只差目标）。
    """
    params: Dict[str, Any] = {
        "message_id": _num(message_id),
        "emoji_id": _num(emoji_id),
        "set": bool(set_),
    }
    if self_id:
        params["self_id"] = _num(self_id)
    frame: Dict[str, Any] = {"action": "set_msg_emoji_like", "params": params}
    if echo:
        frame["echo"] = echo
    return frame


#: `[CQ:type,k=v,...]` —— CQ 码的通用形状。
_CQ_SEG_RE = re.compile(r"\[CQ:([A-Za-z_]+)((?:,[^\[\]]*)?)\]")

#: CQ 码里哪些 key 要转成数字 —— **按段类型决定**。
#:
#: 依据（一手）：NapCat 的 OB11 消息段 schema，`packages/napcat-onebot/types/message.ts`
#:   image.data.sub_type          → `Type.Number`   ← 唯一必须数字的段
#:   mface.data.emoji_package_id  → `Type.Number`（emoji_id / key 是 String）
#:   face.data.id                 → **`Type.String`**
#:   at.data.qq                   → **`Type.String`**（qq 或 "all"）
#:   reply.data.id                → `Type.String`（build_action 里本来就走 str()）
#:
#: ⚠️ 2026-10-03 踩过的坑：这里以前是**全局**集合 `{"qq","id",...}`，于是
#: `[CQ:face,id=500]` 被转成 `{"id":500}`（数字）、`[CQ:at,qq=…]` 被转成数字 qq ——
#: 两者都不合 schema，NapCat 直接回 `retcode=1200 消息体无法解析`，**整条消息发不出去**
#: （2026-10-03 06:51 聊天门实测，见 logs/gateway.log:5663）。所以改成按类型给。
#: 未知段类型一律**保持字符串**（保守：宁可让协议端自己报错，也别再猜错类型）。
_CQ_NUM_KEYS_BY_TYPE: Dict[str, Tuple[str, ...]] = {
    "image": ("sub_type",),
    "mface": ("emoji_package_id",),
}

#: 保留旧名字供排障/引用，但**它不再是全局规则**（真正的判定看上面那张表）。
_CQ_NUM_KEYS = {"sub_type", "emoji_package_id"}

#: 没发出去、被丢掉的非原生表情（只用于日志/自检，不参与业务）。
_DROPPED_FACES: Dict[str, int] = {}


def cq_to_segments(text: str) -> List[Dict[str, Any]]:
    """把文本里的 ``[CQ:xxx,k=v]`` 解析成**真正的消息段**，其余部分原样留在 text 段里。

    为什么需要它：群管理（欢迎语、防撤回复述）会用 ``{at}`` 生成 ``[CQ:at,qq=123]``。
    如果原样塞进数组格式的 text 段，就成了一段**字面字符**——NapCat 官方文档写的是
    「支持使用 CQ 码发送」，但那是针对**字符串格式**的说法；数组格式里文本段内的 CQ 码
    各协议端表现并不一致。要 @ 人就该给出真的 ``at`` 段，语义无歧义，不用赌。

    认不出的 CQ 码**原样保留**成文本（不静默吞）。
    """
    if not text:
        return []
    segs: List[Dict[str, Any]] = []
    dropped: List[str] = []
    pos = 0
    for m in _CQ_SEG_RE.finditer(text):
        if m.start() > pos:
            segs.append({"type": "text", "data": {"text": text[pos:m.start()]}})
        ctype = m.group(1)
        data: Dict[str, Any] = {}
        for pair in m.group(2).lstrip(",").split(","):
            if not pair:
                continue
            k, _, v = pair.partition("=")
            k = k.strip()
            if not k:
                continue
            v = v.strip()
            num_keys = _CQ_NUM_KEYS_BY_TYPE.get(ctype, ())
            data[k] = _num(v) if k in num_keys and v.lstrip("-").isdigit() else v
        if ctype == "face":
            fid = str(data.get("id", "")).strip()
            if fid.isdigit() and NATIVE_FACE_IDS and int(fid) not in NATIVE_FACE_IDS:
                # 不是原生小黄脸（例如 500 —— QQ 的「大表情/超级表情」）。
                # 协议端必然回 `消息体无法解析` 拒掉**整条**消息，所以这一段直接丢掉，
                # 保住其余文本；整条只有它时会退化成字面 `[表情:500]`（诚实，不静默吞）。
                # 实测见 reports/onebot-face-outbound-fix-2026-10-03.md。
                _log.warning("出站 face 段 id=%s 不在原生表情表里（%s），丢弃这一段",
                             fid, FACE_NAMES.get(int(fid), "非原生表情"))
                _DROPPED_FACES[fid] = _DROPPED_FACES.get(fid, 0) + 1
                dropped.append(fid)
                pos = m.end()
                continue
        if data or ctype == "at":
            segs.append({"type": ctype, "data": data})
        else:
            segs.append({"type": "text", "data": {"text": m.group(0)}})
        pos = m.end()
    if pos < len(text):
        segs.append({"type": "text", "data": {"text": text[pos:]}})
    segs = [s for s in segs if not (s["type"] == "text" and not s["data"].get("text"))]
    if not segs and dropped:
        return [{"type": "text", "data": {"text": " ".join(f"[表情:{i}]" for i in dropped)}}]
    return segs
