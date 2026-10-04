#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""health_cron.py — Hermes cron 用的「健康静默 / 异常开口」包装器（no-agent 模式）。

它**只调用** /opt/data/scripts/health_all.py，不改动其任何检查逻辑。

语义（配合 `hermes cron create --no-agent`）：
  · 健康      -> 不打印任何东西（空 stdout = 静默 tick，不打扰主人）
  · 只有「提醒」(⚠️) -> **也不出声**：能自愈/不需要人干预的事只写日志（主人被刷过一次，明确说别刷）
  · 有「失败」(❌) -> 打印一段简短告警（stdout 被 cron 原样投递到主人）
  · 同一异常持续 -> 不每轮都吵，每 RE_ALERT_EVERY 轮（约 4 小时）提醒一次，其余轮静默
  · 恢复      -> 只写日志，不打扰主人
  · 每次运行都追加一行到 /opt/data/logs/health-cron.log（北京时间 + 结论），便于事后回溯；
    异常时把完整明细另存 logs/health-cron-last-fail.log。

★ 告警去向（2026-09-23 定案）：**本包装器的 stdout 就是唯一告警通道**——cron job
  `5b3410f7489e` 的 `deliver` 指向主人 QQ 私聊（官方 bot 门 qqbot:CCDFBF85…），告警文本由
  Hermes 自己的 cron 投递机制送到主人。**完全不经过 AstrBot**（AstrBot 的 :8098/report 已废弃）。
  本包装器还会给子进程设 `HEALTH_CRON_WRAPPER=1`：health_all.py 看到它就不再自己
  `hermes send` 直发一遍（避免重复投递；它自己也认得父进程命令行，不依赖这个 env）。

自身永远 exit 0：真实故障由 stdout 表达；只有包装器自己坏掉才让 cron 走
«脚本非零退出 -> failure 告警» 那条兜底路。

演练开关（只在演练时存在，平时不要创建）：
    创建 /opt/data/cron/.health_cron_force_fail 后，本包装器会无视真实健康结果，
    打印一条标注【演练】的告警并照常走 cron 投递，用来验证「异常能到达主人」。
    演练完请立刻删除该文件。
    命令行参数会原样透传给 health_all.py（例如 `health_cron.py --simulate-fail`）。
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime

try:  # 容器本地时区是 UTC，日志与 cron 展示（+08:00）对不上，统一按北京时间记
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    TZ = None

HERMES_HOME = "/opt/data"
HEALTH = "/opt/data/scripts/health_all.py"
LOG = "/opt/data/logs/health-cron.log"
FAIL_DETAIL = "/opt/data/logs/health-cron-last-fail.log"
STATE = "/opt/data/cron/health_cron_state.json"
DRILL_MARKER = "/opt/data/cron/.health_cron_force_fail"
TIMEOUT = 600          # health_all.py 实测约 20s；给足余量，超时也算异常
RE_ALERT_EVERY = 8     # 同一异常连续未恢复时，每 8 轮（30min × 8 = 4h）再提醒一次


def log(line):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line.rstrip("\n") + "\n")
    except Exception:
        pass


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_state(d):
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception:
        pass


def item_name(line):
    """从 health_all.py 的明细行里取检查项名（去掉会变的读数）：
    '❌ AstrBot ERROR/Traceback 计数（本次启动以来）     26 条  例：…' -> 'AstrBot ERROR/Traceback 计数（本次启动以来）'"""
    body = line.lstrip("❌⚠️ \u26a0\ufe0f").strip()
    return re.split(r"\s{2,}", body)[0].strip()


def main():
    env = dict(os.environ)
    env.setdefault("HOME", HERMES_HOME)
    env["HERMES_HOME"] = HERMES_HOME
    env["HEALTH_CRON_WRAPPER"] = "1"      # 告诉 health_all.py：别再自己 hermes send 直发
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    env["PATH"] = env.get("PATH") or "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    if "/opt/hermes/bin" not in env["PATH"]:
        env["PATH"] = env["PATH"] + ":/opt/hermes/bin"

    t0 = datetime.now()
    ts = (t0.astimezone(TZ) if TZ else t0).strftime("%Y-%m-%d %H:%M:%S %z")
    try:
        p = subprocess.run([sys.executable or "python3", HEALTH] + sys.argv[1:],
                           capture_output=True, text=True, timeout=TIMEOUT,
                           env=env, cwd=HERMES_HOME, errors="replace")
        rc, out, err = p.returncode, p.stdout or "", p.stderr or ""
    except Exception as e:
        rc, out, err = 254, "", "%s: %s" % (type(e).__name__, e)

    secs = (datetime.now() - t0).total_seconds()
    lines = [l for l in out.splitlines() if l.strip()]
    summary = lines[0] if lines else "(无输出)"
    detail = [l for l in lines[2:] if l.strip() and not l.startswith(("-", "（"))]
    bad = [l for l in detail if l.lstrip().startswith(("❌", "⚠️"))]
    bad_fail = [l for l in bad if l.lstrip().startswith("❌")]
    drill = os.path.exists(DRILL_MARKER)
    healthy = (rc == 0 and not bad) and not drill
    st = load_state()

    if healthy:
        if st.get("sig"):
            log("[%s] rc=%d %.1fs | 已恢复正常｜%s" % (ts, rc, secs, summary))
            save_state({})
        else:
            log("[%s] rc=%d %.1fs | %s" % (ts, rc, secs, summary))
        return 0  # 静默：空 stdout -> cron 不投递、不打扰主人

    # 分级：只有「提醒」(⚠️)、没有「失败」(❌) -> 能自愈/不需要人干预 -> 只写日志，不出声。
    # （rc in (0,1) 且确有输出才走这条：包装器自己崩了 / 没输出时仍要按异常报。）
    if not bad_fail and not drill and rc in (0, 1) and lines:
        log("[%s] rc=%d %.1fs | 仅提醒（不出声）：%s" % (ts, rc, secs, summary))
        for l in bad[:6]:
            log("        " + l.strip())
        return 0

    # 失败签名 = 当前异常项名集合（不含会变动的读数），用来判断「是不是同一个老问题」
    sig = "|".join(sorted(item_name(l) for l in bad)) or summary
    same = (st.get("sig") == sig)
    repeat = int(st.get("repeat") or 0) + 1 if same else 1
    save_state({"sig": sig, "repeat": repeat, "last_run": ts})

    if same and not drill and (repeat - 1) % RE_ALERT_EVERY:
        log("[%s] rc=%d %.1fs | 同一异常仍在持续（第 %d 轮，本轮静默）｜%s"
            % (ts, rc, secs, repeat, summary))
        return 0

    if same and repeat > 1:
        summary = "（同一异常已连续 %d 轮未恢复，第 %d 次提醒）｜%s" % (
            repeat, (repeat - 1) // RE_ALERT_EVERY + 1, summary)
    if drill and not bad:
        summary = "【演练】人为触发告警通道（非真实故障）｜" + summary

    log("[%s] rc=%d %.1fs | %s%s" % (ts, rc, secs, "[演练] " if drill else "", summary))

    try:
        with open(FAIL_DETAIL, "w", encoding="utf-8") as f:
            f.write(out + ("\n--- stderr ---\n" + err if err.strip() else ""))
    except Exception:
        pass

    print("🚨 健康自检异常（%s，用时 %.0fs）" % (ts, secs))
    print(summary)
    for l in (bad[:12] if bad else detail[:12]):
        print(l)
    print("详见 /opt/data/logs/health-cron.log 与 health-cron-last-fail.log")
    return 0


if __name__ == "__main__":
    sys.exit(main())
