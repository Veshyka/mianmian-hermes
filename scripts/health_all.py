#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""health_all.py — 一条命令查全部健康（Hermes 双侧 / A2A / Hindsight / AstrBot 插件与补丁）

用法:
    python3 /opt/data/scripts/health_all.py            # 人话总览 + 明细
    python3 /opt/data/scripts/health_all.py --json     # 机器可读
    python3 /opt/data/scripts/health_all.py --no-alert # 不告警（只打印）
    python3 /opt/data/scripts/health_all.py --simulate-fail  # 演练告警通道（会真发一条标注【自检】的告警）

为什么需要这个脚本（官方入口不够用的部分，逐条说明）:
  官方已覆盖、本脚本只做**封装解析**、绝不重写:
    · 双侧 gateway 在不在跑  -> `hermes gateway list`（官方，给出 PID）
    · s6 托管 / 记忆 provider / profile 门  -> `hermes doctor`
    · AstrBot 源码补丁在不在位 -> `docker exec astrbot python3 /opt/astrbot-patches/verify_patch.py`（项目官方自检，exit 0 = 5 条 AST 判据全过）
    · hermes_report 接收端活体 -> `GET http://172.17.0.1:8098/health`（插件自带探针）
  官方没有、必须补的最小探针:
    1) 端点真实可达性：8642/8643(api_server) / 9901·9900(A2A) / 8888(Hindsight) / 9119(dashboard)
       ——`hermes doctor` 只判进程与工具可用性，不发真实请求；dashboard 端口现网是 9119（host 网络），
         任务书里的 19119 是旧发布端口，已失效，本脚本两个都试并如实打印。
    2) AstrBot 运行态读数：MCP hindsight 1/1、provider 装载、适配器连接、ERROR/Traceback 计数
       ——AstrBot 面板/CLI 无「一条命令看这些」的官方入口（`astrbot plug search` 在容器里直接报不是有效根目录）。
    3) 插件配置值核对（debounce wait=10.0s / hermes_memory 双向开关 / hermes_report 两个开关 / memo / lookup）
       ——这些值会被并发施工改回旧值，只有逐个核对配置值才能发现；没有官方接口暴露它们。
    4) 源码副本 vs 加载副本 md5 一致性（改完没同步 = 白改，且静默退化）。
    5) 异常时主动告警：**只有一条通道——Hermes 自己**：挂 cron 时由 health_cron.py 的 stdout
       经 Hermes cron 投递机制送到主人 QQ 私聊；手工直跑时用官方 `hermes send -t <主人私聊>`。
       **不依赖 AstrBot**。（早期用的 `POST :8098/report`（AstrBot 插件 hermes_report）已整条删除：
       它要靠 AstrBot 平台才能发消息，AstrBot 一退役/停容器就必然 500。2026-09-23 实测。）

只读：不改任何配置、不重启任何服务。

★ AstrBot 判据分层（2026-09-23 定案）
  AstrBot 是【旧聊天门】，现已被 Hermes 自己的 OneBot 适配器取代；2026-09-23 主人同意
  **停掉它的容器**（`docker stop astrbot`，容器保留、配置零改动、随时可开）。
  · 容器停着（NapCat 的 `websocketClients[0].url` 指向 Hermes 的 :6700）＝ **已退役，预期**：
    插件参数 / 工具面 / 补丁 / 适配器 / ERROR 计数 全部 **跳过**，不判失败、也不报提醒（主人
    明确说过别再拿这条刷他）。容器一旦重新启动（`docker start astrbot`），这些检查**自动恢复**。
  · 容器停着但 NapCat 还指着 AstrBot :6199 ＝ 在役链路断了 → **fail（要人干预）**。
  · 容器在跑 → 做完整检查；此时 NapCat 指着 Hermes 的话，「适配器不可用 / 平台类 ERROR」仍按预期处理，
    只有**非平台类 ERRO 级**错误才算故障（WARN 级 traceback 只提醒；平台/非平台由
    `scripts/ab_log_classify.py` 按「时间戳切事件 + 分级」判定，不再用阈值凑）。
