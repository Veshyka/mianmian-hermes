#!/usr/bin/env python3
"""将新订阅 trojan:// 链接列表转为 mihomo proxies 段，替换 config.yaml。

用法（宿主有 python3；mihomo 容器内无 python3，脚本必须在宿主跑）：
1. 下载订阅：curl -sL -o /tmp/new_sub.yaml "<订阅URL>"
   （订阅常是 base64 编码的 trojan:// 链接列表，先 base64 -d 解码）
2. 从容器拷出旧配置：docker cp mihomo:/etc/mihomo/config.yaml /tmp/mihomo_old_config.yaml
3. 改本脚本顶部 SUB_FILE 指向解码后的链接文件，跑本脚本
4. 再跑 fix_mihomo_groups.py 清理 proxy-groups 里已不存在的节点引用
5. docker cp /tmp/mihomo_new_config.yaml mihomo:/etc/mihomo/config.yaml
6. docker restart mihomo，curl -x 验证
"""
import re, urllib.parse, sys, shutil

SUB_FILE = "/tmp/new_sub_decoded.txt"
OLD_CONFIG = "/tmp/mihomo_old_config.yaml"
CONFIG = "/tmp/mihomo_new_config.yaml"

lines = [l.strip() for l in open(SUB_FILE) if l.strip()]
print(f"订阅节点数: {len(lines)}")

proxies = []
for line in lines:
    if not line.startswith("trojan://"):
        continue
    rest = line[len("trojan://"):]
    m = re.match(r"([^@]+)@([^:]+):(\d+)(\?[^#]*)?#?(.*)", rest)
    if not m:
        print(f"解析失败: {line[:60]}")
        continue
    password, server, port, params, name = m.groups()
    params = params or ""
    name = urllib.parse.unquote(name).strip() if name else server
    sni = None
    peer = None
    for k, v in re.findall(r"([a-zA-Z]+)=([^&]+)", params):
        if k == "sni":
            sni = urllib.parse.unquote(v)
        elif k == "peer":
            peer = urllib.parse.unquote(v)
    p = {
        "name": name, "type": "trojan", "server": server,
        "port": int(port), "password": \"<SECRET>\",
        "udp": True, "skip-cert-verify": True,
    }
    if sni:
        p["sni"] = sni
    if peer and peer != sni:
        p["peer"] = peer
    proxies.append(p)

print(f"解析成功: {len(proxies)}")
if len(proxies) < 50:
    print("节点太少，中止")
    sys.exit(1)

bak = OLD_CONFIG + ".bak-20260827-sub"
shutil.copy(OLD_CONFIG, bak)
print(f"备份: {bak}")

cfg = open(OLD_CONFIG, encoding="utf-8").read()
lines_cfg = cfg.split("\n")
start = None
for i, l in enumerate(lines_cfg):
    if re.match(r"^proxies:\s*$", l):
        start = i
        break
if start is None:
    print("找不到 proxies: 段")
    sys.exit(1)
end = None
for i in range(start + 1, len(lines_cfg)):
    if re.match(r"^\S", lines_cfg[i]) and not lines_cfg[i].startswith("- "):
        end = i
        break
if end is None:
    end = len(lines_cfg)

proxy_lines = ["proxies:"]
for p in proxies:
    line = f"    - {{ name: '{p['name']}', type: trojan, server: {p['server']}, port: {p['port']}, password: {p['password']}, udp: true"
    if p.get("sni"):
        line += f", sni: {p['sni']}"
    if p.get("peer"):
        line += f", peer: {p['peer']}"
    line += ", skip-cert-verify: true }"
    proxy_lines.append(line)

new_cfg = "\n".join(lines_cfg[:start] + proxy_lines + lines_cfg[end:])
open(CONFIG, "w", encoding="utf-8").write(new_cfg)
print(f"已写入新配置（proxies 段 {start}-{end} 替换）")
