"""hermes_delegate —— 给 AstrBot 提供一个「派活给 Hermes 干活门」的 LLM 工具。

与 hermes_forward 的分工（改造计划 v2）：
  * hermes_forward：把每条消息整包转发给 Hermes 聊天门（旧路径，每轮 ~8k token 固定开销）
  * 本插件：不碰消息流。闲聊由 AstrBot 自己的 provider 直答；只有真需要动手的活
    才由 LLM 调用 delegate_to_hermes → A2A 发给干活门（Hermes default profile）

派活契约（2026-09-23 实测通过）：
  出站 POST http://127.0.0.1:9901/   JSON-RPC `message/send`
    params.message = {"role": "user", "messageId": ..., "parts": [{"kind": "text", "text": ...}]}
  回包   result.status.message.parts[0].text   （备用 result.artifacts[].parts[].text）
  对端同步等待上限：A2A_REPLY_TIMEOUT，默认 300 秒（见 /opt/hermes/plugins/platforms/a2a/adapter.py）

注意：AstrBot 的 llm_tool 读的是 docstring 里的 `Args:` 段，不读类型注解——
下面工具的 docstring 改了要连参数说明一起改，否则 LLM 看不到参数。
"""

from __future__ import annotations

import json
import uuid

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

DEFAULT_A2A_URL = "http://127.0.0.1:9901/"
DEFAULT_TIMEOUT = 300


class Main(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}

    # ---- 内部：拼任务书 + 走 A2A ----
    def _build_brief(self, task: str, deliverable: str, acceptance: str) -> str:
        parts = [f"Task: {task.strip()}"]
        if deliverable and deliverable.strip():
            parts.append(f"Deliverable: {deliverable.strip()}")
        if acceptance and acceptance.strip():
            parts.append(f"Acceptance: {acceptance.strip()}")
        parts.append("（来源：聊天门 AstrBot 代主人派单。收到就干，别回问格式。）")
        return "\n".join(parts)

    async def _call_a2a(self, text: str) -> str:
        url = str(self.config.get("a2a_url") or DEFAULT_A2A_URL)
        timeout = int(self.config.get("reply_timeout") or DEFAULT_TIMEOUT)
        payload = {
            "jsonrpc": "2.0",
            "id": f"delegate-{uuid.uuid4().hex[:12]}",
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "messageId": uuid.uuid4().hex,
                    "parts": [{"kind": "text", "text": text}],
                }
            },
        }
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.post(url, json=payload,
                                     timeout=aiohttp.ClientTimeout(total=timeout + 30)) as resp:
                    raw = await resp.text()
                    status = resp.status
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[hermes_delegate] A2A 调用失败: {exc}")
            return f"（派活失败：连不上干活门 {url} — {exc}）"

        if status != 200:
            return f"（派活失败：干活门返回 HTTP {status} — {raw[:200]}）"

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return f"（派活失败：回包不是 JSON — {raw[:200]}）"

        if data.get("error"):
            return f"（派活失败：{str(data['error'])[:200]}）"

        result = data.get("result") or {}
        status_obj = result.get("status") or {}
        texts = []
        msg = status_obj.get("message") or {}
        for p in (msg.get("parts") or []):
            if isinstance(p, dict) and p.get("text"):
                texts.append(p["text"])
        if not texts:
            for art in (result.get("artifacts") or []):
                for p in ((art or {}).get("parts") or []):
                    if isinstance(p, dict) and p.get("text"):
                        texts.append(p["text"])
        state = status_obj.get("state") or "未知状态"
        body = "\n".join(texts).strip() or "（干活门没给正文）"
        return f"[{state}] {body}"

    # ---- 给 LLM 的工具 ----
    @filter.llm_tool(name="delegate_to_hermes")
    async def delegate_to_hermes(self, event: AstrMessageEvent, task: str,
                                 deliverable: str = "", acceptance: str = "") -> str:
        """把需要真动手的活派给 Hermes 干活门执行（跑脚本、查机器、改文件、多轮调研），返回它的结论。

        Args:
            task(string): 要做的事，一句话说清目标
            deliverable(string): 交付物是什么（文件路径或形态），可留空
            acceptance(string): 怎么算完成，可留空
        """
        # 群里一律不接活：派活只由主人在私聊触发
        try:
            if event.get_group_id():
                return "（群聊不派活：只在私聊里派）"
        except Exception:  # noqa: BLE001
            pass
        if not (task or "").strip():
            return "（派活失败：task 为空）"

        brief = self._build_brief(task, deliverable, acceptance)
        logger.info(f"[hermes_delegate] 派活 → {brief[:120]}")
        return await self._call_a2a(brief)
