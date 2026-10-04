"""假 NapCat —— 一个 OneBot v11 反向 WS *客户端*，用于离线端到端验证适配器。

它做的事和真实 NapCat 的「反向 WS」一模一样：
  1. 用 Bearer token 连到适配器的 WS 服务
  2. 推一条消息事件（post_type=message）
  3. 收到 action 帧（send_private_msg / send_group_msg …）后回一个 echo 响应
  4. 把收到的动作按顺序记下来供断言

**它不与真实 NapCat / AstrBot / 小号发生任何关系** —— 纯进程内假协议端。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List, Optional

import aiohttp


class MockNapCat:
    def __init__(self, url: str, token: str = "", self_id: str = "<BOT_QQ>"):
        self.url = url
        self.token = token
        self.self_id = self_id
        self.actions: List[Dict[str, Any]] = []   # 每个收到的 action 帧（含收到时刻）
        self.raw_frames: List[str] = []
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws = None
        self._recv_task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self.connected = asyncio.Event()

    async def __aenter__(self) -> "MockNapCat":
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        self._session = aiohttp.ClientSession()
        try:
            self._ws = await self._session.ws_connect(self.url, headers=headers, heartbeat=None)
        except BaseException:
            # 握手失败（例如被 401 拒绝）也必须收干净，别漏连接器。
            with __import__("contextlib").suppress(BaseException):
                await self._session.close()
            self._session = None
            raise
        self.connected.set()
        self._recv_task = asyncio.create_task(self._recv_loop())
        return self

    async def __aexit__(self, *exc) -> None:
        self._stop.set()
        if self._recv_task:
            self._recv_task.cancel()
            with __import__("contextlib").suppress(BaseException):
                await self._recv_task
        if self._ws is not None:
            with __import__("contextlib").suppress(BaseException):
                await self._ws.close()
        if self._session is not None:
            with __import__("contextlib").suppress(BaseException):
                await self._session.close()

    async def _recv_loop(self) -> None:
        try:
            async for msg in self._ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                self.raw_frames.append(msg.data)
                try:
                    frame = json.loads(msg.data)
                except ValueError:
                    continue
                if isinstance(frame, dict) and "action" in frame:
                    self.actions.append({"t": time.monotonic(), **frame})
                    # OneBot v11 成功响应形状
                    await self._ws.send_str(json.dumps({
                        "status": "ok", "retcode": 0,
                        "data": {"message_id": 1000 + len(self.actions)},
                        "echo": frame.get("echo"),
                    }))
        except (asyncio.CancelledError, Exception):
            pass

    async def push(self, event: Dict[str, Any]) -> None:
        await self._ws.send_str(json.dumps(event, ensure_ascii=False))

    async def push_private(self, text: str, user_id: str = "10001", message_id: int = 1,
                           nickname: str = "主人") -> None:
        await self.push({
            "post_type": "message", "message_type": "private", "sub_type": "friend",
            "self_id": int(self.self_id) if self.self_id.isdigit() else self.self_id,
            "user_id": int(user_id), "message_id": message_id, "time": int(time.time()),
            "sender": {"user_id": int(user_id), "nickname": nickname},
            "message": [{"type": "text", "data": {"text": text}}],
            "raw_message": text,
        })

    async def push_group(self, text: str, group_id: str = "55555", user_id: str = "10001",
                         message_id: int = 1, nickname: str = "主人") -> None:
        await self.push({
            "post_type": "message", "message_type": "group", "sub_type": "normal",
            "self_id": int(self.self_id) if self.self_id.isdigit() else self.self_id,
            "group_id": int(group_id), "user_id": int(user_id), "message_id": message_id,
            "time": int(time.time()),
            "sender": {"user_id": int(user_id), "nickname": nickname, "card": ""},
            "message": [{"type": "text", "data": {"text": text}}],
            "raw_message": text,
        })

    def sent_texts(self, action: str = "send_private_msg") -> List[str]:
        out = []
        for a in self.actions:
            if a.get("action") != action:
                continue
            msg = (a.get("params") or {}).get("message")
            if isinstance(msg, list):
                out.append("".join(s.get("data", {}).get("text", "") for s in msg
                                   if isinstance(s, dict) and s.get("type") == "text"))
            else:
                out.append(str(msg))
        return out
