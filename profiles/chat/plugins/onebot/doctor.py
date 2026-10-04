#!/usr/bin/env python3
"""hermes_onebot doctor —— 一条命令跑完所有检查，输出「正常 / 异常 + 具体哪一项」。

**只读**：不启停任何服务、不改任何配置、不发消息、不联网写。

检查项：
  1. 源码真身（chat-layer）与已部署副本是否一致（md5）
  2. 插件是否已部署到 profile 的 plugins 目录；plugins.enabled / platforms.onebot.enabled
  3. 心跳状态文件（存在性 / 新鲜度 / 连接 / 收发计数 / 连续失败）
  4. 反向 WS 监听端口是否有人监听
  5. 告警通道（hermes_report 8098）是否活着

退出码：0 = 正常；1 = 异常（也用于 CI/巡检）。

用法：
  python3 doctor.py                                  # 默认 profile=chat
  python3 doctor.py --profile chat
  python3 doctor.py --root /vol1/1000/<USER>   # 宿主路径视图
  python3 doctor.py --json                           # 机器可读
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from health import FAIL, OK, WARN, judge, summarize, thresholds_from_extra  # noqa: E402

PLUGIN_FILES = ("plugin.yaml", "__init__.py", "adapter.py", "segmentation.py", "group_window.py",
                "group_wake.py",
                "debounce.py", "onebot_proto.py", "health.py", "doctor.py")


def _md5(path: Path) -> str:
    h = hashlib.md5()
    h.update(path.read_bytes())
    return h.hexdigest()


def _truthy(value: Any, default: bool = False) -> bool:
    """与适配器同款宽松布尔解析（doctor 独立运行，不 import 适配器）。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _finding(key: str, level: str, detail: str) -> Dict[str, str]:
    return {"key": key, "level": level, "detail": detail}


def check_copy(here: Path, deployed: Path) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    if not deployed.is_dir():
        return [_finding("deployed", FAIL, f"未部署到 {deployed}（跑 deploy.sh）")]
    out.append(_finding("deployed", OK, f"已部署到 {deployed}"))
    drift = []
    for name in PLUGIN_FILES:
        src, dst = here / name, deployed / name
        if not src.is_file() or not dst.is_file():
            drift.append(f"{name}(缺失)")
        elif _md5(src) != _md5(dst):
            drift.append(name)
    if drift:
        out.append(_finding("copy_sync", WARN,
                            f"源码真身与部署副本不一致：{', '.join(drift)} —— 跑 deploy.sh 同步"))
    else:
        out.append(_finding("copy_sync", OK, "源码真身与部署副本一致"))
    return out


class YamlUnavailable(RuntimeError):
    """系统 python3 常常没有 pyyaml —— 静默吞掉会让「配置项」被误判成「未启用」。"""


