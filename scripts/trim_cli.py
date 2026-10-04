#!/usr/bin/env python3
"""trim-cli 官方通道封装（Hermes 容器内用）

为什么存在：宿主级操作应该先走官方通道，但裸调 CLII 要记一长串连接参数、还要处理
"会话过期被伪装成 docker 报错"的坑。这个封装把这两件事都吃掉，让官方通道成为最省事的路径。

用法：
    python3 scripts/trim_cli.py --json docker container ls
    python3 scripts/trim_cli.py docker stats
    python3 scripts/trim_cli.py file ls "/vol1/1000/<USER>"
    python3 scripts/trim_cli.py app status ai_installer
    python3 scripts/trim_cli.py login            # 手动刷新会话

行为：
  * 自动带 --host 172.17.0.1 --port 5666 --scheme ws --allow-insecure-ws
  * 输出里出现 `errno 135168`（= E_INVALID_TOKEN，会话过期，不是权限问题）时：
    自动重登一次再重试原命令，并在 stderr 报告一行
  * --json 把返回的 JSON 美化打印（其余情况原样透传）
  * 凭据只从 /opt/data/scripts/askpass.sh 读，绝不打印

退出码 = trim-cli 的退出码（重试后仍失败则为重试后的码）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

BIN = "/opt/data/skills/productivity/fnos-trim-cli-skill/bin/trim-cli-linux-x64"
ASKPASS = "/opt/data/scripts/askpass.sh"
HOST = "172.17.0.1"
PORT = "5666"
SCHEME = "ws"
USER = "棉棉"
CONN = ["--host", HOST, "--port", PORT, "--scheme", SCHEME, "--allow-insecure-ws"]
SESSION_EXPIRED = "135168"  # = 0x21000 = E_INVALID_TOKEN
# 会话目录固定到一处：trim-cli 默认按 $HOME/.config/trim-cli 找 session，而两个门的 HOME 不一样
# （干活门 = /opt/data/home，聊天门的网关进程 = /opt/data）→ 不写死的话聊天门那边永远没有 session，
# 第一条命令就会卡在交互式 login。写死成同一个目录，两个门共享一份 session。
# 2026-10-03 实测：不设置时 `+status` 会走一次「会话过期→重登」；设置后直接复用。
CONFIG_DIR = "/opt/data/home/.config/trim-cli"


def _env() -> dict:
    env = dict(os.environ)
    env.setdefault("TRIM_CLI_CONFIG_DIR", CONFIG_DIR)
    return env


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([BIN, *CONN, *args], capture_output=True, text=True, env=_env())


def _password() -> str:
    """执行 askpass.sh 取密码；从不打印。"""
    return subprocess.run(["bash", ASKPASS], capture_output=True, text=True).stdout.strip()


def login() -> bool:
    """刷新会话 token（trim-cli 只支持命令行传密码）。"""
    pw = _password()
    if not pw:
        return False
    r = subprocess.run(
        [BIN, *CONN, "login", "-u", USER, "-p", pw, "--trust-device"],
        capture_output=True,
        text=True,
        env=_env(),
    )
    return r.returncode == 0


def main(argv: list[str]) -> int:
    pretty = "--json" in argv
    args = [a for a in argv if a != "--json"]
    if not args:
        print(__doc__)
        return 2

    r = _run(args)
    combined = (r.stdout or "") + (r.stderr or "")
    if SESSION_EXPIRED in combined:
        print("[trim_cli] 会话过期（errno 135168）→ 重登后重试", file=sys.stderr)
        if login():
            r = _run(args)
            combined = (r.stdout or "") + (r.stderr or "")
            if SESSION_EXPIRED in combined:
                print("[trim_cli] 重登后仍报 135168，请人工检查", file=sys.stderr)
        else:
            print("[trim_cli] 重登失败（检查 askpass.sh / 账号）", file=sys.stderr)

    out = r.stdout or ""
    if pretty and out.strip():
        try:
            out = json.dumps(json.loads(out), ensure_ascii=False, indent=2)
        except Exception:
            pass
    sys.stdout.write(out)
    if r.stderr:
        sys.stderr.write(r.stderr)
    return r.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
