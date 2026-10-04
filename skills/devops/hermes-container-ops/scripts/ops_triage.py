#!/usr/bin/env python3
"""Hindsight 任务队列体检（默认只读）。

为什么用 API 而不是 psql：Hindsight 容器里的 Postgres 是内嵌实例（端口 5433、数据在
/home/hindsight/.pg0/instances/hindsight/data），连接要口令且不在 env 里；REST API 足够
且不会误伤数据。

用法：
    python3 ops_triage.py                      # 体检主库，只读
    python3 ops_triage.py --bank <bank>
    python3 ops_triage.py --zombie-min 30      # processing 超过多久算僵尸（默认 30 分钟）
    python3 ops_triage.py --retry <op_id>      # 显式重试某条（写操作，先自己确认再跑）

看什么：
  1) 队列全貌（按 task_type × status 聚合）——先看这个，别去猜「是不是有个大批量任务在跑」
  2) failed 清单 + 错误摘要（错误类型直接决定要不要 retry）
  3) 僵尸：status=processing 且 updated_at 停在很久以前（worker 崩溃遗留的占用）
  4) documents 总数与最新一条时间——判断「写入到底进没进库」的硬证据
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone

# 容器 env 里有 http_proxy，不禁掉的话 172.17.0.1 会被代理绕死（等价于 curl --noproxy '*'）
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get(url, timeout=25):
    try:
        with OPENER.open(url, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"_error": f"HTTP {e.code}: {e.read()[:200]!r}"}
    except Exception as e:  # noqa: BLE001
        return {"_error": str(e)}


def post(url, timeout=60):
    req = urllib.request.Request(url, data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with OPENER.open(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"_error": f"HTTP {e.code}: {e.read()[:200]!r}"}
    except Exception as e:  # noqa: BLE001
        return {"_error": str(e)}


def fetch_ops(base, status=None, task_type=None, max_pages=40):
    """分页拉 ops。limit 上限 = 100，传 >=200 会返回 {'detail': ...} 错误。"""
    out, offset = [], 0
    for _ in range(max_pages):
        q = ["limit=100", f"offset={offset}"]
        if status:
            q.append(f"status={status}")
        if task_type:
            q.append(f"type={task_type}")
        d = get(f"{base}/operations?" + "&".join(q))
        if "operations" not in d:
            print(f"  ! 取 ops 失败: {d.get('_error') or d}", file=sys.stderr)
            break
        batch = d["operations"]
        out += batch
        total = d.get("total") or 0
        offset += len(batch)
        if not batch or offset >= total:
            break
    return out


def parse_ts(s):
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bank", default="mianmian-history")
    ap.add_argument("--api", default="http://127.0.0.1:8888")
    ap.add_argument("--zombie-min", type=int, default=30)
    ap.add_argument("--retry", metavar="OP_ID")
    a = ap.parse_args()

    base = f"{a.api}/v1/default/banks/{a.bank}"

    if a.retry:
        r = post(f"{base}/operations/{a.retry}/retry")
        print("retry 结果:", json.dumps(r, ensure_ascii=False)[:300])
        return

    print(f"health: {get(a.api + '/health')}")
    for b in get(a.api + "/v1/default/banks").get("banks", []):
        if b.get("bank_id") == a.bank:
            print(f"bank {a.bank}: fact_count={b.get('fact_count')} updated={b.get('updated_at')}")

    docs = get(f"{base}/documents?limit=3")
    items = docs.get("documents") or docs.get("items") or []
    print(f"documents: total={docs.get('total')} 最新={items[0].get('created_at') if items else '-'}")

    ops = fetch_ops(base)
    print(f"\nops 取回 {len(ops)} 条")
    print("按类型×状态:", dict(Counter((o.get("task_type"), o.get("status")) for o in ops)))

    now = datetime.now(timezone.utc)
    failed = [o for o in ops if o.get("status") == "failed"]
    print(f"\n--- failed（{len(failed)}）---")
    for o in sorted(failed, key=lambda x: x.get("created_at") or "", reverse=True)[:25]:
        print(f"  {str(o.get('task_type')):20s} {str(o.get('created_at'))[:19]} retry={o.get('retry_count')} "
              f"items={o.get('items_count')} id={o.get('id')}\n     err={(o.get('error_message') or '')[:110]}")

    stuck = []
    for o in ops:
        if o.get("status") in ("processing", "pending"):
            ts = parse_ts(o.get("updated_at") or o.get("created_at"))
            if ts and (now - ts).total_seconds() > a.zombie_min * 60:
                stuck.append((o, int((now - ts).total_seconds() // 60)))
    print(f"\n--- 僵尸/停滞（{len(stuck)}，阈值 {a.zombie_min} 分钟）---")
    for o, mins in stuck:
        print(f"  {str(o.get('task_type')):20s} {str(o.get('status')):10s} 停了 {mins} 分钟 "
              f"created={str(o.get('created_at'))[:19]} retry={o.get('retry_count')} id={o.get('id')}")
    if stuck:
        print("  → processing 长期不动 = worker 崩溃遗留占用，用 --retry <op_id> 复活；"
              "pending 从未被取走则看 worker 槽是否被别的任务占着")

    print("\n错误分类速查: 503 Loading model=抽取后端在重载模型(等它起来再 retry) | "
          "502 batch embeddings=嵌入代理不通 | JSONDecodeError=8b 输出截断 | "
          "exceeded max recovery attempts=崩溃重试耗尽 | no tool-calling=该功能需工具调用模型")


if __name__ == "__main__":
    main()
