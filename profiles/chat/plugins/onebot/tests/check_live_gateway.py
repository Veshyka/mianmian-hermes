#!/usr/bin/env python3
"""Live probe: connect to the *running* hermes_onebot adapter as if we were NapCat,
push ONE private message event from the master's QQ, and record what the adapter sends back.

Proves: reverse-WS accept + auth + ingest + agent turn + outbound send, end to end,
against the deployed gateway (not an in-process fixture).

Token is read from the profile .env and never printed.

⚠️ 副作用：适配器只保留一条 WS（新连接会顶掉旧的）—— 本探针一连上，**真 NapCat 会被踢下线**，
   它 3 秒后自动重连；若探针还在跑，出站回复就会落到重连上来的 NapCat 上（即真发到 QQ）。
   要在真机上「只看不动」，别用本脚本，去读 `onebot-state.json` 与 `gateway.log`。

用法:
  /opt/hermes/.venv/bin/python /opt/data/chat-layer/plugin/hermes_onebot/tests/check_live_gateway.py
  PROBE_WAIT=180 PROBE_TEXT='...' ... 同上
退出码: 0 = 收到了出站回复；1 = 没收到
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mock_onebot import MockNapCat  # noqa: E402

ENV = os.environ.get("PROBE_ENV", "/opt/data/profiles/chat/.env")
URL = os.environ.get("PROBE_URL", "ws://127.0.0.1:6700/ws")
MASTER = os.environ.get("PROBE_UID", "<OWNER_QQ>")
TEXT = os.environ.get("PROBE_TEXT", "（切换测试·自动自检）你在吗？")
WAIT = float(os.environ.get("PROBE_WAIT", "150"))


def token() -> str:
    for line in open(ENV, encoding="utf-8"):
        if line.strip().startswith("ONEBOT_ACCESS_TOKEN="):
            return line.split("=", 1)[1].strip()
    return ""


async def main() -> int:
    tok = token()
    print(f"[probe] url={URL} token_present={bool(tok)}")
    async with MockNapCat(URL, token=tok) as mc:
        await asyncio.sleep(0.5)
        await mc.push_private(TEXT, user_id=MASTER, message_id=900001, nickname="主人")
        print(f"[probe] pushed private message from {MASTER}: {TEXT!r}")
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            if mc.actions:
                break
            await asyncio.sleep(0.5)
        await asyncio.sleep(3)  # let multi-segment sends drain
        actions = list(mc.actions)
        print(f"[probe] frames received back: {len(mc.raw_frames)}")
        print(f"[probe] actions: {[a.get('action') for a in actions]}")
        texts = mc.sent_texts()
        print(f"[probe] outbound text count: {len(texts)}")
        for i, t in enumerate(texts, 1):
            print(f"[probe]   [{i}] {t[:200]}")
        print("[probe] RESULT:", "OK" if texts else "NO_OUTBOUND")
        return 0 if texts else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
