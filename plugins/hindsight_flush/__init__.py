"""干活门（默认 profile）的 Hindsight「退出冲缓冲」补丁 —— 稳定性优先的最小改动。

为什么要有它
------------
bundled provider 的 `shutdown()` **不冲缓冲**（`plugins/memory/hindsight/__init__.py` 里那个
shutdown 只做三件事：停 writer、等 prefetch、关 client）。而 `retain_every_n_turns: 2` 时，
第 1 轮只在内存里等第 2 轮：
  * 正常换会话（/new、压缩、resume）→ bundled 自带 flush-on-switch ✓ 不丢；
  * 进程被硬杀（容器重启、OOM、`docker kill`）→ 那一轮就没了 ✗。
聊天门那边靠 `hindsight_guard` 里的同名补丁兜住；干活门原来用 bundled 原样，所以补这一个。

它做了什么（就一件事）
----------------------
只重写 `shutdown()`：缓冲非空时借 **bundled 自己的** `on_session_switch()` 冲一次
（和「切换会话」走同一条路径，不自己造写库逻辑），然后原样 `super().shutdown()`。
其余一切行为**完全委托**给 bundled 实现 —— 这个文件不改任何 retain/recall 语义。

稳定性约定（别删这几条）
------------------------
1. 冲缓冲整体 try/except：**任何异常都不许影响退出**（宁可丢一轮，也不能让进程退不干净）；
2. 冲缓冲失败只记一行 warning，不抛、不重试、不阻塞（bundled 的 shutdown 里还有 10s 有限等待）；
3. bundled 导入失败时本类退化成空壳，`shutdown()` 不做事（别把启动拖垮）；
4. 一行回退：`config.yaml` 里 `memory.provider: hindsight` → 立刻回到 bundled 原样。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

try:                                                     # bundled 导入失败也只是少个补丁
    from plugins.memory.hindsight import HindsightMemoryProvider as _Base
except Exception as _e:                                  # noqa: BLE001
    _Base = None                                         # type: ignore[assignment]
    logger.warning("[hindsight_flush] 导入 bundled provider 失败：%r（本插件退化为空壳）", _e)


if _Base is not None:

    class HindsightFlushProvider(_Base):                 # type: ignore[misc]
        """bundled provider + 退出前冲缓冲。"""

        def shutdown(self) -> None:
            try:
                pending = list(getattr(self, "_session_turns", None) or [])
                if pending:
                    logger.info("[hindsight_flush] 退出前冲缓冲：%d 轮（retain_every_n=%s）",
                                len(pending), getattr(self, "_retain_every_n_turns", "?"))
                    # 借 bundled 的 flush-on-switch：先按旧 session 冲缓冲，再轮转（退出时轮转无所谓）。
                    # ⚠️ 别在这里调任何「看起来该有」的方法：bundled 没有 `_forward_kwargs`
                    #    （那是闸门自己定义的帮助函数）—— 2026-10-04 写这个插件时先踩了一次，
                    #    靠 tmp/check_hindsight_flush.py 拦下来了。只传它签名里真的有的东西。
                    self.on_session_switch(getattr(self, "_session_id", "") or "")
            except Exception as e:                       # noqa: BLE001 —— 冲缓冲失败不许挡退出
                logger.warning("[hindsight_flush] 退出前冲缓冲失败（不影响退出）：%r", e)
            super().shutdown()

else:                                                    # pragma: no cover —— 只在 bundled 缺失时

    class HindsightFlushProvider:                        # type: ignore[no-redef]
        """空壳：bundled 不可用时不该让 provider 加载失败。"""

        def __init__(self, *a, **k) -> None:
            pass
