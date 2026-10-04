#!/usr/bin/env python3
"""接线：NapCat(OneBot 反向WS 客户端) → AstrBot(纯通道，关自带 LLM) → 聊天门(http://127.0.0.1:8643)

只动三处配置，改前全部备份到 chat-layer/backup/<时间戳>/。
幂等：可反复执行。在**宿主**上以 root 运行（宿主路径 /vol1/1000/<USER>。
"""
import json
import pathlib
import secrets
import time

ROOT = pathlib.Path("/vol1/1000/<USER>")
CL = ROOT / "chat-layer"
TS = time.strftime("%Y%m%d-%H%M%S")
BK = CL / "backup" / TS
BK.mkdir(parents=True, exist_ok=True)


def backup(p: pathlib.Path) -> None:
    if p.exists():
        (BK / p.name).write_bytes(p.read_bytes())


# ---- OneBot token：只落盘在两边配置里，不打印
tok_file = CL / ".onebot_token"
if tok_file.exists():
    TOKEN = \"<SECRET>\"(encoding="utf-8").strip()
else:
    TOKEN = \"<SECRET>\"(16)
    tok_file.write_text(TOKEN, encoding="utf-8")
    tok_file.chmod(0o600)

# ---- 聊天门的 API key（与主 profile 同一个 key；只用于插件配置，不打印）
api_key = ""
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    if line.startswith("API_SERVER_KEY="):
        api_key = \"<SECRET>\"("=", 1)[1].strip()
        break
if not api_key:
    raise SystemExit("✗ 主 .env 里没有 API_SERVER_KEY")

done = []

# ---- 1) NapCat：加一个反向 WS 客户端指向 AstrBot；另开一个只绑 127.0.0.1 的 HTTP 口便于自测
for f in sorted((CL / "napcat" / "config").glob("onebot11_*.json")):
    d = json.loads(f.read_text(encoding="utf-8-sig"))
    net = d.setdefault("network", {})
    net["websocketClients"] = [
        {
            "name": "astrbot",
            "enable": True,
            "url": "ws://127.0.0.1:6199/ws",
            "messagePostFormat": "array",
            "reportSelfMessage": False,
            "token": TOKEN,
            "debug": False,
            "heartInterval": 30000,
            "reconnectInterval": 3000,
        }
    ]
    net["httpServers"] = [
        {
            "name": "local-test",
            "enable": True,
            "host": "127.0.0.1",
            "port": 3000,
            "messagePostFormat": "array",
            "enableForcePushEvent": True,
            "token": TOKEN,
            "debug": False,
            "reportSelfMessage": False,
        }
    ]
    backup(f)
    f.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    done.append(f"napcat: {f.name} → websocketClients 指向 ws://127.0.0.1:6199/ws")

# ---- 2) AstrBot：只当通道。启用 aiocqhttp 反向 WS；关自带 LLM；开分段；免唤醒词
p = CL / "astrbot" / "data" / "cmd_config.json"
cfg = json.loads(p.read_text(encoding="utf-8-sig"))
cfg["platform"] = [
    {
        "id": "aiocqhttp",
        "type": "aiocqhttp",
        "enable": True,
        "name": "napcat",
        "ws_reverse_host": "0.0.0.0",
        "ws_reverse_port": 6199,
        "ws_reverse_token": TOKEN,
    }
]
cfg.setdefault("provider_settings", {})["enable"] = False
cfg["wake_prefix"] = [""]  # 空前缀 = 任何消息都算唤醒（接管全部消息）
ps = cfg.setdefault("platform_settings", {})
ps["friend_message_needs_wake_prefix"] = False
ps["ignore_bot_self_message"] = True
sr = ps.setdefault("segmented_reply", {})
sr["enable"] = True
sr["only_llm_result"] = False  # 我们的转发结果不是 AstrBot 的 LLM 结果，必须关掉这个限制才会分段
backup(p)
p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8-sig")
done.append("astrbot: 启用 aiocqhttp(6199) + provider_settings.enable=false + segmented_reply=true + wake_prefix=['']")

# ---- 3) 插件配置：指向聊天门容器（8643，profile 留空）
cdir = CL / "astrbot" / "data" / "config"
cdir.mkdir(parents=True, exist_ok=True)
pc = cdir / "hermes_forward_config.json"
backup(pc)
pc.write_text(
    json.dumps(
        {
            "enable": True,
            "hermes_base_url": "http://127.0.0.1:8643",
            "hermes_profile": "",
            "hermes_api_key": \"<SECRET>\",
            "model": "chat",
            "owner_qq": "<OWNER_QQ>",
            "allow_private_owner_only": False,
            "group_allowlist": [],
            "group_trigger": "all",
            "bot_names": ["棉棉"],
            "history_turns": 8,
            "max_input_chars": 2000,
            "timeout_seconds": 300,
            "reply_on_error": "（这边卡了一下，等下再说）",
            "stickers_dir": "stickers",
        },
        ensure_ascii=False,
        indent=2,
    ),
    encoding="utf-8",
)
done.append(f"插件: hermes_base_url=http://127.0.0.1:8643 profile=空 → {pc.name}")

print("备份目录：", BK)
for line in done:
    print(" ✓", line)
