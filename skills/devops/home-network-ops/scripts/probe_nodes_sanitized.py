#!/usr/bin/env python3
"""mihomo 节点列表脱敏探针：只输出计数 / 类别 / md5，绝不打印节点名、域名、IP、端口、URL。

用法（在跑 mihomo 的那台机器上跑，通常是宿主）：
    python3 probe_nodes_sanitized.py /path/to/config.yaml [--sample 40] [--api http://127.0.0.1:19090]

为什么必须脱敏：代理/订阅/节点清单是 provider 输入侧审核的最高敏类型，
命中即 HTTP 400 Content Exists Risk（模型不跑、重试无效、该会话永久废）。

判别口径（配合 SKILL.md「分层判别」表）：
  DNS 全失败            -> 机场域名失效，换订阅
  DNS 正常 + TCP 全 refused/timeout -> 本地节点列表过期（旧快照），重拉订阅
  TCP 能建连但过代理无流量 -> 账号 / 同时在线设备数 / 协议问题
"""
import argparse
import collections
import hashlib
import json
import os
import re
import socket
import ssl
import sys
import urllib.parse
import urllib.request


def md5_6(s):
    return hashlib.md5(s.encode()).hexdigest()[:6]


def parse_nodes(cfg_path):
    """节点是多行块：- name: / type: / server: / port: 各占一行。
    按 ' - ' 开头分块解析；按行配对 server+port 会得 0 个（踩过）。"""
    with open(cfg_path, "r", errors="ignore") as f:
        txt = f.read()
    m = re.search(r"^proxies:\s*$(.*?)^\w", txt, re.S | re.M)
    seg = m.group(1) if m else txt
    blocks, cur = [], []
    for line in seg.splitlines():
        if re.match(r"^\s*-\s", line):
            if cur:
                blocks.append(cur)
            cur = [line]
        else:
            cur.append(line)
    if cur:
        blocks.append(cur)
    pairs = []
    for blk in blocks:
        t = "\n".join(blk)
        s = re.search(r"server:\s*\"?'?([^,\s}\"']+)", t)
        p = re.search(r"port:\s*\"?'?(\d+)", t)
        if s and p:
            pairs.append((s.group(1).strip(), int(p.group(1))))
    return pairs


def cfg_fingerprint(cfg_path):
    """只报结构指纹：有没有 proxy-providers（= 会不会自己重拉订阅）、mtime、大小。"""
    with open(cfg_path, "r", errors="ignore") as f:
        txt = f.read()
    import datetime
    st = os.stat(cfg_path)
    return {
        "has_proxy_providers": len(re.findall(r"^proxy-providers:", txt, re.M)) > 0,
        "has_interval": "interval:" in txt,
        "size": st.st_size,
        "mtime": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
    }


def probe(pairs, sample_n):
    step = max(1, len(pairs) // sample_n)
    sample = pairs[::step][:sample_n]
    dns_stat, tcp_stat, ok = collections.Counter(), collections.Counter(), []
    for host, port in sample:
        try:
            ip = socket.gethostbyname(host)
            if ip.startswith(("10.", "127.", "192.168.", "172.16.", "169.254.")):
                dns_stat["internal_ip"] += 1
            elif ip == "0.0.0.0":
                dns_stat["zero_ip"] += 1
            else:
                dns_stat["resolved"] += 1
        except socket.gaierror:
            dns_stat["dns_fail"] += 1
            tcp_stat["skip_no_dns"] += 1
            continue
        except Exception as e:
            dns_stat[type(e).__name__] += 1
            tcp_stat["skip_no_dns"] += 1
            continue
        try:
            s = socket.create_connection((host, port), timeout=4)
            s.close()
            tcp_stat["ok"] += 1
            ok.append((host, port))
        except socket.timeout:
            tcp_stat["timeout"] += 1
        except ConnectionRefusedError:
            tcp_stat["refused"] += 1
        except Exception as e:
            tcp_stat[type(e).__name__] += 1
    tls_stat = collections.Counter()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    for host, port in ok[:10]:
        try:
            with socket.create_connection((host, port), timeout=5) as sock:
                with ctx.wrap_socket(sock) as ss:
                    ss.send(b"\x00")
                    tls_stat["handshake_ok"] += 1
        except Exception as e:
            tls_stat[type(e).__name__] += 1
    return sample, dns_stat, tcp_stat, tls_stat, ok


def api_counts(api_base):
    """数节点用 /proxies 按 type 计数；别用 /providers/proxies（会把组算进去）。"""
    try:
        data = json.load(urllib.request.urlopen(api_base.rstrip("/") + "/proxies", timeout=15))
        p = data.get("proxies", {})
        return {"total": len(p), "by_type": dict(collections.Counter(v.get("type") for v in p.values()))}
    except Exception as e:
        return {"error": type(e).__name__}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("config")
    ap.add_argument("--sample", type=int, default=40)
    ap.add_argument("--api", default=None, help="mihomo API（如 http://127.0.0.1:19090），可选")
    a = ap.parse_args()

    pairs = parse_nodes(a.config)
    out = {"config": cfg_fingerprint(a.config), "nodes_with_port": len(pairs)}
    if pairs:
        sample, dns_stat, tcp_stat, tls_stat, ok = probe(pairs, a.sample)
        out["sampled"] = len(sample)
        out["dns"] = dict(dns_stat)
        out["tcp"] = dict(tcp_stat)
        out["tls_top10"] = dict(tls_stat)
        out["reachable_md5"] = [md5_6(f"{h}:{p}") for h, p in ok[:5]]
    if a.api:
        out["api"] = api_counts(a.api)
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    sys.exit(main())