"""

import argparse
import base64
import glob
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime

HERMES = "/opt/hermes/bin/hermes"
FYGO_SSH = "/opt/data/scripts/fygo_ssh.sh"
HOST_SUDO = "/vol1/1000/<USER>"
HOST_HERMES_ROOT = "/vol1/1000/<USER>"       # = 容器内 /opt/data
PLUGIN_DIR_LOCAL = "/opt/data/chat-layer/plugin"       # 源码副本（改这里）
PLUGIN_DIR_LIVE = "/opt/data/stack/astrbot/data/plugins"  # 容器实际加载的副本

# ---- 告警去向（只有一条：Hermes 自己）--------------------------------------
# 主人 QQ 私聊（官方 bot 门）。`hermes send -t` 直投，不经过 AstrBot。
# ❌ 已废弃：早期走的 `POST http://172.17.0.1:8098/report`（AstrBot 插件 hermes_report）——
#    它要靠 AstrBot 平台才能把消息发出去，AstrBot 一退役/停容器就必然 500（2026-09-23 实测
#    `HTTP Error 500`），属于「告警通道依赖被退役的一方」的自杀式设计，已整条删除。
ALERT_TARGET = "qqbot:<DM_CHAT_ID>"
# 挂 cron（health_cron.py）时由 cron 的 stdout 投递机制送达，本脚本不再重复直发。
def _under_cron_wrapper():
    """判断自己是不是被 health_cron.py（cron 包装器）拉起来的。

    两种判据任一命中即可：① 包装器显式设的 env；② 父进程命令行里就是 health_cron.py。
    ② 是为了「不依赖另一个文件是否设了 env」——两个文件可能被不同的人/会话先后改，
    这里自己认得更稳。
    """
    if os.environ.get("HEALTH_CRON_WRAPPER"):
        return True
    try:
        with open("/proc/%d/cmdline" % os.getppid(), "rb") as f:
            return "health_cron.py" in f.read().decode("utf-8", "replace")
    except Exception:
        return False


UNDER_CRON_WRAPPER = _under_cron_wrapper()

# ---- AstrBot 预期状态（退役待命的判据来源）--------------------------------
HERMES_ONEBOT_PORT = 6700      # Hermes 聊天门 OneBot 适配器（NapCat 现在的目标）
ASTRBOT_LEGACY_PORT = 6199     # AstrBot 反向 WS（退役前的目标；改回去 = AstrBot 在役）
NAPCAT_CFG_GLOB = "/opt/data/stack/napcat/config/onebot11_*.json"
# 平台类错误指纹：NapCat 指着 Hermes 时，AstrBot 发消息必然走不通 → 这类 ERROR 属预期
PLATFORM_ERR_PAT = re.compile(
    r"aiocqhttp|ApiNotAvailable|Failed to send the message chain|_dispatch_send"
    r"|aiocqhttp_message_event|respond\.stage|napcat|onebot", re.I)
# 错误块的起始行（与 LOGS 段 grep 的 ERROR|Traceback 同源，保持口径一致）
ERR_START_PAT = re.compile(r"\[ERRO\]|\[ERROR\]|^Traceback \(most recent call last\)")

# 期望的插件配置值（改这里 = 改判据）
EXPECT_DEBOUNCE_WAIT = 10.0
EXPECT_REPORT_PORT = 8098
EXPECT_LOOKUP_TOOLS = 5
EXPECT_PLUGINS = ["hermes_debounce", "hermes_memory", "hermes_memo", "hermes_report", "hermes_lookup", "hermes_delegate"]

results = []   # (id, name, status, detail)


def add(cid, name, status, detail):
    results.append({"id": cid, "name": name, "status": status, "detail": str(detail)})
    return status == "ok"


def sh(cmd, timeout=240):
    """本地（hermes 容器内）执行，返回 (rc, stdout, stderr)"""
    try:
        p = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True,
                           text=True, timeout=timeout, errors="replace")
        return p.returncode, p.stdout, p.stderr
    except Exception as e:
        return 255, "", "%s: %s" % (type(e).__name__, e)


def ssh(remote_cmd, timeout=300):
    """经宿主通道执行（容器内 -> SSH 棉棉 -> 宿主），返回 (rc, stdout, stderr)"""
    return sh(["bash", FYGO_SSH, remote_cmd], timeout=timeout)


def http(url, timeout=8):
    """无代理 GET，返回 (code, body)；连不上 -> (0, 错误文本)"""
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with op.open(url, timeout=timeout) as r:
            return r.status, r.read(4000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(2000).decode("utf-8", "replace")
    except Exception as e:
        return 0, "%s: %s" % (type(e).__name__, e)


# --------------------------------------------------------------------------- #
# 远程（宿主侧）一次拿全 AstrBot 现状：docker inspect + 容器内探针 + 官方补丁自检 + 日志指纹
# --------------------------------------------------------------------------- #

PROBE_PY = r'''
import json, os, re, sqlite3
D = "/AstrBot/data"
out = {}
def rd(p):
    try:
        return json.load(open(p, encoding="utf-8-sig"))
    except Exception as e:
        return {"__err__": str(e)}
try:
    out["plugin_dirs"] = sorted(os.listdir(D + "/plugins"))
except Exception as e:
    out["plugin_dirs"] = "ERR " + str(e)
for n in ("hermes_debounce", "hermes_memory", "hermes_memo", "hermes_report",
          "hermes_lookup", "hermes_delegate"):
    out["cfg_" + n] = rd("%s/config/%s_config.json" % (D, n))
out["mcp"] = rd(D + "/mcp_server.json")
c = rd(D + "/cmd_config.json")
out["providers"] = [{"id": p.get("id"), "enable": p.get("enable")} for p in (c.get("provider") or [])]
out["provider_settings_enable"] = (c.get("provider_settings") or {}).get("enable")
try:
    con = sqlite3.connect("file:" + D + "/data_v4.db?mode=ro", uri=True)
    out["personas"] = [{"id": pid, "tools_count": (len(json.loads(t)) if t else None)}
                       for pid, t in con.execute("select persona_id, tools from personas")]
except Exception as e:
    out["personas_err"] = str(e)
mp = D + "/mianmian-memo/MEMORY.md"
out["memo_exists"] = os.path.exists(mp)
out["memo_chars"] = os.path.getsize(mp) if out["memo_exists"] else 0
try:
    src = open(D + "/plugins/hermes_lookup/main.py", encoding="utf-8").read()
    out["lookup_tools"] = len(re.findall(r"@filter\.llm_tool", src))
except Exception as e:
    out["lookup_tools"] = "ERR " + str(e)
try:
    out["patch_log_tail"] = open(D + "/patches-boot.log", encoding="utf-8",
                                 errors="replace").read()[-160:].strip()
except Exception as e:
    out["patch_log_tail"] = "ERR " + str(e)
print("JSON_BEGIN")
print(json.dumps(out, ensure_ascii=False))
print("JSON_END")
'''

REMOTE_TMPL = """
export SUDO_ASKPASS=__SUDO__;
D="sudo -A docker";
S=$($D inspect astrbot --format '{{.State.StartedAt}}' 2>/dev/null);
echo '##STATE';
$D inspect astrbot --format '{{.State.Status}}|{{.State.StartedAt}}|{{.RestartCount}}|{{json .Config.Entrypoint}}';
echo '##MOUNTS';
$D inspect astrbot --format '{{range .Mounts}}{{.Source}}=>{{.Destination}} {{end}}';
echo '##PROBE';
$D exec astrbot python3 -c "import base64;exec(base64.b64decode('__B64__'))" 2>&1;
echo '##VERIFY';
VOUT=$($D exec astrbot python3 /opt/astrbot-patches/verify_patch.py 2>&1); RC=$?;
echo "$VOUT" | tail -2;
echo "VERIFY_RC=$RC";
echo '##LOGS';
$D logs --since "$S" astrbot 2>&1 | grep -aE 'MCP services initialization completed|适配器已连接|Loading model openai_chat_completion|Selected openai_chat_completion|ERROR|Traceback';
echo '##ERRSUM';
$D logs --since "$S" astrbot 2>&1 | python3 __HOSTROOT__/scripts/ab_log_classify.py 2>/dev/null || echo '{"__err__":"ab_log_classify.py 执行失败"}';
echo '##END';
"""

GREP_NOTE = ("MCP services initialization completed", "适配器已连接",
             "Loading model openai_chat_completion", "ERROR", "Traceback")

# ---- 预期状态：AstrBot 现在到底该不该连着 QQ（由 NapCat 配置显式推出）------

def collect_napcat_expected():
    """读 NapCat 的 websocketClients，推出 AstrBot 的预期状态。

    返回 {"ok": 配置读到了没, "retired": AstrBot 是否退役待命, "targets": [(name,url,enable)],
          "detail": 人话摘要}
    retired=True  ⇔ 有启用的 client 指向 Hermes 的 :6700，且没有指向 AstrBot 的 :6199。
    """
    nb = {"ok": False, "retired": None, "targets": [], "detail": ""}
    files = sorted(glob.glob(NAPCAT_CFG_GLOB))
    if not files:
        nb["detail"] = "找不到 %s" % NAPCAT_CFG_GLOB
        return nb
    urls = []
    for f in files:
        try:
            with open(f, encoding="utf-8-sig") as fh:
                cfg = json.load(fh)
        except Exception as e:
            nb["detail"] = "读 %s 失败: %s" % (os.path.basename(f), e)
            return nb
        for c in ((cfg.get("network") or {}).get("websocketClients") or []):
            if c.get("url"):
                urls.append((c.get("name") or "?", c["url"], bool(c.get("enable", True))))
    nb["targets"] = urls
    nb["ok"] = bool(urls)
    to_hermes = [u for _, u, en in urls if en and (":%d" % HERMES_ONEBOT_PORT) in u]
    to_astrbot = [u for _, u, en in urls if en and (":%d" % ASTRBOT_LEGACY_PORT) in u]
    nb["retired"] = bool(to_hermes) and not to_astrbot
    nb["detail"] = "；".join("%s -> %s%s" % (n, u, "" if en else "（停用）") for n, u, en in urls)
    return nb


def split_err_blocks(txt):
    """（已废弃）早期用 grep -A N 切块的做法——嵌套 traceback 会被切碎、误判成非平台类错误。
    现在统一走 `scripts/ab_log_classify.py`（按时间戳切「事件」+ 分级）。保留此函数只为兼容
    旧探针输出，健康自检不再调用。"""
    blocks, cur = [], []
    for line in txt.splitlines():
        if line.strip() == "--":
            if cur:
                blocks.append(cur)
            cur = []
            continue
        if ERR_START_PAT.search(line):
            if cur:
                blocks.append(cur)
            cur = [line]
        elif cur:
            cur.append(line)
    if cur:
        blocks.append(cur)
    return blocks


def collect_astrbot():
    b64 = base64.b64encode(PROBE_PY.encode("utf-8")).decode("ascii")
    cmd = (REMOTE_TMPL.replace("__SUDO__", HOST_SUDO)
           .replace("__B64__", b64)
           .replace("__HOSTROOT__", HOST_HERMES_ROOT))
    rc, out, err = ssh(cmd)
    sec, cur = {}, None
    for line in out.splitlines():
        if line.startswith("##") and line.strip() in ("##STATE", "##MOUNTS", "##PROBE", "##VERIFY", "##LOGS", "##ERRSUM", "##END"):
            cur = line.strip()[2:]
            sec[cur] = []
            continue
        if cur:
            sec[cur].append(line)
    info = {"_rc": rc, "_stderr": err.strip()[-300:]}

    state = (sec.get("STATE") or [""])[0].strip()
    parts = (state.split("|") + ["", "", "", ""])[:4]
    info["state"], info["started"], info["restarts"] = parts[0], parts[1], parts[2]
    try:
        info["entrypoint"] = json.loads(parts[3]) if parts[3] else []
    except Exception:
        info["entrypoint"] = parts[3]
    info["mounts"] = (sec.get("MOUNTS") or [""])[0].strip()

    probe_txt = "\n".join(sec.get("PROBE") or [])
    m = re.search(r"JSON_BEGIN\s*(\{.*?\})\s*JSON_END", probe_txt, re.S)
    if m:
        try:
            info["probe"] = json.loads(m.group(1))
        except Exception as e:
            info["probe"] = {"__err__": "parse: %s" % e}
    else:
        info["probe"] = {"__err__": probe_txt.strip()[-300:] or "no output"}

    vt = sec.get("VERIFY") or []
    info["verify_txt"] = " | ".join(x.strip() for x in vt if x.strip())
    vm = re.search(r"VERIFY_RC=(\d+)", info["verify_txt"])
    info["verify_rc"] = int(vm.group(1)) if vm else None

    logs = "\n".join(sec.get("LOGS") or [])
    esum_txt = " ".join(sec.get("ERRSUM") or []).strip()
    m = re.search(r"\{.*\}", esum_txt, re.S)
    try:
        esum = json.loads(m.group(0)) if m else {"__err__": esum_txt[-200:] or "无输出"}
    except Exception as e:
        esum = {"__err__": "解析失败 %s: %s" % (e, esum_txt[-160:])}
    info["errsum"] = esum
    info["log_lines"] = len([x for x in logs.splitlines() if x.strip()])
    info["mcp_ok"] = "1/1 successful, 0 failed" in logs
    # 「适配器已连接」是**启动时刻**的一行日志，NapCat 之后改指 :6700 它仍留在日志里
    # → 不能只看这一行（会把退役后的旧状态当成现在连着）。加上「之后是否出现 ApiNotAvailable」
    # 这条硬证据（AstrBot 发消息失败 = 现在确实没连）才判得准。
    info["adapter_connect_line"] = next(
        (x.strip() for x in logs.splitlines() if "适配器已连接" in x), "")
    info["adapter_api_unavail"] = (esum.get("apinotavail") or 0) > 0
    info["adapter_ok"] = bool(info["adapter_connect_line"]) and not info["adapter_api_unavail"]
    info["providers_loaded"] = sorted(set(re.findall(r"Loading model openai_chat_completion\(([^)]+)\)", logs)))
    info["err_count"] = len(re.findall(r"ERROR|Traceback", logs))
    info["err_sample"] = next((x.strip()[:200] for x in logs.splitlines() if re.search(r"ERROR|Traceback", x)), "")
    # 错误分类（平台类 / 非平台类，ERRO 级 / WARN 级）：退役待命期只拿「非平台类 ERRO 级」当故障
    info["err_events"] = esum.get("err_events")
    info["err_platform"] = esum.get("platform")
    info["err_other"] = esum.get("other")
    info["err_other_erro"] = esum.get("other_erro")
    info["err_other_warn"] = esum.get("other_warn")
    info["err_platform_sample"] = esum.get("platform_sample", "")
    info["err_other_samples"] = [s.get("line", "") for s in (esum.get("other_samples") or [])]
    return info


# --------------------------------------------------------------------------- #
# 各项检查
# --------------------------------------------------------------------------- #

def check_hermes_official(hermes_side):
    rc, out, err = sh([HERMES, "gateway", "list"], timeout=90)
    gates = dict((n, p) for n, p in re.findall(r"✓\s+(\S+).*?PID\s+(\d+)", out))
    for name, label in (("default", "gateway-default（干活门 8642）"),
                        ("chat", "gateway-chat（聊天门 8643）")):
        if name in gates:
            add("gw-" + name, label, "ok", "官方 `hermes gateway list`: ✓ %s PID %s" % (name, gates[name]))
        else:
            add("gw-" + name, label, "fail",
                "官方 `hermes gateway list` 未报 ✓ %s（rc=%d, %r）" % (name, rc, out.strip()[:200]))
    hermes_side["gateway_list"] = out.strip()

    rc, out, err = sh([HERMES, "doctor"], timeout=300)
    hermes_side["doctor"] = out
    if not out.strip():
        add("doctor", "hermes doctor（官方）", "fail", "无输出 rc=%d err=%s" % (rc, err.strip()[:200]))
        return
    m = re.search(r"Per-profile gateways:\s*(\d+)/(\d+) supervised up", out)
    if m and m.group(1) == m.group(2) and int(m.group(1)) >= 2:
        add("doctor-s6", "s6 托管（双侧网关）", "ok", "官方 doctor: Per-profile gateways %s/%s up" % (m.group(1), m.group(2)))
    elif m:
        add("doctor-s6", "s6 托管（双侧网关）", "fail", "官方 doctor: Per-profile gateways %s/%s up" % (m.group(1), m.group(2)))
    else:
        add("doctor-s6", "s6 托管（双侧网关）", "warn", "官方 doctor 输出里找不到 per-profile 行")
    for pat, cid, label in ((r"Memory Provider\s*\n?\s*✓ hindsight\w* provider active",
                             "doctor-mem", "Hindsight 记忆 provider（官方 doctor；hindsight / hindsight_guard / hindsight_flush 都算）"),
                            (r"main-hermes:\s*up", "doctor-main", "s6 main-hermes"),
                            (r"dashboard:\s*up", "doctor-dash", "s6 dashboard 进程")):
        add(cid, label, "ok" if re.search(pat, out) else "fail",
            "官方 doctor 命中" if re.search(pat, out) else "官方 doctor 未见对应 ✓ 行")


def check_endpoints():
    for cid, label, url, want in (
            ("ep-8642", "gateway-default api_server :8642", "http://127.0.0.1:8642/v1/models", (200, 401, 403)),
            ("ep-8643", "gateway-chat api_server :8643", "http://127.0.0.1:8643/v1/models", (200, 401, 403)),
            ("ep-8888", "Hindsight :8888 /health", "http://172.17.0.1:8888/health", (200,)),
    ):
        code, body = http(url)
        add(cid, label, "ok" if code in want else "fail",
            "HTTP %s（期望 %s）%s" % (code, "/".join(map(str, want)), "" if code in want else body[:120]))

    for cid, label, port in (("ep-9901", "A2A 干活门 :9901（agent card）", 9901),
                             ("ep-9900", "A2A 聊天门 :9900（agent card）", 9900)):
        code, body = http("http://127.0.0.1:%d/.well-known/agent-card.json" % port)
        m = re.search(r'"name"\s*:\s*"([^"]+)"', body)
        name = m.group(1) if m else ""
        add(cid, label, "ok" if (code == 200 and name) else "fail",
            "HTTP %s name=%s" % (code, name or body[:120]))

    code9119, _ = http("http://127.0.0.1:9119/")
    code19119, _ = http("http://127.0.0.1:19119/")
    code = code9119 if code9119 else code19119
    add("ep-dash", "dashboard（现网 9119 / 旧 19119）",
        "ok" if 200 <= code < 400 or code in (401, 403) else "fail",
        "9119 -> HTTP %s；19119 -> HTTP %s（host 网络后 dashboard 落在 9119，19119 是旧发布端口）" % (code9119, code19119))
    # :8098（AstrBot hermes_report 「主动回报」接收端）已随 AstrBot 退役整条废弃，不再作为检查项。


# --------------------------------------------------------------------------- #
# 家庭网络：旁路由（ImmortalWrt，与 NAS 同二层，静态 192.168.1.250）
# --------------------------------------------------------------------------- #

ROUTER_HOST = "192.168.1.250"
ROUTER_KEY = "/opt/data/keys/newifi_ed25519"


def router_probe(timeout=25):
    """只读探针：一次 SSH 拿全旁路由运行态。返回 (ok, kv 或 错误文本)。"""
    remote = ("echo ipv=$(uci -q get network.lan.ipaddr); "
              "echo ula=$(uci -q get network.globals.ula_prefix); "
              "echo ports=$(uci -q get network.@device[0].ports); "
              "echo dhcp=$(uci -q get dhcp.lan.ignore); "
              "echo pwauth=$(uci -q get dropbear.main.PasswordAuth); "
              "echo emptyroot=$(grep -c '^root::' /etc/shadow 2>/dev/null); "
              "echo nft=$(nft list ruleset 2>/dev/null | wc -l); "
              "echo netok=$(ping -c2 -W2 223.5.5.5 2>/dev/null | grep -c '0% packet loss'); "
              "echo adbe=$(uci -q get adblock-fast.config.enabled); "
              "echo adbc=$(grep -c . /var/run/adblock-fast/dnsmasq.servers 2>/dev/null || echo 0); "
              "echo adbt=$(nslookup doubleclick.net 127.0.0.1 2>/dev/null | grep -c NXDOMAIN); "
              "echo dnsok=$(nslookup www.baidu.com " + ROUTER_HOST + " 2>/dev/null | grep -c 'Address')")
    rc, out, err = sh(["ssh", "-i", ROUTER_KEY, "-o", "StrictHostKeyChecking=no",
                       "-o", "UserKnownHostsFile=/dev/null", "-o", "BatchMode=yes",
                       "-o", "ConnectTimeout=6", "root@" + ROUTER_HOST, remote],
                      timeout=timeout)
    if rc != 0 or "ipv=" not in out:
        return False, ((err or out).strip().replace("\n", " ")[:160] or ("rc=%s" % rc))
    return True, dict(re.findall(r"^(\w+)=(.*)$", out, re.M))


def check_home_network(probe=None):
    """旁路由运行态判据（只读）。每条都对应一次真实踩坑：

    - LAN 静态 192.168.1.250/24 —— 2026-10-02 由 .2 迁出。.2 上有另一台设备
      （天邑康和 CPE，MAC 90:52:bf:*）抢答 ARP 且只答不转发 = 黑洞，
      网关/DNS 指向 .2 的客户端成段全丢包（受控实证 10 轮：全丢）。
    - br-lan 只挂 lan1-4 —— DSA 下 eth0 是 CPU conduit、wan 是独立口，
      塞进网桥会断网（内置 overlay 那份旧配置就是这么写的，已修）。
    - dhcp.lan.ignore=1 —— 网里只能有光猫那一个 DHCP。
    - nftables 有规则 —— `/etc/init.d/firewall running` **不报真值**，一律看 nft 行数。
    """
    if probe is None:
        ok, p = router_probe()
        if not ok:
            add("rt-ssh", "旁路由 %s SSH（密钥登录）" % ROUTER_HOST, "warn",
                "连不上/认证失败：%s（关机属正常；若刚动过密钥，查 "
                "authorized_keys 与 dropbear 的 PasswordAuth）" % p)
            return
    else:
        p = probe

    ip = (p.get("ipv") or "").strip()
    if ip == ROUTER_HOST:
        add("rt-ip", "旁路由 LAN 地址", "ok",
            "ipaddr=%s ula_prefix=%s" % (ip, (p.get("ula") or "").strip()))
    else:
        add("rt-ip", "旁路由 LAN 地址", "fail",
            "ipaddr=%r，期望 %s —— 客户端是手动指向它的，地址漂移=随机失联；"
            "若漂回 .2 还会撞上那台黑洞设备" % (ip, ROUTER_HOST))

    ports = (p.get("ports") or "").split()
    badp = [x for x in ports if x in ("eth0", "wan")]
    if badp:
        add("rt-ports", "旁路由 br-lan 端口", "fail",
            "含 %s：DSA 下 eth0 是 CPU conduit、wan 是独立口，塞进网桥会断网（ports=%s）"
            % (",".join(badp), " ".join(ports)))
    else:
        add("rt-ports", "旁路由 br-lan 端口", "ok", "ports=%s" % " ".join(ports))

    dhcp = (p.get("dhcp") or "").strip()
    add("rt-dhcp", "旁路由不提供 DHCP", "ok" if dhcp == "1" else "fail",
        "dhcp.lan.ignore=%r（必须 1：网里只能有光猫那一个 DHCP）" % dhcp)

    try:
        nftn = int((p.get("nft") or "0").strip())
    except Exception:
        nftn = 0
    add("rt-nft", "旁路由 nftables 已加载", "ok" if nftn > 50 else "fail",
        "ruleset %d 行（init 脚本的 running 判据不可信，一律看行数）" % nftn)

    netok = (p.get("netok") or "").strip() == "1"
    add("rt-net", "旁路由外网连通（ping 223.5.5.5）", "ok" if netok else "warn",
        "经 .1 出网：%s" % ("通" if netok else "不通（LAN 内转发不受影响，值得看一眼）"))

    pwauth = (p.get("pwauth") or "").strip()
    emptyroot = (p.get("emptyroot") or "0").strip()
    if pwauth == "on" and emptyroot not in ("", "0"):
        add("rt-auth", "旁路由 SSH 凭证", "fail",
            "root 空密码 + PasswordAuth=on → 同网段任何设备都能以 root 登入"
            "（2026-10-02 已修一次的洞又开了；改回：PasswordAuth/RootPasswordAuth=off）")
    elif emptyroot not in ("", "0"):
        add("rt-auth", "旁路由 SSH 凭证", "ok",
            "SSH 仅认密钥 ✓（PasswordAuth=%r）；root 未设密码 → LuCI 网页后台任何密码都能进 "
            "—— 主人 2026-10-02 明示「就这样，不管」，故不再告警（要改：LuCI 的 "
            "System→Administration）" % pwauth)
    elif pwauth == "on":
        add("rt-auth", "旁路由 SSH 凭证", "warn",
            "PasswordAuth=on（root 已设密码 → 不算洞；密钥仍是主路径）")
    else:
        add("rt-auth", "旁路由 SSH 凭证", "ok",
            "仅密钥登录 + root 已设密码（PasswordAuth=%r）" % pwauth)

    dnso = (p.get("dnsok") or "").strip()
    add("rt-dns", "旁路由 dnsmasq 解析（NAS 的 IPv6 DNS 靠它）",
        "ok" if dnso not in ("", "0") else "warn",
        "nslookup www.baidu.com @%s：%s 条应答" % (ROUTER_HOST, dnso or "0"))

    # 广告过滤（adblock-fast，2026-10-02 装）：判据 = 开关 + 规则数 + 真拦一条
    adbe = (p.get("adbe") or "").strip()
    try:
        adbn = int((p.get("adbc") or "0").strip())
    except ValueError:
        adbn = 0
    adbt = (p.get("adbt") or "0").strip()
    if adbe != "1":
        add("rt-adb", "旁路由广告过滤（adblock-fast）", "ok",
            "未启用/未安装（enabled=%r）→ 不过滤属预期状态" % (adbe or "未安装"))
    elif adbn > 1000 and adbt not in ("", "0"):
        add("rt-adb", "旁路由广告过滤生效（%d 条规则）" % adbn, "ok",
            "实测 doubleclick.net → NXDOMAIN；只开一条列表（StevenBlack）。覆盖面："
            "只有把 DNS 交给这台路由器的客户端（它的 WiFi 设备）被过滤；走网线的设备"
            "直接问光猫，过滤不到——这也正是「旁路由挂了不影响上网」的原因")
    else:
        add("rt-adb", "旁路由广告过滤开了但没在过滤", "warn",
            "规则 %d 条 / 拦截测试 %r → 查 `/etc/init.d/adblock-fast status`、"
            "列表源可达性、/var/run/adblock-fast/dnsmasq.servers" % (adbn, adbt))


def check_napcat(nb):
    """把「AstrBot 预期状态」本身也作为一项检查打出来（判据可读、可复核）。"""
    if not nb.get("ok"):
        add("ab-retired", "AstrBot 预期状态（据 NapCat 通道目标判定）", "warn",
            "读不到 NapCat 配置 → 无法判定退役状态，回退严格判据：%s" % nb.get("detail"))
        return
    if nb.get("retired"):
        add("ab-retired", "AstrBot 预期状态 = 已退役/待命（NapCat 指向 Hermes :%d）" % HERMES_ONEBOT_PORT,
            "ok",
            "NapCat websocketClients：%s → AstrBot 插件/工具面/补丁/适配器/ERROR 检查全部跳过"
            "（不判失败、不报提醒）。回退旧通道 = 启动容器 + 把 url 改回 :%d 并重启 napcat"
            % (nb["detail"], ASTRBOT_LEGACY_PORT))
    else:
        add("ab-retired", "AstrBot 预期状态 = 在役（NapCat 回指 AstrBot :%d）" % ASTRBOT_LEGACY_PORT,
            "ok", "NapCat websocketClients：%s → 严格判据生效（适配器必须已连接；ERROR 按原阈值）"
            % nb["detail"])


def check_astrbot(a, nb=None):
    retired = bool((nb or {}).get("retired"))
    st = a.get("state", "")
    running = (st == "running")

    # ① 已退役 + 容器停着 = 预期（主人 2026-09-23 同意停掉旧 AstrBot 容器）：
    #    运行态类检查（插件参数/工具面/补丁/适配器/ERROR）一律**跳过**，不产生 warn 噪音。
    if not running and retired:
        add("ab-container", "AstrBot 已退役（容器已停）＝预期", "ok",
            "state=%s；NapCat 指向 Hermes :%d → 按已退役处理：插件参数/工具面/补丁/适配器/ERROR "
            "全部跳过（不判失败、不报提醒）。重新启动容器（`docker start astrbot`）后这些检查自动恢复；"
            "回退整条旧通道 = 启动容器 + 把 NapCat url 改回 :%d" % (
                st or "?", HERMES_ONEBOT_PORT, ASTRBOT_LEGACY_PORT))
        return
    if not running:
        # ② 在役（NapCat 还指着 AstrBot）却停着 = 真故障
        add("ab-container", "AstrBot 容器状态（在役却停着）", "fail",
            "state=%s；NapCat 仍指向 AstrBot :%d → 聊天门链路已断，需要人干预"
            "（`docker start astrbot`）" % (st or "?", ASTRBOT_LEGACY_PORT))
        add("ab-probe", "AstrBot 容器内探针", "fail", "容器不在跑（state=%s），无法探测" % (st or "?"))
        return

    # ③ 容器在跑（无论退役待命还是在役）→ 做完整检查
    add("ab-container", "AstrBot 容器状态", "ok",
        "state=%s Restarts=%s StartedAt=%s" % (st, a.get("restarts", "?"), a.get("started", "?")))
    ep = a.get("entrypoint") or []
    ep_ok = any("astrbot-entrypoint" in str(x) for x in (ep if isinstance(ep, list) else [ep]))
    add("ab-entrypoint", "AstrBot 自愈入口（补丁 entrypoint + patches 挂载）",
        "ok" if ep_ok and "/opt/astrbot-patches" in a.get("mounts", "") else "fail",
        "Entrypoint=%s；挂载含 /opt/astrbot-patches=%s" % (ep, "/opt/astrbot-patches" in a.get("mounts", "")))

    pr = a.get("probe") or {}
    if pr.get("__err__"):
        add("ab-probe", "AstrBot 容器内探针", "fail", "探针失败：%s" % pr["__err__"])
        return
    prov = pr.get("providers") or []
    prov_ok = len(prov) >= 2 and all(p.get("enable") for p in prov)
    add("ab-providers", "AstrBot 两个 provider",
        "ok" if prov_ok else "fail",
        "provider=%s；provider_settings.enable=%s" % (
            json.dumps(prov, ensure_ascii=False), pr.get("provider_settings_enable")))

    add("ab-mcp", "MCP hindsight 1/1（启动后日志）",
        "ok" if a.get("mcp_ok") else "fail",
        "MCP services 1/1 successful 命中=%s；mcp_server.json url=%s" % (
            a.get("mcp_ok"), json.dumps((pr.get("mcp") or {}).get("mcpServers", {}), ensure_ascii=False)[:160]))
    ad_ok = a.get("adapter_ok")
    if retired and not ad_ok:
        add("ab-adapter", "aiocqhttp 适配器（退役待命 → 预期不连）", "ok",
            "当前不可用**属预期**：NapCat websocketClients 指向 Hermes :%d；启动时那行「适配器已连接」是"
            "切换前的旧状态，之后出现 ApiNotAvailable（发消息失败）＝现在确实没连。证据：启动行=%s；"
            "有 ApiNotAvailable=%s" % (HERMES_ONEBOT_PORT, bool(a.get("adapter_connect_line")),
                                    a.get("adapter_api_unavail")))
    elif retired and ad_ok:
        add("ab-adapter", "aiocqhttp 适配器（退役待命 → 预期不连）", "warn",
            "当前仍连得上：退役待命期却显示可用（是不是有人把 NapCat 回指了 :%d？）" % ASTRBOT_LEGACY_PORT)
    else:
        add("ab-adapter", "aiocqhttp 适配器已连接（启动后日志）",
            "ok" if ad_ok else "fail", "命中=%s" % ad_ok)
    add("ab-providers-log", "provider 装载日志（启动后）",
        "ok" if len(a.get("providers_loaded") or []) >= 2 else "warn",
        "已装载：%s" % ", ".join(a.get("providers_loaded") or []) or "无")

    ec = a.get("err_count")
    if retired:
        # 退役待命：平台类（aiocqhttp 发不出消息）属预期；只有「非平台类 ERRO 级」才可能是真故障。
        # 非平台类 WARN 级 traceback（工具/agent 单次调用出错）只提醒、不算失败。
        esum = a.get("errsum") or {}
        if esum.get("__err__"):
            add("ab-errors", "AstrBot ERROR/Traceback（退役待命 → 错误分类器没跑起来）", "warn",
                "无法区分平台类/非平台类，按提醒处理：%s" % str(esum.get("__err__"))[:200])
        else:
            other_e = a.get("err_other_erro") or 0
            st_ = "ok" if other_e == 0 else ("warn" if other_e <= 2 else "fail")
            samples = "；".join((a.get("err_other_samples") or [])[:2])
            add("ab-errors", "AstrBot ERROR/Traceback（退役待命 → 只判非平台类 ERRO 级）", st_,
                "本次启动以来错误事件 %s 个＝平台类 %s（**预期**：NapCat 指向 Hermes :%d，"
                "AstrBot 发消息必然走不通）＋ 非平台类 ERRO 级 %s ＋ 非平台类 WARN 级 %s"
                "（WARN 级 traceback＝工具/agent 单次调用出错，只提醒不算失败）。非平台样本：%s" % (
                    a.get("err_events"), a.get("err_platform"), HERMES_ONEBOT_PORT,
                    other_e, a.get("err_other_warn") or 0, samples or "无"))
    else:
        add("ab-errors", "AstrBot ERROR/Traceback 计数（本次启动以来）",
            "ok" if ec == 0 else ("warn" if ec is not None and ec <= 3 else "fail"),
            "%s 条%s" % (ec, ("  例：" + a["err_sample"]) if a.get("err_sample") else ""))

    dirs = pr.get("plugin_dirs") or []
    missing = [p for p in EXPECT_PLUGINS if p not in dirs] if isinstance(dirs, list) else EXPECT_PLUGINS
    add("ab-plugins", "六个 hermes_* 插件都在加载目录",
        "ok" if not missing else "fail",
        "缺：%s；全部：%s" % (missing or "无", ", ".join(dirs) if isinstance(dirs, list) else dirs))

    d = pr.get("cfg_hermes_debounce") or {}
    w = d.get("wait_seconds")
    add("cfg-debounce", "hermes_debounce 防抖窗口 = 10.0s",
        "ok" if d.get("enabled") and float(w or 0) == EXPECT_DEBOUNCE_WAIT else "fail",
        "enabled=%s wait_seconds=%s max_wait=%s scope=%s（期望 %s，回 7 = 被别人覆盖）" % (
            d.get("enabled"), w, d.get("max_wait_seconds"), d.get("scope"), EXPECT_DEBOUNCE_WAIT))

    m = pr.get("cfg_hermes_memory") or {}
    add("cfg-memory", "hermes_memory retain + recall 双向开关",
        "ok" if m.get("retain_enable") and m.get("recall_enable") else "fail",
        "retain_enable=%s retention_mode=%s recall_enable=%s bank=%s recall_umos=%s" % (
            m.get("retain_enable"), m.get("retain_mode"), m.get("recall_enable"),
            m.get("bank_id"), m.get("recall_umos")))

    mo = pr.get("cfg_hermes_memo") or {}
    add("cfg-memo", "hermes_memo 常驻记忆块（注入开）",
        "ok" if mo.get("enable") and mo.get("inject_enable") else "fail",
        "enable=%s inject_enable=%s tools_enable=%s；MEMORY.md 存在=%s %s 字符" % (
            mo.get("enable"), mo.get("inject_enable"), mo.get("tools_enable"),
            pr.get("memo_exists"), pr.get("memo_chars")))

    r = pr.get("cfg_hermes_report") or {}
    rep_ok = (r.get("listen_port") == EXPECT_REPORT_PORT and r.get("inject_enable") is False
              and r.get("proactive_inject_enable") is True)
    add("cfg-report", "hermes_report 开关①关②开 + 监听 8098",
        "ok" if rep_ok else "fail",
        "listen=%s:%s inject_enable=%s proactive_inject_enable=%s" % (
            r.get("listen_host"), r.get("listen_port"), r.get("inject_enable"), r.get("proactive_inject_enable")))

    lk = pr.get("cfg_hermes_lookup") or {}
    n = pr.get("lookup_tools")
    add("cfg-lookup", "hermes_lookup 5 个查询工具 + 工具面闭集",
        "ok" if lk.get("enable") and lk.get("enforce_boundary") and n == EXPECT_LOOKUP_TOOLS else "fail",
        "enable=%s enforce_boundary=%s 注册到的 llm_tool=%s（期望 %s）" % (
            lk.get("enable"), lk.get("enforce_boundary"), n, EXPECT_LOOKUP_TOOLS))

    personas = pr.get("personas") or []
    pn = [p for p in personas if p.get("id") == "mianmian"]
    cnt = pn[0].get("tools_count") if pn else None
    add("cfg-persona", "persona 白名单（闭集，不能是 NULL）",
        "ok" if isinstance(cnt, int) and cnt >= 15 else "fail",
        "persona mianmian tools_count=%s（None = 全量工具 = 白名单失效）" % cnt)


def check_patches(a, nb=None):
    retired = bool((nb or {}).get("retired"))
    if a.get("state") != "running" and retired:
        add("patch-verify", "补丁在位（AstrBot 已退役 → 跳过）", "ok",
            "容器已停，不做补丁自检（官方 verify_patch.py 要容器在跑）；重启容器后自动恢复")
    else:
        rc = a.get("verify_rc")
        add("patch-verify", "补丁在位（官方 verify_patch.py，5 条 AST 判据）",
            "ok" if rc == 0 else "fail",
            "exit=%s；%s" % (rc, a.get("verify_txt", "")[:220]))

    bad, skipped = [], []
    # hermes_report / hermes_forward 是 compose 直接把 chat-layer/plugin/<name> 挂进容器
    # （同一文件，不存在两份副本），无需也无法比对 md5。
    BIND_MOUNTED = {"hermes_report", "hermes_forward"}
    for p in EXPECT_PLUGINS:
        s = os.path.join(PLUGIN_DIR_LOCAL, p, "main.py")
        l = os.path.join(PLUGIN_DIR_LIVE, p, "main.py")
        if p in BIND_MOUNTED and not os.path.exists(l):
            skipped.append("%s(compose 直接挂载=同一文件)" % p)
            continue
        if not (os.path.exists(s) and os.path.exists(l)):
            bad.append("%s(缺文件 src=%s live=%s)" % (p, os.path.exists(s), os.path.exists(l)))
            continue
        h1 = sh(["md5sum", s])[1].split()[:1]
        h2 = sh(["md5sum", l])[1].split()[:1]
        h1 = h1[0] if h1 else "?"
        h2 = h2[0] if h2 else "?"
        if h1 != h2:
            bad.append("%s(%s≠%s)" % (p, h1[:8], h2[:8]))
    add("patch-sync", "插件源码副本 = 加载副本（md5）",
        "ok" if not bad else "fail",
        "不一致：%s；跳过：%s" % (bad or "无", skipped or "无"))


# --------------------------------------------------------------------------- #
# llama.cpp 栈：参数漂移（compose 声明 vs 线上容器真实值）
# --------------------------------------------------------------------------- #

LLAMA_COMPOSE = "/opt/data/llamacpp/docker-compose.yml"
# 要逐项对比的两个服务：-c / -ngl（命令行）+ --cache-ram（命令行有无及值）+ mem_limit / restart
# restart 声明在 compose 的 x-common（`restart: unless-stopped`），两个服务都没覆盖它
LLAMA_SERVICES = ("llama-extract", "llama-embed")


def _compose_block(text, name):
    """从 compose 文本里抠出某个 service 的块（纯字符串/正则，环境无 pyyaml）。

    起点 = 行首两个空格的 `  <name>:`；终点 = 下一个同样缩进的服务键（或文件尾）。
    """
    m = re.search(r"^  %s:\s*$" % re.escape(name), text, re.M)
    if not m:
        return None
    rest = text[m.end():]
    m2 = re.search(r"^(?:  [^\s#]|\S)", rest, re.M)
    return rest[:m2.start()] if m2 else rest


def _compose_list_items(block):
    """把块的 YAML 列表项（`      - 值`）抠成字符串列表，去掉首尾引号。"""
    items = []
    for line in block.splitlines():
        m = re.match(r"^\s+-\s+(.*?)\s*$", line)
        if not m:
            continue
        v = m.group(1)
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        items.append(v)
    return items


def _arg_val(items, flag):
    """取命令行里紧跟 flag 的那个值（flag 不存在 / 是末尾开关 → None）。"""
    if not isinstance(items, list) or flag not in items:
        return None
    i = items.index(flag)
    return items[i + 1] if i + 1 < len(items) else None


def _mem_to_bytes(s):
    """`2583m` / `5g` / `1234567` → 字节数（docker mem_limit 是 1024 进制）。"""
    m = re.match(r"^(\d+)\s*([kmg]?)b?$", (s or "").strip().lower())
    if not m:
        return None
    return int(m.group(1)) * {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[m.group(2)]


def _fmt_mem(n):
    """字节数 → 人话（MiB）；不是整数就原样打印。"""
    return "%.0fMiB" % (n / 1024.0 / 1024.0) if isinstance(n, int) else str(n)


def collect_llama_live():
    """一次 SSH 拿两个容器的真实参数：CMD（命令行数组）/ Memory / RestartPolicy。

    返回 {"_rc":rc, "_stderr":..., "containers": {"llama-extract": {"cmd":[...],
          "mem":N, "restart":"..."}, ...}}
    """
    remote = (
        'export SUDO_ASKPASS=%s; D="sudo -A docker"; '
        'for c in %s; do '
        'echo "##$c"; '
        "$D inspect $c --format 'CMD={{json .Config.Cmd}}' 2>&1; "
        "$D inspect $c --format 'MEM={{.HostConfig.Memory}} RESTART={{.HostConfig.RestartPolicy.Name}}' 2>&1; "
        'done') % (HOST_SUDO, " ".join(LLAMA_SERVICES))
    rc, out, err = ssh(remote)
    info = {"_rc": rc, "_stderr": err.strip()[-200:], "containers": {}}
    cur = None
    for raw in out.splitlines():
        line = raw.strip()
        if line.startswith("##"):
            cur = line[2:]
            info["containers"].setdefault(cur, {})
            continue
        if cur is None:
            continue
        if line.startswith("CMD="):
            try:
                info["containers"][cur]["cmd"] = json.loads(line[4:])
            except Exception:
                info["containers"][cur]["cmd"] = None
        elif line.startswith("MEM="):
            m = re.search(r"MEM=(\d+)\s+RESTART=(\S+)", line)
            if m:
                info["containers"][cur]["mem"] = int(m.group(1))
                info["containers"][cur]["restart"] = m.group(2)
    return info


def check_llama_drift(live=None):
    """llama 栈参数漂移（compose vs 线上）——背景：2026-10-02 真实事故。

    这两个容器当日是手工 `docker run` 起的，`-c`/`--cache-ram`/`mem_limit`/`restart`
    全不在 compose 里；一 `docker compose up -d --force-recreate` 就会被重建回旧默认值，
    参数**静默丢失**（`--cache-ram` 没了 → 内存又会被大 batch 顶到 OOM；`-c` 打回旧值 →
    Hindsight 抽取报 HTTP 400）。当天已把终态固化进 llamacpp/docker-compose.yml。
    本检查逐项对比【线上容器真实参数】与【compose 声明】，不一致即报 ❌ 并写清
    哪一项、两边各是什么值；SSH 取不到值 → warn（不误报失败）。
    compose 用纯字符串/正则解析（环境无 pyyaml，`import yaml` 会 ModuleNotFoundError）。
    """
    CID, NAME = "llama-drift", "llama 栈参数漂移（compose vs 线上）"
    if live is None:
        live = collect_llama_live()
    try:
        with open(LLAMA_COMPOSE, encoding="utf-8") as f:
            text = f.read()
    except Exception as e:
        add(CID, NAME, "warn", "读不到 compose（%s）：%s" % (LLAMA_COMPOSE, e))
        return
    if live.get("_rc") != 0 or not live.get("containers"):
        add(CID, NAME, "warn",
            "SSH/宿主机取不到容器参数（rc=%s，%s）→ 无法对比，按提醒处理"
            % (live.get("_rc"), live.get("_stderr") or "无输出"))
        return

    mism, unk, summary = [], [], []
    mp = re.search(r"^  restart:\s*(\S+)", text, re.M)
    compose_restart = mp.group(1) if mp else None

    for svc in LLAMA_SERVICES:
        short = svc.replace("llama-", "")
        block = _compose_block(text, svc)
        lc = live["containers"].get(svc) or {}
        live_cmd = lc.get("cmd") if isinstance(lc.get("cmd"), list) else None
        if block is None:
            unk.append("%s: compose 里找不到服务块" % svc)
            continue
        items = _compose_list_items(block)

        # ① -c（上下文长度）
        cv, lv = _arg_val(items, "-c"), _arg_val(live_cmd, "-c")
        if cv is None or lv is None:
            unk.append("%s -c 取不到（compose=%r 线上=%r）" % (svc, cv, lv))
        elif cv != lv:
            mism.append("%s -c：线上 %s ≠ compose %s" % (svc, lv, cv))
        summary.append("%s -c=%s" % (short, lv if lv is not None else "?"))

        # ② -ngl（上 GPU 的层数）
        cv, lv = _arg_val(items, "-ngl"), _arg_val(live_cmd, "-ngl")
        if cv is None or lv is None:
            unk.append("%s -ngl 取不到（compose=%r 线上=%r）" % (svc, cv, lv))
        elif cv != lv:
            mism.append("%s -ngl：线上 %s ≠ compose %s" % (svc, lv, cv))
        summary.append("%s -ngl=%s" % (short, lv if lv is not None else "?"))

        # ③ --cache-ram（extract 必须有 1024；「有没有」「值」两边都比）
        c_has = "--cache-ram" in items
        l_has = isinstance(live_cmd, list) and "--cache-ram" in live_cmd
        c_ram, l_ram = _arg_val(items, "--cache-ram"), _arg_val(live_cmd, "--cache-ram")
        if c_has != l_has:
            mism.append("%s --cache-ram：%s 有、%s 没有"
                        % (svc, "compose" if c_has else "线上", "线上" if c_has else "compose"))
        elif c_has and c_ram != l_ram:
            mism.append("%s --cache-ram：线上 %s ≠ compose %s" % (svc, l_ram, c_ram))
        if l_has:
            summary.append("%s --cache-ram=%s" % (short, l_ram))

        # ④ mem_limit（系统内存上限，字节比对）
        mm = re.search(r"^\s+mem_limit:\s*(\S+)", block, re.M)
        c_mem = _mem_to_bytes(mm.group(1)) if mm else None
        l_mem = lc.get("mem")
        if c_mem is None or l_mem is None:
            unk.append("%s mem_limit 取不到（compose=%r 线上=%r）"
                       % (svc, mm.group(1) if mm else None, l_mem))
        elif c_mem != l_mem:
            mism.append("%s mem_limit：线上 %s ≠ compose %s"
                        % (svc, _fmt_mem(l_mem), _fmt_mem(c_mem)))
        summary.append("%s mem=%s" % (short, _fmt_mem(l_mem) if l_mem is not None else "?"))

        # ⑤ restart 策略
        l_restart = lc.get("restart")
        if compose_restart is None or l_restart is None:
            unk.append("%s restart 取不到（compose=%r 线上=%r）" % (svc, compose_restart, l_restart))
        elif l_restart != compose_restart:
            mism.append("%s restart：线上 %s ≠ compose %s" % (svc, l_restart, compose_restart))

    tail = "｜线上读数：" + "，".join(summary)
    if mism:
        add(CID, NAME, "fail", "❌ " + "；".join(mism) + tail)
    elif unk:
        add(CID, NAME, "warn", "⚠️ 取不到值：" + "；".join(unk) + tail)
    else:
        add(CID, NAME, "ok",
            "全部一致：" + "，".join(summary) + "；restart=%s（两个容器均与 compose 声明一致）"
            % compose_restart)


# --------------------------------------------------------------------------- #
# 告警
# --------------------------------------------------------------------------- #

# ---- 告警去重（同一异常未恢复前只报一次）-----------------------------------
# 为什么要它：2026-09-23 出过一次「每两分钟刷同一条失败」——
#   同一批异常被反复检出，每次调用都直发一次告警（那次是 :8098 回执被当入站消息
#   喂回聊天门），对收件人就是刷屏。判据本身没错，错在**每条都发**。
# 规则：以「异常项 id 集合」为签名；同一签名在冷却期内（默认 4h）不重复发，
#       恢复（overall=ok）即清零；--simulate-fail 演练不受冷却限制。
ALERT_STATE = os.environ.get("HEALTH_ALERT_STATE") or "/opt/data/logs/health-alert-state.json"
ALERT_COOLDOWN = float(os.environ.get("HEALTH_ALERT_COOLDOWN_S") or 4 * 3600)


def alert_signature(bad):
    """告警签名 = 当前异常项的 id 集合（不含会变的读数，同类异常才算同一条）。"""
    return "|".join(sorted(str(r.get("id") or "") for r in bad))


def alert_dedup_check(sig):
    """返回 (是否该发, 人话说明)。state 里记着上一次发过的签名与时间。"""
    try:
        with open(ALERT_STATE, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        return True, "首次告警（无历史状态）"
    if not isinstance(st, dict) or st.get("sig") != sig:
        return True, "异常项变了（上次 %s）→ 该报" % (st.get("sig") or "无")
    try:
        age = datetime.now().timestamp() - float(st.get("ts") or 0)
    except Exception:
        age = 0.0
    if ALERT_COOLDOWN > 0 and age < ALERT_COOLDOWN:
        return False, ("同一异常未恢复（上次 %s 已报过，%.0f 分钟前）→ 本轮不重复告警；"
                       "恢复即清零，仍要重发设 HEALTH_ALERT_COOLDOWN_S=0"
                       % (st.get("first") or "?", age / 60.0))
    return True, "同一异常但已过冷却期（%.1fh）→ 该报" % (age / 3600.0)


def alert_dedup_mark(sig, summary):
    """记下"这条已经报过"。first=第一次报的时刻（冷却按它算，反复报不会无限顺延）。"""
    st = {}
    try:
        with open(ALERT_STATE, encoding="utf-8") as f:
            old = json.load(f)
        if isinstance(old, dict) and old.get("sig") == sig:
            st = old
    except Exception:
        pass
    st.setdefault("first", st.get("ts") or datetime.now().isoformat(timespec="seconds"))
    st.update({"sig": sig, "ts": datetime.now().timestamp(), "summary": summary[:200],
               "cooldown_s": ALERT_COOLDOWN})
    try:
        os.makedirs(os.path.dirname(ALERT_STATE), exist_ok=True)
        with open(ALERT_STATE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
    except Exception:
        pass
    return st


def alert_dedup_clear():
    """恢复正常 → 清掉去重状态（下次出问题一定报）。"""
    try:
        os.remove(ALERT_STATE)
    except Exception:
        pass


def send_primary_alert(title, summary, status):
    """兜底告警通道：**Hermes 自己**（主通道已改 magicpush，见 send_magicpush） —— 官方 `hermes send` 直投主人 QQ 私聊。

    不碰 AstrBot、不碰 :8098（那个通道要靠 AstrBot 平台才能发消息，AstrBot 退役后必然失败，
    2026-09-23 已整条删除）。
    挂 cron 时本函数不会被调用：改由 health_cron.py 的 stdout 经 Hermes cron 投递机制送达。
    """
    text = "%s\n%s" % (title, summary)
    rc, out, err = sh([HERMES, "send", "-t", ALERT_TARGET, "--json", text], timeout=90)
    raw = " ".join((out or err or "").split())
    return rc == 0, "hermes send rc=%d -> %s；%s" % (rc, ALERT_TARGET, raw[-300:])


def send_magicpush(title, summary):
    """magicpush 通道（容器 :818，应用商场托管）—— 主人 2026-10-02 定：「包括任何警告…调用魔法推送」。

    token 从 /opt/data/secrets/magicpush.token 读（脚本内不落明文）；
    必须显式设 NO_PROXY，否则容器里的代理会把 172.17.0.1 也拦进代理。
    """
    import json as _json, os as _os, urllib.request as _u
    _os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost,172.17.0.1")
    try:
        tok = open("/opt/data/secrets/magicpush.token").read().strip()
    except Exception as e:
        return False, "读不到 token：%s" % str(e)[:80]
    if not tok:
        return False, "token 为空"
    body = _json.dumps({"title": title, "content": summary, "type": "markdown"}).encode()
    req = _u.Request("http://172.17.0.1:818/api/push/%s" % tok, data=body,
                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        r = _u.urlopen(req, timeout=30)
        return 200 <= r.status < 300, "magicpush HTTP %s -> %s" % (r.status, r.read().decode()[:160])
    except Exception as e:
        return False, "magicpush 失败：%s" % str(e)[:120]

# --------------------------------------------------------------------------- #

def selftest(a=None, nb=None):
    """灵敏度自检：把（合成的）基线读数逐项改坏，检查器**必须**报 fail；
    分层判据的「预期」项**必须既不 fail 也不 warn**（证明既不是橡皮图章，也不是一刀切放水）。

    用合成基线而不是线上读数：AstrBot 容器已停时探针本来就取不到读数（探测到 running 才查），
    自检要验的是**检查逻辑**，不该受线上开关影响。同时先跑一遍未改坏的基线，确认它全静默。
    """
    import copy
    global results          # add() 往模块级 results 里追加；自检要临时换掉它再还原
    RETIRED = {"ok": True, "retired": True, "targets": [("hermes_onebot", "ws://127.0.0.1:6700/ws", True)],
               "detail": "selftest 构造：已退役/待命"}
    ACTIVE = {"ok": True, "retired": False, "targets": [("astrbot", "ws://127.0.0.1:6199/ws", True)],
              "detail": "selftest 构造：AstrBot 在役"}
    BASE = {
        "state": "running", "restarts": "0", "started": "selftest",
        "entrypoint": ["/bin/bash", "/opt/astrbot-patches/astrbot-entrypoint.sh"],
        "mounts": "s=>/opt/astrbot-patches ",
        "mcp_ok": True, "adapter_ok": False, "adapter_connect_line": "selftest 启动行",
        "adapter_api_unavail": True, "providers_loaded": ["a", "b"],
        "err_count": 0, "err_events": 0, "err_platform": 0, "err_other_erro": 0,
        "err_other_warn": 0, "errsum": {}, "err_sample": "",
        "verify_rc": 0, "verify_txt": "[VERIFY] 补丁已生效 ✓",
        "probe": {
            "plugin_dirs": list(EXPECT_PLUGINS),
            "providers": [{"id": "deepseek/deepseek-flash", "enable": True},
                          {"id": "local/bonsai2-27b", "enable": True}],
            "provider_settings_enable": True,
            "cfg_hermes_debounce": {"enabled": True, "wait_seconds": 10.0,
                                    "max_wait_seconds": 45, "scope": "private"},
            "cfg_hermes_memory": {"retain_enable": True, "recall_enable": True,
                                  "retain_mode": "both", "bank_id": "mianmian-history"},
            "cfg_hermes_memo": {"enable": True, "inject_enable": True, "tools_enable": True},
            "cfg_hermes_report": {"listen_port": 8098, "inject_enable": False,
                                  "proactive_inject_enable": True},
            "cfg_hermes_lookup": {"enable": True, "enforce_boundary": True},
            "lookup_tools": EXPECT_LOOKUP_TOOLS,
            "personas": [{"id": "mianmian", "tools_count": 22}],
            "memo_exists": True, "memo_chars": 1126,
        },
    }
    print("【灵敏度自检】合成基线逐项改坏 → 检查器是否都按预期反应")
    # (名称, 改坏函数, 期望, 用哪套 NapCat 预期状态, 容器状态)
    muts = [
        ("debounce wait=7（被人覆盖回旧值）",
         lambda x: x["probe"]["cfg_hermes_debounce"].update({"wait_seconds": 7}), "fail", RETIRED, "running"),
        ("hermes_memory recall 关掉",
         lambda x: x["probe"]["cfg_hermes_memory"].update({"recall_enable": False}), "fail", RETIRED, "running"),
        ("hermes_memo 注入关掉",
         lambda x: x["probe"]["cfg_hermes_memo"].update({"inject_enable": False}), "fail", RETIRED, "running"),
        ("hermes_report 监听端口改成 9999",
         lambda x: x["probe"]["cfg_hermes_report"].update({"listen_port": 9999}), "fail", RETIRED, "running"),
        ("hermes_lookup 工具掉到 3 个",
         lambda x: x["probe"].update({"lookup_tools": 3}), "fail", RETIRED, "running"),
        ("persona 白名单变 NULL（全量工具）",
         lambda x: x["probe"].update({"personas": [{"id": "mianmian", "tools_count": None}]}),
         "fail", RETIRED, "running"),
        ("MCP hindsight 掉成 0/1", lambda x: x.update({"mcp_ok": False}), "fail", RETIRED, "running"),
        ("补丁失位（verify exit 1）", lambda x: x.update({"verify_rc": 1}), "fail", RETIRED, "running"),
        ("出现 5 个非平台类 ERRO 级错误",
         lambda x: x.update({"err_events": 5, "err_platform": 0, "err_other_erro": 5,
                             "err_other_warn": 0, "errsum": {}}), "fail", RETIRED, "running"),
        # ↓ 分层判据的反向验证：这些必须是「预期」，不能报 fail、也不能报 warn
        ("退役待命：适配器不可用 → 应判「预期」（不 fail、不 warn）",
         lambda x: x.update({"adapter_ok": False}), "silent", RETIRED, "running"),
        ("退役待命：4 个平台类 ERROR → 应判「预期」",
         lambda x: x.update({"err_events": 4, "err_platform": 4, "err_other_erro": 0,
                             "err_other_warn": 0, "errsum": {}}), "silent", RETIRED, "running"),
        ("退役待命：只有 WARN 级非平台 traceback → 只记日志不出声",
         lambda x: x.update({"err_events": 1, "err_platform": 0, "err_other_erro": 0,
                             "err_other_warn": 1, "errsum": {}}), "silent", RETIRED, "running"),
        ("已退役：容器停着 → 预期（运行态检查全跳过）",
         lambda x: x.update({"state": "exited"}), "silent", RETIRED, "exited"),
        ("退役待命：适配器竟显示可用（有人回指 :6199？）→ 只报提醒、不判 fail",
         lambda x: x.update({"adapter_ok": True, "adapter_api_unavail": False}), "warn", RETIRED, "running"),
        # ↓ AstrBot 在役（NapCat 回指 :6199）时，严格判据必须自动生效
        ("在役：适配器掉线 → 必须报 fail",
         lambda x: x.update({"adapter_ok": False}), "fail", ACTIVE, "running"),
        ("在役：5 条平台类 ERROR → 仍算故障（严格判据）",
         lambda x: x.update({"err_count": 5}), "fail", ACTIVE, "running"),
        ("在役：容器停着 → 必须报 fail",
         lambda x: x.update({"state": "exited"}), "fail", ACTIVE, "exited"),
    ]
    bad_ok = []
    # 先验基线：未改坏时必须全静默（否则下面的「改坏后报警」没有意义）
    saved, results = results, []
    try:
        check_astrbot(copy.deepcopy(BASE), RETIRED)
        check_patches(copy.deepcopy(BASE), RETIRED)
        base_bad = [(r["id"], r["status"]) for r in results if r["status"] != "ok"]
    finally:
        results = saved
    if base_bad:
        bad_ok.append("基线不静默 -> %s" % base_bad)
        print("  ✗ 合成基线（未改坏）就不静默：%s" % base_bad)
    else:
        print("  ✓ 合成基线（未改坏）全静默 ✅")

    for name, f, expect, nbv, state in muts:
        x = copy.deepcopy(BASE)
        x["state"] = state              # 真实容器现在已被停掉；这里显式给每例设定容器状态
        f(x)
        saved, results = results, []
        try:
            check_astrbot(x, nbv)
            check_patches(x, nbv)
            got = {r["id"]: r["status"] for r in results}
            fails = [k for k, v in got.items() if v == "fail"]
            warns = [k for k, v in got.items() if v == "warn"]
        finally:
            results = saved
        if expect == "fail":
            if fails:
                print("  ✓ 改坏「%s」→ 报 fail（%s）" % (name, ",".join(fails)))
            else:
                bad_ok.append("%s -> 期望 fail，实际 %s" % (name, got))
                print("  ✗ 改坏「%s」→ 检查器没报 fail（橡皮图章！）" % name)
        elif expect == "warn":
            if warns and not fails:
                print("  ✓ 「%s」→ 只报提醒不判 fail（%s）" % (name, ",".join(warns)))
            else:
                bad_ok.append("%s -> 期望仅 warn，实际 %s" % (name, got))
                print("  ✗ 「%s」→ 期望仅 warn，实际 %s" % (name, got))
        else:
            if not fails and not warns:
                print("  ✓ 「%s」→ 既无 fail 也无 warn（符合预期）" % name)
            else:
                bad_ok.append("%s -> 期望全静默，实际 %s" % (name, got))
                print("  ✗ 「%s」→ 有噪音（%s）" % (name, ",".join(fails + warns)))
    # ---- 旁路由判据（合成读数，不碰线上）-------------------------------------
    GOOD = {"ipv": "192.168.1.250", "ula": "fd80:d338:9d87::/48",
            "ports": "lan1 lan2 lan3 lan4", "dhcp": "1", "nft": "217",
            "netok": "1", "dnsok": "3", "pwauth": "off", "emptyroot": "0",
            "adbe": "1", "adbc": "41588", "adbt": "1"}
    router_cases = [
        ("基线（未改坏）→ 应全静默", dict(GOOD), "silent"),
        ("地址漂回 .2（旧黑洞冲突地址）", dict(GOOD, ipv="192.168.1.2"), "fail"),
        ("br-lan 被塞回 eth0/wan", dict(GOOD, ports="eth0 lan1 lan2 lan3 lan4 wan"), "fail"),
        ("旁路由自己开了 DHCP", dict(GOOD, dhcp="0"), "fail"),
        ("nftables 没加载（0 行）", dict(GOOD, nft="0"), "fail"),
        ("外网不通（LAN 内仍可用）", dict(GOOD, netok="0"), "warn"),
        ("空密码 + PasswordAuth=on（大洞又开了）",
         dict(GOOD, pwauth="on", emptyroot="1"), "fail"),
        ("root 未设密码（主人 2026-10-02 明示「不管」→ 不再告警）",
         dict(GOOD, emptyroot="1"), "silent"),
        ("PasswordAuth=on 但已设密码（仅提醒）", dict(GOOD, pwauth="on"), "warn"),
        ("广告过滤开着但规则为空（列表没加载）", dict(GOOD, adbc="0"), "warn"),
        ("广告过滤开着但域名没被拦（服务没生效）", dict(GOOD, adbt="0"), "warn"),
        ("广告过滤未启用（不过滤属预期，不告警）",
         dict(GOOD, adbe="0", adbc="0", adbt="0"), "silent"),
    ]
    for rname, rprobe, rexpect in router_cases:
        saved, results = results, []
        try:
            check_home_network(probe=rprobe)
            rgot = {r["id"]: r["status"] for r in results}
            rfails = [k for k, v in rgot.items() if v == "fail"]
            rwarns = [k for k, v in rgot.items() if v == "warn"]
        finally:
            results = saved
        if ((rexpect == "fail" and rfails)
                or (rexpect == "warn" and rwarns and not rfails)
                or (rexpect == "silent" and not rfails and not rwarns)):
            print("  ✓ 旁路由「%s」→ %s ✅" % (rname, rexpect))
        else:
            bad_ok.append("router: %s -> 期望 %s，实际 %s" % (rname, rexpect, rgot))
            print("  ✗ 旁路由「%s」→ 期望 %s，实际 %s" % (rname, rexpect, rgot))

    ncase = len(muts) + len(router_cases)
    if bad_ok:
        print("灵敏度自检：失败 %d 项" % len(bad_ok))
        return 1
    print("灵敏度自检：%d/%d 项全部按预期反应 ✅" % (ncase, ncase))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-alert", action="store_true")
    ap.add_argument("--selftest", action="store_true", help="灵敏度自检：把读数改坏，验证检查器会报警（不动线上）")
    ap.add_argument("--simulate-fail", action="store_true", help="演练告警通道（会真发一条标注【自检】的告警）")
    args = ap.parse_args()

    hermes_side = {}
    check_hermes_official(hermes_side)
    check_endpoints()
    check_home_network()        # 旁路由（ImmortalWrt，与 NAS 同二层）运行态，只读
    nb = collect_napcat_expected()
    check_napcat(nb)
    a = collect_astrbot()
    check_astrbot(a, nb)
    check_patches(a, nb)
    check_llama_drift()         # llama.cpp 栈：compose 声明 vs 线上容器真实参数（防重建打回旧值）

    if args.selftest:
        return selftest(a, nb)

    if args.simulate_fail:
        add("sim", "告警通道演练（人为置失败）", "fail", "由 --simulate-fail 注入，不是真实故障")

    fails = [r for r in results if r["status"] == "fail"]
    warns = [r for r in results if r["status"] == "warn"]
    overall = "fail" if fails else ("warn" if warns else "ok")

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    alert_note = ""
    if overall == "ok":
        alert_dedup_clear()          # 恢复 → 清零，下次出问题一定报
    elif not args.no_alert:
        bad = fails or warns
        head = "、".join("%s" % r["name"] for r in bad[:4])
        summary = "；".join("[%s] %s" % (r["status"], r["id"]) for r in bad)[:300]
        title = ("【自检】健康自检演练：人为置失败（非真实故障）" if args.simulate_fail
                 else "【健康自检】%d 项异常：%s" % (len(fails) or len(warns), head))
        status = "fail" if fails else "partial"
        # 唯一告警通道：Hermes 自己（挂 cron 时由 cron 的 stdout 投递机制负责，这里不重复直发）
        if UNDER_CRON_WRAPPER:
            alert_note = "主通道＝Hermes cron 投递（本脚本 stdout 交给 cron，直投 %s）" % ALERT_TARGET
        else:
            # 去重闸门：同一签名未恢复前只报一次（演练除外，演练要看通道通不通）
            sig = alert_signature(bad)
            if args.simulate_fail:
                should_send, why = True, "演练（不去重）"
            else:
                should_send, why = alert_dedup_check(sig)
            if should_send:
                # 2026-10-02：主通道改 magicpush（主人指定），Hermes 留作兜底
                ok_mp, note_mp = send_magicpush(title, summary)
                if ok_mp:
                    ok, note = True, "主通道=magicpush（%s）" % note_mp
                else:
                    ok2, note2 = send_primary_alert(title, summary, status)
                    ok, note = ok2, "magicpush 未成（%s）→ 回落 Hermes：%s" % (note_mp, note2)
                if ok and not args.simulate_fail:
                    st = alert_dedup_mark(sig, summary)
                    why = "%s；已记录去重状态（首次 %s）" % (why, st.get("first"))
                alert_note = "%s %s｜去重：%s" % (
                    "✅ 主通道 hermes send：" if ok else "❌ 主通道 hermes send 失败：", note, why)
            else:
                alert_note = "🔇 已去重、本轮不打扰：%s（签名 %s）" % (why, sig)

    if args.json:
        print(json.dumps({"time": ts, "overall": overall, "results": results,
                          "alert": alert_note, "napcat": nb,
                          "alert_target": ALERT_TARGET,
                          "doctor": hermes_side.get("doctor", ""),
                          "gateway_list": hermes_side.get("gateway_list", ""),
                          "astrbot": {k: v for k, v in a.items() if k != "log_lines"}},
                         ensure_ascii=False, indent=2))
        return 0 if overall == "ok" else 1

    icon = {"ok": "✅", "warn": "⚠️", "fail": "❌"}
    head = "全部正常（%d/%d 项通过）" % (len([r for r in results if r["status"] == "ok"]), len(results))
    if overall == "fail":
        head = "异常 %d 项：%s" % (len(fails), "、".join(r["name"] for r in fails))
    elif overall == "warn":
        head = "有 %d 项提醒：%s" % (len(warns), "、".join(r["name"] for r in warns))
    print("%s 健康总览：%s｜%s%s" % (icon[overall], head, ts, ("｜" + alert_note) if alert_note else ""))
    print("-" * 72)
    for r in results:
        print("%s %-38s %s" % (icon[r["status"]], r["name"], r["detail"]))
    print("-" * 72)
    print("（Hermes 侧状态与 s6 由官方 `hermes gateway list` / `hermes doctor` 判定；补丁由官方 "
          "verify_patch.py 判定；本脚本只补官方没覆盖的端点探针与 AstrBot 运行态）")
    print("（告警去向：**主通道 magicpush :818**，失败回落 Hermes 自己（cron 投递 / `hermes send` → %s）。"
          "AstrBot 的 :8098/report 已随 AstrBot 退役整条废弃）" % ALERT_TARGET)
    return 0 if overall == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
