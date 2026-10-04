#!/usr/bin/env python3
"""Hindsight 记忆软废弃工具（可复用）。

用法：
    mem_invalidate.py <id前缀> "<原因>" [<id前缀> "<原因>" ...]
    mem_invalidate.py --list-invalidated        # 看已废弃的
    mem_invalidate.py --dry <id前缀>            # 只看会改哪条（前缀可给 8 位）

只做 PATCH state=invalidated（软废弃，可追溯），**永不 DELETE**。
配套规程（何时废弃、三类条目怎么分、怎么验证）见
references/hindsight-memory-curation.md。
"""
import json
import os
import sys
import urllib.request

BANK = os.environ.get("HINDSIGHT_BANK", "mianmian-history")
BASE = "http://172.17.0.1:8888/v1/default/banks/" + BANK
# 容器外的 proxy 环境会拦本机请求，强制绕开
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def api(path, data=None, method="GET"):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json", "Accept-Encoding": "identity"},
        method=method,
    )
    return json.loads(OP.open(req, timeout=180).read())


def index():
    """id 前缀 -> 完整 id。存量大时把 limit 调大。"""
    d = api("/memories/list?limit=5000")
    return {it["id"][:8]: it["id"] for it in (d.get("items") or [])}


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return

    if args[0] == "--list-invalidated":
        d = api("/memories/list?limit=200&state=invalidated")
        for it in (d.get("items") or []):
            print("  %s | %s | %s" % (
                it["id"][:8],
                str(it.get("invalidated_at"))[:19],
                str(it.get("text"))[:90],
            ))
        print("共 %d 条" % d.get("total", len(d.get("items") or [])))
        return

    idx = index()
    dry = args[0] == "--dry"
    if dry:
        args = args[1:]

    for i in range(0, len(args), 2):
        prefix, reason = args[i], args[i + 1]
        mid = idx.get(prefix)
        if not mid:
            print("!! 找不到 %s" % prefix)
            continue
        if dry:
            print("会废弃 %s …" % prefix)
            continue
        r = api("/memories/" + mid, {"state": "invalidated", "reason": reason}, "PATCH")
        print("PATCH %s -> state=%s at=%s" % (
            prefix, r.get("state"), str(r.get("invalidated_at"))[:19]))


if __name__ == "__main__":
    main()
