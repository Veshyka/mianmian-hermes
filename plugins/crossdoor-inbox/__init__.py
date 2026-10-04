"""crossdoor-inbox —— 跨门任务书收件箱（干活门侧）。

为什么有它：聊天门派活原本走 a2a_call（同步调用）——派活方要在那一轮里等着，
主人也看不见任务书。本插件把"派活"改成一次**不阻塞的文件投递**：

    聊天门 write_file 任务书 → /opt/data/var/crossdoor-inbox/<名字>.md   （写完即走，不卡）
         ↓ 本插件轮询（2 秒）
    PluginContext.inject_message(text, role="user", session_key=<主人QQ私聊会话>)
         ↓
    主人在 QQ 那条对话里看到任务书 → 干活门在该会话里被真正激活一轮 → 回复回该会话

三条硬约束（改坏会丢任务或重复干活，别动）：
1. **只在持有 live gateway 的进程里消费文件**（`has_gateway_message_injector`）。CLI/cron 进程
   也加载插件，但没有网关注入器 —— 它们只看不动，否则会把任务吞掉。
2. **原子认领**：`os.replace(INBOX/f, .processing/f)` 只有一个进程能成功 → 多进程不重复注入。
3. **注入失败必须把文件退回 INBOX**，绝不允许归档掉（否则任务静默消失）。

配置（`config.yaml`）：
    plugins:
      enabled: [..., crossdoor-inbox]
      entries:
        crossdoor-inbox:
          allow_gateway_injection: true      # 框架的同意闸，fail-closed，必须显式开
          settings:
            session_key: "agent:main:qqbot:dm:<chat_id>"   # 可选，默认主人 QQ 私聊
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

INBOX = Path(os.environ.get("CROSSDOOR_INBOX", "/opt/data/var/crossdoor-inbox"))
PROCESSING = INBOX / ".processing"
DONE = INBOX / "done"
# 主人 QQ 官方 bot 私聊会话（= 主人和干活门对话的那条）
DEFAULT_SESSION_KEY = "agent:main:qqbot:dm:<DM_CHAT_ID>"
POLL_SECONDS = 2.0
MARK = "[跨门任务书 · 来自聊天门]"
SUFFIXES = ("*.md", "*.txt")

_stop = threading.Event()
_thread: threading.Thread | None = None


def _claim(path: Path) -> Path | None:
    """原子认领一个待处理文件；抢不到（已被别的进程拿走）返回 None。"""
    target = PROCESSING / path.name
    try:
        os.replace(path, target)
        return target
    except OSError:
        return None


def _gateway_ready() -> bool:
    from hermes_cli.plugins import get_plugin_manager

    return bool(get_plugin_manager().has_gateway_message_injector)


def _inject(ctx, session_key: str, text: str) -> bool:
    """请求网关把这条内容当入站消息投进目标会话；True = 网关已接受异步派发。"""
    if not _gateway_ready():
        return False
    return bool(ctx.inject_message(text, role="user", session_key=session_key))


def _drain(ctx, session_key: str) -> None:
    import time as _time

    for pattern in SUFFIXES:
        for path in sorted(INBOX.glob(pattern)):
            # 刚写进来的文件先等 2 秒：避免读到"写了一半"的内容
            try:
                if _time.time() - path.stat().st_mtime < 2.0:
                    continue
            except OSError:
                continue
            claimed = _claim(path)
            if claimed is None:
                continue
            try:
                body = claimed.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                body = ""
            if not body:
                os.replace(claimed, DONE / claimed.name)
                logger.warning("crossdoor-inbox: %s 是空文件，已归档未注入", path.name)
                continue
            text = f"{MARK} {path.name}\n\n{body}"
            if _inject(ctx, session_key, text):
                os.replace(claimed, DONE / claimed.name)
                logger.info("crossdoor-inbox: 已注入 %s（session=%s）", path.name, session_key)
            else:
                # 无网关/被拒 → 退回，等有网关的进程或下一轮再试
                os.replace(claimed, INBOX / claimed.name)
                logger.warning("crossdoor-inbox: 注入未成功，已退回 %s", path.name)
                return


def _loop(ctx, session_key: str) -> None:
    while not _stop.is_set():
        try:
            _drain(ctx, session_key)
        except Exception:
            logger.warning("crossdoor-inbox: 轮询异常", exc_info=True)
        _stop.wait(POLL_SECONDS)


def register(ctx) -> None:
    global _thread
    for d in (INBOX, PROCESSING, DONE):
        d.mkdir(parents=True, exist_ok=True)
    session_key = ctx.get_config("session_key", DEFAULT_SESSION_KEY) or DEFAULT_SESSION_KEY
    if _thread is None or not _thread.is_alive():
        _thread = threading.Thread(
            target=_loop, args=(ctx, session_key), name="crossdoor-inbox", daemon=True,
        )
        _thread.start()
        logger.info("crossdoor-inbox: 收件箱已启动 %s → session=%s", INBOX, session_key)

    @ctx.on_unload
    def _stop_watcher() -> None:
        _stop.set()
