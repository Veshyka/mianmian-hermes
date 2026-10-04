"""运行状态判读（纯函数，零依赖，可离线单测）。

设计动机（主人红线 2）：**故障发现不了** 比内存/性能更致命。所以适配器持续把运行状态
写成一个 json 心跳文件，再由本模块给「正常 / 异常 + 具体哪一项」的判断 —— 判断逻辑
与采集分离，这样 doctor 脚本离线也能跑、单测也不需要起网关。

判据一览（阈值都可由 config.yaml 覆盖）：

| key                      | 含义                                         | 默认 |
|--------------------------|----------------------------------------------|------|
| ``state_stale_seconds``  | 心跳文件多久没更新 → 进程可能根本没在跑/卡死 | 180  |
| ``no_client_seconds``    | 协议端（NapCat）多久没连上来                 | 300  |
| ``client_grace_seconds`` | 适配器刚启动的宽限期：这期间「还没连上」不算故障 | 180  |
| ``rx_idle_seconds``      | 多久没收到任何事件 → 线还连着但没人来        | 43200(12h) |
| ``consecutive_failures`` | 连续发送失败次数上限                         | 5    |

状态文件里**只有计数、时间戳与错误摘要，绝不含消息正文与凭据**。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

OK = "ok"
WARN = "warn"
FAIL = "fail"
_LEVEL_RANK = {OK: 0, WARN: 1, FAIL: 2}

DEFAULT_THRESHOLDS: Dict[str, Any] = {
    "state_stale_seconds": 180.0,
    "no_client_seconds": 300.0,
    "client_grace_seconds": 180.0,
    "rx_idle_seconds": 43200.0,
    "consecutive_failures": 5,
}


def _num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def thresholds_from_extra(extra: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """从配置 extra 里取阈值覆盖（缺省用 DEFAULT_THRESHOLDS）。"""
    t = dict(DEFAULT_THRESHOLDS)
    extra = extra or {}
    for key in DEFAULT_THRESHOLDS:
        if key in extra and extra[key] is not None:
            t[key] = _num(extra[key], _num(DEFAULT_THRESHOLDS[key]))
    return t


def judge(state: Optional[Dict[str, Any]], *, now: Optional[float] = None,
          thresholds: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    """返回问题清单；空列表 = 全部正常。

    每项形如 ``{"key","level","detail"}``。level: ok/warn/fail。
    """
    t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    now = time.time() if now is None else float(now)

    if not state:
        return [{"key": "state_file", "level": FAIL,
                 "detail": "找不到状态文件 —— 适配器可能从未启动，或 profile 路径不对"}]

    findings: List[Dict[str, str]] = []

    # 0) 宽限期：适配器进程刚起来时，协议端(NapCat)还没连上属正常（重启后重连要几秒到几十秒）。
    #    这段时间内「从未连上」「还没收到事件」不判故障，否则每次网关/NapCat 重启都会误报。
    #    started_ts 缺失（老状态文件/单测合成状态）→ 视作进程早就在跑，退回原判据，不做宽限。
    grace = _num(t.get("client_grace_seconds"))
    boot_age = None
    started_ts = _num(state.get("started_ts"))
    if started_ts > 0:
        boot_age = max(0.0, now - started_ts)
    in_grace = bool(grace > 0 and boot_age is not None and boot_age < grace)

    # 1) 心跳新鲜度：这条最硬 —— 文件不更新说明进程没在跑/卡死
    age = now - _num(state.get("ts"))
    if age > _num(t["state_stale_seconds"]):
        findings.append({"key": "heartbeat", "level": FAIL,
                         "detail": f"心跳已 {_fmt_age(age)} 没更新（阈值 {_fmt_age(t['state_stale_seconds'])}）"
                                   " —— 适配器进程很可能没在运行"})

    # 2) 监听器
    if not state.get("listener_up"):
        findings.append({"key": "listener", "level": FAIL,
                         "detail": f"反向 WS 监听器未就绪（{state.get('host','?')}:{state.get('port','?')}）"})
    else:
        findings.append({"key": "listener", "level": OK,
                         "detail": f"监听中 {state.get('host','?')}:{state.get('port','?')}"})

    # 3) 协议端连接
    if not state.get("client_connected"):
        since = now - _num(state.get("last_client_ts"), 0.0)
        if _num(state.get("last_client_ts")) <= 0:
            if in_grace:
                findings.append({"key": "client", "level": WARN,
                                 "detail": f"NapCat（协议端）尚未连上（从未连上；适配器刚启动 "
                                           f"{_fmt_age(boot_age)}，仍在 {_fmt_age(grace)} 宽限内 "
                                           f"—— 重启后等协议端重连，不算故障）"})
            else:
                findings.append({"key": "client", "level": FAIL,
                                 "detail": "NapCat（协议端）从未连上"})
        elif since > _num(t["no_client_seconds"]):
            findings.append({"key": "client", "level": FAIL,
                             "detail": f"协议端已断开 {_fmt_age(since)}（阈值 {_fmt_age(t['no_client_seconds'])}）"})
        else:
            findings.append({"key": "client", "level": WARN,
                             "detail": f"协议端当前未连接（断 {_fmt_age(since)}）"})
    else:
        findings.append({"key": "client", "level": OK, "detail": "协议端已连接"})

    # 4) 入站空闲：线连着但长时间没有任何事件
    idle_limit = _num(t["rx_idle_seconds"])
    if idle_limit > 0:
        last_rx = _num(state.get("last_rx_ts"))
        if last_rx <= 0:
            if not in_grace:      # 刚启动、还没人说话属正常，宽限期内不提醒
                findings.append({"key": "rx_idle", "level": WARN,
                                 "detail": "启动至今没收到过任何事件"})
        elif now - last_rx > idle_limit:
            findings.append({"key": "rx_idle", "level": WARN,
                             "detail": f"已 {_fmt_age(now - last_rx)} 没收到任何事件"
                                       f"（阈值 {_fmt_age(idle_limit)}）—— 线可能静默掉了"})

    # 5) 连续发送失败
    fails = int(_num(state.get("consecutive_failures")))
    limit = int(_num(t["consecutive_failures"], 5))
    if fails >= limit:
        findings.append({"key": "send_failures", "level": FAIL,
                         "detail": f"连续 {fails} 次发送失败（阈值 {limit}）：{state.get('last_error') or '无摘要'}"})
    elif fails > 0:
        findings.append({"key": "send_failures", "level": WARN,
                         "detail": f"有 {fails} 次连续发送失败：{state.get('last_error') or '无摘要'}"})

    if state.get("read_only"):
        findings.append({"key": "read_only", "level": WARN,
                         "detail": "read_only=true —— 只收不发（原型/安全默认；要真发需显式关掉）"})

    # 6) 群聊（B 阶段：只看不说）—— 只判两件硬事，别把「群里没人说话」误报成故障
    if state.get("group_enabled"):
        rx = int(_num(state.get("group_rx_count")))
        llm = int(_num(state.get("group_llm_calls")))
        wake = bool(state.get("group_wake_enabled"))
        if llm > 0 and not wake:
            findings.append({"key": "group_wake", "level": FAIL,
                             "detail": f"group_wake_enabled=false 却已有 {llm} 次群消息进 agent"
                                       " —— B 阶段红线（0 次 LLM 调用）被破坏，立刻查日志"})
        elif llm > 0:
            findings.append({"key": "group_wake", "level": WARN,
                             "detail": f"群唤醒已打开：{llm} 次群消息进过 agent（付费回合）"})
        else:
            findings.append({"key": "group_wake", "level": OK,
                             "detail": f"群消息只采集不进 agent（已收 {rx} 条，0 次 LLM 调用）；"
                                       f"窗口 {state.get('group_window') or '—'}"})
        win_err = int(_num(state.get("group_window_errors")))
        if win_err > 0:
            findings.append({"key": "group_window", "level": WARN,
                             "detail": f"群窗口落盘失败 {win_err} 次（采集继续，但记录可能不全）"})

    return findings


def worst_level(findings: List[Dict[str, str]]) -> str:
    level = OK
    for f in findings:
        if _LEVEL_RANK.get(f.get("level", OK), 0) > _LEVEL_RANK.get(level, 0):
            level = f.get("level", OK)
    return level


def has_failure(findings: List[Dict[str, str]]) -> bool:
    return any(f.get("level") == FAIL for f in findings)


def summarize(findings: List[Dict[str, str]]) -> str:
    """一句话结论。

    * 有 fail → ``异常（N 项）: key[fail] …``
    * 只有 warn → ``正常（N 项提醒）: key[warn] …``   ← 提醒不等于异常
    * 全 ok → ``正常``
    """
    fails = [f for f in findings if f.get("level") == FAIL]
    warns = [f for f in findings if f.get("level") == WARN]
    if fails:
        head = "；".join(f"{f['key']}[{f['level']}]" for f in fails)
        extra = f"（另有 {len(warns)} 项提醒）" if warns else ""
        return f"异常（{len(fails)} 项）: {head}{extra}"
    if warns:
        head = "；".join(f"{f['key']}[{f['level']}]" for f in warns)
        return f"正常（{len(warns)} 项提醒）: {head}"
    return "正常"


def alert_text(findings: List[Dict[str, str]]) -> str:
    """给人看的一条告警（走已有通道发出去）。不含任何消息正文/凭据。"""
    bad = [f for f in findings if f.get("level") != OK]
    lines = [f"QQ 小号(OneBot)线体自检：{summarize(bad)}"]
    for f in bad:
        lines.append(f"- {f['key']}: {f['detail']}")
    return "\n".join(lines)


def _fmt_age(seconds: float) -> str:
    s = max(0.0, _num(seconds))
    if s < 90:
        return f"{s:.0f}s"
    if s < 5400:
        return f"{s / 60:.0f}min"
    if s < 172800:
        return f"{s / 3600:.1f}h"
    return f"{s / 86400:.1f}d"