def _read_yaml(path: Path) -> Dict[str, Any]:
    """读 profile 的 config.yaml。缺 pyyaml 时**必须报出来**，不能返回 {} 假装配置是空的。

    症状：用系统 python3（无 yaml）跑 doctor → `plugins.enabled 不含 onebot` 之类假告警，
    让人以为没启用。用到 yaml 的地方一律用 `/opt/hermes/.venv/bin/python doctor.py`。
    """
    try:
        import yaml  # Hermes 自带（在 /opt/hermes/.venv 里）
    except ImportError as e:
        raise YamlUnavailable(
            "缺少 pyyaml 模块，无法读配置 → 请用 Hermes 自带的解释器运行："
            "`/opt/hermes/.venv/bin/python doctor.py`"
        ) from e
    try:
        with path.open(encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except Exception as e:
        raise YamlUnavailable(f"{path} 解析失败：{e}") from e


def check_config(root: Path, profile: str) -> tuple[List[Dict[str, str]], bool]:
    """返回 (findings, 是否「预期在跑」)。后者决定「没有状态文件」是故障还是正常。"""
    cfg_path = root / "profiles" / profile / "config.yaml"
    if not cfg_path.is_file():
        return [_finding("config", WARN, f"找不到 {cfg_path}")], False
    try:
        cfg = _read_yaml(cfg_path)
    except YamlUnavailable as e:
        return [_finding("config", FAIL, str(e))], False
    if not isinstance(cfg, dict):
        return [_finding("config", WARN, f"{cfg_path} 解析不出字典")], False

    enabled_plugins = ((cfg.get("plugins") or {}).get("enabled") or [])
    onebot_platform = (cfg.get("platforms") or {}).get("onebot") or {}
    plat_enabled = bool(onebot_platform.get("enabled"))
    plugin_enabled = "onebot" in enabled_plugins
    expected_running = bool(plugin_enabled and plat_enabled)

    out: List[Dict[str, str]] = []
    if plugin_enabled:
        out.append(_finding("plugin_enabled", OK, "plugins.enabled 含 onebot"))
    else:
        out.append(_finding("plugin_enabled", WARN,
                            "plugins.enabled 不含 onebot → 适配器不会被加载"
                            "（当前为「未启用」预期状态，要启用需主人拍板）"))
    if plat_enabled:
        out.append(_finding("platform_enabled", OK, "platforms.onebot.enabled = true"))
    else:
        out.append(_finding("platform_enabled", WARN,
                            "platforms.onebot.enabled 未开 → 不会起监听（当前为「未启用」预期状态）"))
    out.append(_finding("read_only", (WARN if onebot_platform.get("extra", {}).get("read_only", True)
                                      else OK),
                        f"read_only={onebot_platform.get('extra', {}).get('read_only', True)}（默认只收不发）"))

    # 群聊：把**真值**打出来（2026-09-23 修过一次「RUNBOOK 写 false / config 是 true」的漂移，
    # 从此让 doctor 当唯一真值来源，文档与它对齐）。
    extra = onebot_platform.get("extra") or {}
    g_enabled = bool(extra.get("group_enabled"))
    g_mode = str(extra.get("group_wake_mode") or "").strip().lower()
    if not g_mode:
        # 老键语义映射（与 group_wake.parse_mode 同源；这里不 import 适配器，doctor 要能独立跑）
        g_mode = "full" if _truthy(extra.get("group_wake_enabled")) else "collect-only"
    g_wake = g_mode != "collect-only"
    dm_policy = str(extra.get("dm_policy") or "allowlist")
    n_dm_allow = len(extra.get("allow_from") or [])
    guard_req = _truthy(extra.get("group_memory_guard_required"), True)
    profile_dir = root / "profiles" / profile
    iso = memory_isolation(profile_dir) if g_wake else None
    if g_enabled and g_wake and guard_req and iso and not iso.get("ok"):
        # C1 的硬前置：闸门没就位 → 适配器会**拒绝唤醒**（消息进窗口但她不说话）
        out.append(_finding(
            "group_memory_guard", FAIL,
            f"⚠️ 群唤醒模式={g_mode} 但记忆隔离闸未就位（memory.provider="
            f"{iso.get('config_provider') or '未设置'}，应为 {iso.get('expected_provider')}）"
            f" → 适配器会拒绝唤醒。装闸步骤见 RUNBOOK「七」"))
    elif g_wake:
        out.append(_finding(
            "group_memory_guard", OK if (iso or {}).get("ok") else WARN,
            f"记忆隔离：provider={(iso or {}).get('config_provider') or '—'} / "
            f"闸门插件在位={bool((iso or {}).get('plugin_ok'))}"
            + ("" if guard_req else "；⚠️ group_memory_guard_required=false（排障档，不该长期开）")))
    out.append(_finding(
        "group_config",
        WARN if (g_enabled and g_wake and g_mode == "full") else OK,
        f"group_enabled={g_enabled} / group_wake_mode={g_mode}"
        + ("（⚠️ full = 每条群消息都起付费回合，未验收）" if (g_enabled and g_mode == "full")
           else "（被 @/被回复/点名才唤醒）" if (g_enabled and g_mode == "mention-only")
           else "（只采集，0 LLM）")
        + f"；dm_policy={dm_policy}, allow_from={n_dm_allow} 项"))
    return out, expected_running


def memory_isolation(profile_dir: Path) -> Dict[str, Any]:
    """记忆隔离闸静态判据（与适配器/`group_wake.memory_isolation_status` 同一套判据）。

    doctor 可能在没有 Hermes 环境变量时被跑，所以这里**直接读 profile 目录**，不 import 适配器。
    """
    out: Dict[str, Any] = {"config_provider": "", "expected_provider": "hindsight_guard",
                           "plugin_ok": False, "ok": False}
    try:
        import yaml
        cfg = yaml.safe_load((profile_dir / "config.yaml").read_text(encoding="utf-8")) or {}
        out["config_provider"] = str(((cfg.get("memory") or {}).get("provider")) or "")
    except Exception:  # noqa: BLE001
        pass
    out["plugin_ok"] = (profile_dir / "plugins" / "hindsight_guard" / "__init__.py").is_file()
    out["ok"] = bool(out["config_provider"] == out["expected_provider"] and out["plugin_ok"])
    return out


def check_state(state_path: Path, expected_running: bool
                ) -> tuple[List[Dict[str, str]], Dict[str, Any] | None]:
    """看心跳。两种语义要分清：**未启用 → 没状态文件是正常**；已启用 → 没有就是故障。"""
    state = None
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception as e:
            return [_finding("state_file", FAIL, f"{state_path} 读不出 JSON：{e}")], None

    if not expected_running:
        if state is None:
            return [_finding("state_file", OK,
                             "未启用 → 无心跳文件属预期（不是故障）")], None
        findings = judge(state, thresholds=thresholds_from_extra(None))
        stale = any(f["key"] == "heartbeat" for f in findings)
        return [_finding("state_file", WARN if stale else OK,
                         f"平台当前未启用，但存在残留心跳 {state_path}"
                         + ("（已过期）" if stale else "（尚新，可能是刚停）"))], state

    findings = judge(state, thresholds=thresholds_from_extra(None))
    return findings, state


def check_port(host: str, port: int) -> List[Dict[str, str]]:
    try:
        with socket.create_connection((host, port), timeout=3):
            return [_finding("port", OK, f"{host}:{port} 有人监听")]
    except OSError as e:
        return [_finding("port", WARN, f"{host}:{port} 连不上（{e}）—— 未启用时正常")]


def check_alert_channel(url: str) -> List[Dict[str, str]]:
    health_url = url.rsplit("/", 1)[0] + "/health"
    try:
        with urllib.request.urlopen(health_url, timeout=5) as resp:
            body = resp.read(200).decode("utf-8", "replace")
        return [_finding("alert_channel", OK, f"{health_url} 活体正常 {body[:120]}")]
    except (urllib.error.URLError, OSError, ValueError) as e:
        return [_finding("alert_channel", WARN, f"{health_url} 不可达（{e}）")]


def check_group_window(root: Path, profile: str, state: Dict[str, Any] | None) -> List[Dict[str, str]]:
    """群聊窗口（B 阶段）：只判「目录在不在、父目录可不可写、心跳里那两把量尺正不正常」。

    **只读**（doctor 的纪律：不写任何文件）—— 用 `os.access` 判可写性，不做写探针。
    只看**计数**：多少个群、多少条、多少字节。
    **绝不读消息正文**（隐私纪律，同 health.py 头注）。
    """
    out: List[Dict[str, str]] = []
    profile_dir = root / "profiles" / profile
    win_dir = profile_dir / "onebot-groups"
    if win_dir.is_dir():
        n = len(list(win_dir.glob("*.jsonl")))
        out.append(_finding("group_window_dir", OK, f"{win_dir}（{n} 个群的窗口文件）"))
    elif os.access(profile_dir, os.W_OK):
        out.append(_finding("group_window_dir", OK,
                            f"{win_dir} 尚未创建（首次收到群消息时自动建；父目录可写）"))
    else:
        out.append(_finding("group_window_dir", WARN,
                            f"{win_dir} 不存在且 {profile_dir} 不可写 —— 群消息采集会失败"))

    if state is None:
        return out
    if not state.get("group_enabled"):
        out.append(_finding("group_mode", OK, "群开关关着（群消息在准入处丢弃）"))
        return out
    llm = int(state.get("group_llm_calls") or 0)
    wake = bool(state.get("group_wake_enabled"))
    mode = str(state.get("group_wake_mode") or "collect-only")
    out.append(_finding(
        "group_mode",
        OK if (llm == 0 or wake) else FAIL,
        f"{state.get('group_mode', '?')}；已收 {state.get('group_rx_count', 0)} 条 / "
        f"进 agent {llm} 次（@ {state.get('group_mention_count', 0)} / "
        f"被回复 {state.get('group_reply_count', 0)} / 点名 {state.get('group_name_count', 0)}，"
        f"被限流 {state.get('group_wake_limited', 0)}，被拒 {state.get('group_wake_blocked', 0)}） / "
        f"窗口 {state.get('group_window', '—')}"))
    if wake and mode == "mention-only":
        out.append(_finding(
            "group_memory_isolation", OK if state.get("group_memory_isolated") else FAIL,
            "群回合记忆隔离：" + ("闸门在位（群回合不写主库）" if state.get("group_memory_isolated")
                                 else "⚠️ 未就位 —— 适配器本应拒绝唤醒，若仍在唤醒就是 bug")))
    if llm > 0:
        # 成本可见：群里起过付费回合就必须能一眼看到次数
        out.append(_finding("group_cost", OK,
                            f"群消息已起 {llm} 次付费回合（只在 @/被回复/点名时增长）"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="hermes_onebot 只读自检")
    ap.add_argument("--root", default=os.environ.get("HERMES_DATA_ROOT", "/opt/data"),
                    help="Hermes 数据根（容器内 /opt/data；宿主 /vol1/1000/<USER>")
    ap.add_argument("--profile", default=os.environ.get("HERMES_PROFILE", "chat"))
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args()

    root = Path(args.root)
    deployed = root / "profiles" / args.profile / "plugins" / "onebot"
    state_path = root / "profiles" / args.profile / "onebot-state.json"

    findings: List[Dict[str, str]] = []
    findings += check_copy(HERE, deployed)
    cfg_findings, expected_running = check_config(root, args.profile)
    findings += cfg_findings
    state_findings, state = check_state(state_path, expected_running)
    findings += state_findings
    findings += check_group_window(root, args.profile, state)
    findings += check_port(str((state or {}).get("host") or "127.0.0.1"),
                           int((state or {}).get("port") or 6700))
    findings += check_alert_channel("http://172.17.0.1:8098/report")

    summary = summarize(findings)
    if args.json:
        print(json.dumps({"summary": summary, "findings": findings, "state": state},
                         ensure_ascii=False, indent=2))
    else:
        print(f"hermes_onebot doctor  profile={args.profile}  root={root}")
        print(f"源码真身: {HERE}")
        print(f"结论: {summary}")
        print("-" * 60)
        for f in findings:
            mark = {OK: "  ok ", WARN: " warn", FAIL: " FAIL"}.get(f["level"], "  ?  ")
            print(f"[{mark}] {f['key']:16s} {f['detail']}")
        if state:
            print("-" * 60)
            print("运行态（来自心跳文件，无消息正文）:")
            for k in ("ts", "listener_up", "client_connected", "rx_count", "tx_count",
                      "segments_sent", "consecutive_failures", "debounce", "segmentation",
                      "group_mode", "group_wake_mode", "group_rx_count", "group_llm_calls",
                      "group_mention_count", "group_reply_count", "group_name_count",
                      "group_wake_limited", "group_wake_blocked", "group_last_trigger",
                      "group_memory_isolated", "group_memory_guard_required", "group_window"):
                if k in state:
                    print(f"  {k:22s} = {state[k]}")

    bad = [f for f in findings if f["level"] == FAIL]
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
