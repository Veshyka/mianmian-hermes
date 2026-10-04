"""出站分段（纯函数，零外部依赖，可离线单测）。

**规则（2026-09-23 主人拍板，简化成一条）**：

    出现 `⁂` 就切；没有 `⁂` 就整段发。**没有长度阈值。**

为什么去掉长度阈值：旧实现抄 AstrBot 的「`len > threshold` → 不切、整条发」，结果
字数多（尤其英文占比高、字符数容易超）时整条不切 —— **不切也就不清理** ——
`⁂` 就原样留在正文里被主人看到了。所以阈值这条规则被整个删掉，连配置项一起废弃
（`platforms.onebot.extra.segment_threshold` 已不再被读取，RUNBOOK 有登记）。

由此带来的两条硬约束（都要守住）：

  1. **切分只看分隔符**：句末标点「。？！~…」与换行都**不再切分**（旧版会）。
     否则「不含 `⁂` 的 1000+ 字长文要一条不切」这条做不到 —— 长文里必然有句号或换行。
  2. **任何情况下 `⁂` 都不许出现在主人收到的消息里**（硬兜底 `scrub()`）：
     正常切分、整段发、以及异常/降级路径（`segment_enabled: false`、正则写坏、
     模型输出怪东西）全部先过 `scrub()` 再发。

沿用 AstrBot 已验证的部分（源码级参照）：
  * 切分：`/AstrBot/astrbot/core/pipeline/result_decorate/stage.py:229-245`
      `re.findall(regex, text, re.DOTALL | re.MULTILINE)`（正则已按上面第 1 条收窄成「只有分隔符」）
  * 清理：同文件 `:251-263`  先 `re.sub(cleanup, "", seg)` 再 `seg.strip()`，空段丢弃。
  * 间隔：`/AstrBot/astrbot/core/pipeline/respond/stage.py:78` `self.interval = [1.5, 3.5]`
      同文件 `:98-107` `random.uniform(interval[0], interval[1])`。

有意保留的两处「继承边界」：
  1. `cleanup` 用一条失败即置 None 的容错（AstrBot 写法），但这里不改写传入参数，只对本次失效。
     —— 注意 `scrub()` 的兜底**比它更硬**：正则坏了也逐字符滤，符号绝不漏出。
  2. AstrBot 在**每条**（含第一条）发送前 sleep。这里默认 `leading_delay=False`
     —— 第一条不延迟（延迟只影响「段与段之间」，而用户要的正是段间停顿）。
     要逐字复刻 AstrBot，把 leading_delay 置 True 即可。
"""

from __future__ import annotations

import random
import re
from typing import Callable, List, Sequence, Tuple

# ── 分隔符：**唯一改动点**（两侧同步：人格 + 这里） ────────────────────────────
# 为什么是 ⁂ (U+2042)：`※` 在中文文档里会真实出现——她要是发一段带 ※ 的文档，
# 就会被误切成多条。⁂（三星号/asterism）在中文正文里基本不出现。
SEP = "⁂"
"""**当前**段分隔符（写进人格的那一个）。"""

SEP_TOLERATED = ("⁂", "※", "⸮")
"""容错集合：模型输出变体（⸮ U+2E2E）与旧习惯（※ U+203B）都给清掉，
免得换符号后旧话术漏出分隔符、或整段被并成一条。"""

# ⚠️ 正则里**只有分隔符**，没有句末标点、没有阈值 —— 见模块头注第 1 条。
# 尾段用 `.+`（**不写 `$`、不带 MULTILINE 的锚点**）：否则 `.` 会在每个换行处收尾，
# 一段不含分隔符的多行长文会被拆成好几行 —— 那正是「长内容整段发」要避免的。
DEFAULT_REGEX = r".*?[⁂※⸮]+|.+"
DEFAULT_CLEANUP_RULE = r"[⁂※⸮]"

# ⚠️ 长度阈值已废弃（2026-09-23 主人拍板）：这里**不许再加回** DEFAULT_THRESHOLD 之类的东西。
#    需要「长内容整段发」就靠人格里「长内容一个 `⁂` 都不写」那条 —— 不写符号自然不切。

DEFAULT_INTERVAL: Tuple[float, float] = (1.5, 3.5)


