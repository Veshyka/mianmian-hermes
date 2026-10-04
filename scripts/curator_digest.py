#!/usr/bin/env python3
"""curator 合并的固定后处理器：摘要 + 两条代码级护栏（不靠判断）。

背景：Hermes 的 curator 每月跑一次 LLM 合并（cron 调
scripts/curator_consolidate_monthly.sh）。它被系统 prompt 要求「至少归档 10 个」，
且有两点自由发挥会伤到这个库：

  ① 吸收一个技能后，它可以把对方的整包扔在 skills/.archive/ 里不搬运
     —— 深度文件（本机参数、排查记录、脚本）就此"可恢复但不可发现"
  ② 理论上它能做"无吸收归档"（prunings）

本脚本把这两点写死成代码，不交给模型判断：

  护栏 A：把本次所有被吸收技能归档包里未被搬运的
          references/ templates/ scripts/ assets/ 文件复制进伞技能同名目录
          （同名冲突加 `<来源技能>--` 前缀；幂等：已存在同尺寸文件则跳过）
  护栏 B：本次 run.json 的 prunings（无吸收归档）一律 `hermes curator restore` 撤销

摘要：没新报告就静默退出（cron 空 stdout = 完全静默）；有新动作时写
logs/curator/monthly-digest.md，并把结论 retain 进 Hindsight（bank mianmian-history），
这样 /new 之后的新会话也知晓技能库被动了什么。

不做的事：不删技能、不改技能正文、不整轮回滚（那要人拍板）。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERMES = "/opt/hermes/.venv/bin/hermes"
SKILLS = Path("/opt/data/skills")
ARCHIVE = SKILLS / ".archive"
CURATOR_LOGS = Path("/opt/data/logs/curator")
STATE = CURATOR_LOGS / ".digest-state.json"
DIGEST = CURATOR_LOGS / "monthly-digest.md"
HINDSIGHT = "http://172.17.0.1:8888/v1/default/banks/mianmian-history/memories"
REHOME_DIRS = ("references", "templates", "scripts", "assets")
# 单次归档数量告警线（只告警不自动回滚：「合并多少」属于"是否合并"的判断范畴）
ARCHIVE_ALERT = 8


def newest_run() -> tuple[str | None, dict | None]:
    """返回 (报告时间戳目录名, run.json 内容)，取 started_at 最新的那个。"""
    best_ts, best_data, best_at = None, None, ""
    if not CURATOR_LOGS.is_dir():
        return None, None
    for d in CURATOR_LOGS.iterdir():
        rj = d / "run.json"
        if not d.is_dir() or not rj.is_file():
            continue
        try:
            data = json.loads(rj.read_text(encoding="utf-8"))
        except Exception:
            continue
        at = str(data.get("started_at") or "")
        if at > best_at:
            best_ts, best_data, best_at = d.name, data, at
    return best_ts, best_data


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(st: dict) -> None:
    try:
        STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"[postprocess] 状态写入失败: {exc}", file=sys.stderr)


def find_skill_dir(name: str) -> Path | None:
    if not name:
        return None
    for p in SKILLS.rglob("SKILL.md"):
        if p.parent.name == name and ".archive" not in p.parts:
            return p.parent
    return None


def guard_rehome(consolidated) -> list[str]:
    """护栏 A：被吸收技能的支持文件没被搬进伞技能的，这里补搬（幂等）。"""
    notes: list[str] = []
    for e in consolidated or []:
        if not isinstance(e, dict):
            continue
        src_name, into = e.get("name"), e.get("into")
        src, dst = ARCHIVE / str(src_name), find_skill_dir(str(into))
        if not src.is_dir() or dst is None:
            notes.append(f"⚠️ {src_name}: 归档包或目标技能缺失，跳过")
            continue
        moved = 0
        for f in sorted(src.rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(src)
            if rel.name == "SKILL.md" or rel.parts[0] not in REHOME_DIRS:
                continue  # SKILL.md 已被蒸馏；只搬四个标准支持目录
            target = dst / rel
            if target.exists():
                if target.stat().st_size == f.stat().st_size:
                    continue  # 幂等：已搬过
                target = dst / rel.parent / f"{src_name}--{rel.name}"
                if target.exists():
                    continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, target)
                moved += 1
            except Exception as exc:
                notes.append(f"⚠️ {rel} 搬运失败: {exc}")
        if moved:
            notes.append(f"{into} ← {src_name}: 补搬 {moved} 个支持文件")
    return notes


def guard_restore_prunings(pruned) -> list[str]:
    """护栏 B：无吸收归档一律撤销（只授权了合并，没授权裸剪）。"""
    names = []
    for p in pruned or []:
        names.append(p.get("name") if isinstance(p, dict) else str(p))
    notes: list[str] = []
    for n in names:
        try:
            r = subprocess.run([HERMES, "curator", "restore", str(n)],
                               capture_output=True, text=True, timeout=180)
            ok = r.returncode == 0
            notes.append(f"{'已恢复' if ok else '恢复失败'}: {n}"
                         + ("" if ok else f" — {(r.stderr or r.stdout).strip()[:120]}"))
        except Exception as exc:
            notes.append(f"恢复异常: {n} — {exc}")
    return notes


def hindsight_retain(content: str) -> str:
    """把摘要写进 Hindsight；失败只返回原因，不抛异常（摘要文件已经落盘）。

    async=true：Hindsight 的 retain 走本地 LLM 抽取，同步等会超时（实测 60s 不够）。
    """
    body = json.dumps({"items": [{"content": content}], "async": True}).encode("utf-8")
    req = urllib.request.Request(
        HINDSIGHT, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    # 容器内访问 172.17.0.1 不能走代理
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=60) as resp:
            return f"ok({resp.status})"
    except Exception as exc:
        return f"失败: {exc}"


def fmt_names(entries, key="name", limit=8) -> str:
    if not isinstance(entries, list) or not entries:
        return "—"
    out = []
    for e in entries[:limit]:
        if isinstance(e, dict):
            frm = e.get("from") or e.get(key) or "?"
            into = e.get("into")
            out.append(f"{frm}→{into}" if into else str(frm))
        else:
            out.append(str(e))
    if len(entries) > limit:
        out.append(f"…共{len(entries)}")
    return "、".join(out)


def main() -> int:
    # 固定告警入口（启动器在 curator/后处理退出码非 0 时调用）：
    #   python3 curator_digest.py --alert "<消息>"
    # 只写摘要文件 + retain 进 Hindsight，不往任何对话推送。
    if len(sys.argv) > 2 and sys.argv[1] == "--alert":
        msg = sys.argv[2]
        stamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
        try:
            with DIGEST.open("a", encoding="utf-8") as fh:
                fh.write(f"- {stamp} ⚠️ {msg}\n")
        except Exception as exc:
            print(f"[postprocess] 告警写入失败: {exc}", file=sys.stderr)
        print(f"{stamp} ⚠️ {msg} · retain: {hindsight_retain(msg)}")
        return 0

    ts, data = newest_run()
    if not data:
        return 0  # 还没有报告 → 静默

    st = load_state()
    if st.get("last_digested") == ts:
        return 0  # 没有新报告 → 静默（日常走这条）

    c = data.get("counts") or {}
    consolidated = c.get("consolidated_this_run", 0) or 0
    pruned = c.get("pruned_this_run", 0) or 0
    archived = c.get("archived_this_run", 0) or 0
    added = c.get("added_this_run", 0) or 0
    rewritten = c.get("cron_jobs_rewritten", 0) or 0
    tool_calls = c.get("tool_calls_total", 0) or 0
    before, after = c.get("before"), c.get("after")
    duration = data.get("duration_seconds")
    err = data.get("llm_error")

    # 护栏先跑，再记状态（避免异常路径下重复搬运）
    rehomed = guard_rehome(data.get("consolidated"))
    restored = guard_restore_prunings(data.get("pruned"))

    st["last_digested"] = ts
    save_state(st)

    touched = bool(consolidated or pruned or archived or added or rewritten)
    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    alert = isinstance(archived, int) and archived > ARCHIVE_ALERT

    try:
        if not DIGEST.exists():
            DIGEST.write_text(
                "# curator 技能库合并摘要（自动生成）\n\n"
                "维护脚本 `/opt/data/scripts/curator_digest.py`（固定后处理）；"
                "源报告 `logs/curator/<ts>/run.json`；启动器 `scripts/curator_consolidate_monthly.sh`。\n\n",
                encoding="utf-8")
        with DIGEST.open("a", encoding="utf-8") as fh:
            fh.write(f"- {stamp} · 报告 `{ts}` · 技能 {before}→{after} · 合并 {consolidated} · "
                     f"归档无吸收 {pruned} · 新增 {added} · cron 引用改写 {rewritten} · "
                     f"工具调用 {tool_calls} · 耗时 {duration}s\n")
            if touched:
                fh.write(f"  - 合并明细: {fmt_names(data.get('consolidated'), 'name')}\n")
            for n in rehomed:
                fh.write(f"  - 护栏A(补搬深度): {n}\n")
            for n in restored:
                fh.write(f"  - 护栏B(撤销裸归档): {n}\n")
            if alert:
                fh.write(f"  - ⚠️ 本次归档 {archived} 个 > 告警线 {ARCHIVE_ALERT}，请人工确认形状\n")
            if err:
                fh.write(f"  - ⚠️ llm_error: {err}\n")
    except Exception as exc:
        print(f"[postprocess] 摘要写入失败: {exc}", file=sys.stderr)

    if not touched:
        return 0  # 跑了但什么都没动 → 静默，只留文件痕迹

    mem = (f"2026-09 起 curator 技能库合并每月一次（cron 1 号 05:30 调固定启动器）。"
           f"{stamp} 这次：技能 {before}→{after}，合并 {consolidated} 个"
           f"（{fmt_names(data.get('consolidated'), 'name')}），"
           f"无吸收归档 {pruned}（护栏B：{'; '.join(restored) if restored else '无'}），"
           f"新增 {added}，cron 引用改写 {rewritten}，工具调用 {tool_calls}，耗时 {duration}s。"
           f"报告 /opt/data/logs/curator/{ts}/REPORT.md；摘要 monthly-digest.md；"
           f"回滚 hermes curator rollback（整轮）或 restore <名字>（单个）。"
           + (f" ⚠️ 归档 {archived} 个触发告警线。" if alert else "")
           + (f" ⚠️ llm_error: {err}" if err else ""))
    retained = hindsight_retain(mem)

    print(f"curator 月度合并有新动作（报告 {ts}）：技能 {before}→{after}，"
          f"合并 {consolidated}、归档无吸收 {pruned}、新增 {added}、cron 引用改写 {rewritten}"
          f"（耗时 {duration}s，工具调用 {tool_calls}）")
    print(f"合并明细: {fmt_names(data.get('consolidated'), 'name')}")
    for n in rehomed:
        print(f"护栏A: {n}")
    for n in restored:
        print(f"护栏B: {n}")
    if alert:
        print(f"⚠️ 本次归档 {archived} 个，超过告警线 {ARCHIVE_ALERT} —— 请人工确认技能库形状")
    if err:
        print(f"⚠️ llm_error: {err}")
    print(f"摘要文件: {DIGEST} · Hindsight retain: {retained}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
