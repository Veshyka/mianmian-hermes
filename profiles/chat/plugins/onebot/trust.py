"""信任档（Trust Tier）—— 按 uid 判「主人 / 生人」的最小可插拔判据层。

**主人 2026-09-23 拍板**：私聊准入保持**全开**（`dm_policy: open` + `allow_from: ['*']`，
陌生人来也回），但**除主人外一律按「群聊生人」同款对待**。

这条规则的落点必须是**机制**（按 uid 判），提示词只做第二道 —— 所以判据抽在这里：
适配器、提示词注入、C 阶段的群门控都可以问它，而不用各自重新写一遍「谁是主人」。

**它不是准入门**：本模块**不参与** `dm_policy` / `allow_from` 的放行判定，
import 它、调用它都不会改变任何人的准入结果（准入全开是主人的明确要求，别在这里收窄）。

两个轴，别混（这是本模块唯一容易写错的地方）：

* **身份** `is_owner(uid)` —— 这个 uid 是不是主人，**与场合无关**。
  用途：群里认出主人（可以叫主人、可以 @ 他）。
* **档位** `classify(uid, is_group=…)` —— **这一轮按哪一档对待**，**与场合有关**：
  群里一律 `stranger`（**含主人本人在场**：公共场合不因熟人在场就变私密）。

档位的含义（细则写在人设文件里，这里只登记语义）：

| 档 | 谁 | 待遇 |
|---|---|---|
| `owner` | 主人大号，**私聊** | 可亲昵、可接活、可动记忆、私聊可挑逗 |
| `stranger` | 其他任何人（**陌生私聊 + 群里所有人**） | 礼貌克制、不谈内部、不接活、不主动套近乎、不写与主人相关的记忆 |

本模块**默认不接进任何调用路径**（默认行为不变）：它只是把「谁是主人」这件事
变成一个可测的、可配置的判据。真正接线（把档位注入本轮上下文）属于 C 阶段。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional, Tuple

__all__ = [
    "TIER_OWNER",
    "TIER_STRANGER",
    "VALID_TIERS",
    "DEFAULT_OWNER_IDS",
    "CONFIG_KEY",
    "resolve_owner_ids",
    "is_owner",
    "classify",
    "is_public",
]

TIER_OWNER = "owner"
TIER_STRANGER = "stranger"
VALID_TIERS = (TIER_OWNER, TIER_STRANGER)

#: 熟人档默认＝主人大号（QQ uid）。**写死在代码里是有意的**：
#: 配置写空 / 写坏时也不能让主人掉进生人档（fail-safe 方向是「宁可把主人当主人」，
#: 而**不是**「宁可把陌生人当主人」—— 后者靠这里的默认值不可能发生）。
DEFAULT_OWNER_IDS: Tuple[str, ...] = ("<OWNER_QQ>",)

#: 配置键名（`platforms.onebot.extra.trust_tier_owner_ids`，见 PLAN-group-chat.md §3.3 G 组）。
CONFIG_KEY = "trust_tier_owner_ids"

_SEP = re.compile(r"[,\s;]+")


def _norm_id(value: Any) -> str:
    """QQ uid 归一化成裸数字串；空/None → ``""``（调用方按空处理）。"""
    if value is None:
        return ""
    return str(value).strip()


def resolve_owner_ids(value: Any = None) -> Tuple[str, ...]:
    """把配置值解析成 owner uid 元组。

    接受：``None`` / 单个 ``str``（逗号、空格、分号分隔都认）/ 任意可迭代
    （元素可以是 ``int``——YAML 里不写引号的 QQ 号会被读成 int，这是最常见的坑）。

    **空值 / 全是空白 → 回落到** :data:`DEFAULT_OWNER_IDS`（见其注释）。
    去重保序。
    """
    parts: Iterable[Any]
    if value is None:
        parts = ()
    elif isinstance(value, str):
        parts = _SEP.split(value)
    elif isinstance(value, (bytes, bytearray)):
        parts = (value,)
    else:
        try:                       # 任意可迭代（list/tuple/set/dict 的键…）
            parts = list(value)
        except TypeError:          # 单个数字之类的标量
            parts = (value,)

    out = []
    for p in parts:
        uid = _norm_id(p)
        if uid and uid not in out:
            out.append(uid)
    return tuple(out) if out else DEFAULT_OWNER_IDS


def is_owner(uid: Any, owner_ids: Any = None) -> bool:
    """**身份判定**：这个 uid 是不是主人（主人 uid 用 ``str`` 比，``int`` 也认）。"""
    uid_s = _norm_id(uid)
    if not uid_s:
        return False
    return uid_s in resolve_owner_ids(owner_ids)


def is_public(is_group: bool) -> bool:
    """**场合判定**：群里＝公共场合。

    公共场合与身份是**两个轴**：主人本人在群里，身份仍是 owner，
    但场合仍是公共 —— 「群里不吐私聊」这条规则只认场合，不认身份。
    """
    return bool(is_group)


def classify(uid: Any, *, is_group: bool = False, owner_ids: Any = None) -> str:
    """**档位判定**：这一轮按 ``owner`` 还是 ``stranger`` 对待。

    - 群消息（``is_group=True``）→ 一律 :data:`TIER_STRANGER`，
      **包括主人自己发的**（公共场合规则，见模块 docstring）；
      —— 想认「他在群里是谁」用 :func:`is_owner`，那是另一个轴。
    - 私聊 → uid 在 owner 名单里＝:data:`TIER_OWNER`，否则 :data:`TIER_STRANGER`。

    未知 / 空 uid → :data:`TIER_STRANGER`（fail-safe：判不出就当生人）。
    """
    if is_group:
        return TIER_STRANGER
    return TIER_OWNER if is_owner(uid, owner_ids) else TIER_STRANGER