def scrub(text: str, *, cleanup: str = DEFAULT_CLEANUP_RULE) -> str:
    """**硬兜底**：把分隔符从任何准备发出去的文本里摘干净。

    兜底比 `cleanup` 正则更硬：正则坏掉/为空时退回**逐字符白名单过滤**
    （`SEP_TOLERATED` 里的字符一律删掉）。代价只可能是「少几个怪字符」，
    收益是「主人永远不会看到 `⁂`」——这是硬约束，值得。
    """
    raw = text or ""
    if not raw:
        return ""
    if cleanup:
        try:
            return re.sub(cleanup, "", raw)
        except re.error:
            pass
    return "".join(ch for ch in raw if ch not in SEP_TOLERATED)


def _apply_cleanup(seg: str, cleanup: str) -> str:
    """清掉段内的分隔符再 strip（失败即跳过 —— 本次清理失效，但消息照样发得出去）。"""
    if cleanup:
        try:
            seg = re.sub(cleanup, "", seg)
        except re.error:
            pass
    return seg.strip()


def split_text(
    text: str,
    *,
    regex: str = DEFAULT_REGEX,
    cleanup: str = DEFAULT_CLEANUP_RULE,
) -> List[str]:
    """出现 `⁂`（容错 `※`/`⸮`）就切成多条；没有就整段发。**不看长度。**

    返回空列表表示「没有可发内容」（空串，或整条只有分隔符被清空了）。
    返回值里**不含** `⁂`/`※`/`⸮`，且逐段 strip 过。
    """
    raw = text or ""
    if not raw.strip():
        return []

    try:
        parts = re.findall(regex, raw, re.DOTALL | re.MULTILINE)
    except re.error:
        # 正则写坏时退回默认（AstrBot 同款兜底，stage.py:241-245）。
        parts = re.findall(DEFAULT_REGEX, raw, re.DOTALL | re.MULTILINE)

    out: List[str] = []
    for seg in parts:
        if isinstance(seg, tuple):  # 带捕获组的正则会返回 tuple
            seg = seg[0] if seg else ""
        if not isinstance(seg, str):
            continue
        seg = _apply_cleanup(seg, cleanup)
        if seg:
            out.append(seg)

    # 全部被清空（例如整条只有 ⁂）时**不退回原文** —— 退回原文等于把分隔符发给主人看。
    # 返回空列表 → 上层视作「没有可发内容」，不发即可。
    return out


def segment_intervals(
    count: int,
    *,
    interval: Sequence[float] = DEFAULT_INTERVAL,
    leading_delay: bool = False,
    rng: Callable[[float, float], float] | None = None,
) -> List[float]:
    """返回每条消息**发送前**应等待的秒数（长度 = count）。

    ``leading_delay=False``（默认）→ 第一条 0.0，段间为 [interval[0], interval[1]] 均匀随机。
    ``leading_delay=True`` → 每条都延迟（逐字复刻 AstrBot respond/stage.py:276-278）。
    """
    if count <= 0:
        return []
    lo, hi = float(interval[0]), float(interval[1])
    if hi < lo:
        lo, hi = hi, lo
    uniform = rng or random.uniform
    delays = [uniform(lo, hi) for _ in range(count)]
    if not leading_delay and delays:
        delays[0] = 0.0
    return delays


def parse_interval(raw, default: Sequence[float] = DEFAULT_INTERVAL) -> Tuple[float, float]:
    """解析配置里的间隔，形如 ``"1.5,3.5"`` / ``"1.5-3.5"`` / ``[1.5, 3.5]`` / 单值。"""
    if raw is None or raw == "":
        return (float(default[0]), float(default[1]))
    if isinstance(raw, (list, tuple)) and len(raw) >= 1:
        try:
            vals = [float(x) for x in raw]
        except (TypeError, ValueError):
            return (float(default[0]), float(default[1]))
    else:
        text = str(raw).replace(" ", "").replace("~", ",").replace("-", ",")
        text = text.replace("，", ",")
        try:
            vals = [float(x) for x in text.split(",") if x != ""]
        except (TypeError, ValueError):
            return (float(default[0]), float(default[1]))
    if not vals:
        return (float(default[0]), float(default[1]))
    if len(vals) == 1:
        return (vals[0], vals[0])
    return (vals[0], vals[1])
