#!/usr/bin/env python3
"""清理 proxy-groups 中引用已不存在节点的条目。

新订阅节点数常少于旧配置引用的节点数（机场改名/下线节点），mihomo 启动会
报 "Parse config error: proxy group[0]: ... not found" 并不断 Restarting。
跑完 update_mihomo_sub.py 后必须跑本脚本。

用法：python3 fix_mihomo_groups.py   （操作 /tmp/mihomo_new_config.yaml，自动备份）
"""
import yaml, shutil

CFG = "/tmp/mihomo_new_config.yaml"
d = yaml.safe_load(open(CFG, encoding="utf-8"))

proxy_names = {p["name"] for p in d["proxies"]}
group_names = {g["name"] for g in d["proxy-groups"]}
valid = proxy_names | group_names | {"DIRECT", "REJECT", "REJECT-DROP", "PASS", "PASS-RULE"}

for g in d["proxy-groups"]:
    before = len(g.get("proxies", []))
    kept = [p for p in g.get("proxies", []) if p in valid]
    removed = [p for p in g.get("proxies", []) if p not in valid]
    g["proxies"] = kept
    print(f"组 {g['name']}: {before} -> {len(kept)}")
    if removed:
        print(f"   移除: {removed}")

shutil.copy(CFG, CFG + ".bak-20260827-fixed")
with open(CFG, "w", encoding="utf-8") as f:
    yaml.safe_dump(d, f, allow_unicode=True, sort_keys=False)
print("已写入清理后的配置")
