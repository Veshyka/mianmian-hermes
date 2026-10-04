#!/usr/bin/env python3
"""mihomo 换订阅一键脚本（标准化流程）。

用法:
    python3 mihomo_update_sub.py <订阅解码文件路径> [旧配置yaml路径]

行为:
    1. 读取 trojan:// 订阅链接列表 → 解析为 mihomo proxies
    2. 从 mihomo 容器导出当前配置（或使用指定旧配置）
    3. 备份旧配置
    4. 替换 proxies 段（保留 proxy-groups/rules/dns 等）
    5. 清理 proxy-groups 中引用已不存在的节点
    6. yaml 语法校验
    7. docker cp 新配置进容器 + docker restart
"""
import re, urllib.parse, sys, os, subprocess, shutil

def ssh(cmd):
    """经宿主 SSH 执行（含 sudo）。"""
    askpass = "SSH_ASKPASS=/opt/data/scripts/askpass.sh SSH_ASKPASS_REQUIRE=force setsid "
    full = f"{askpass}ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 棉棉@172.17.0.1 'export SUDO_ASKPASS=/vol1/1000/<USER> {cmd}' 2>&1"
    r = subprocess.run(full, shell=True, capture_output=True, text=True, timeout=120)
    return r.stdout

def main():
    if len(sys.argv) < 2:
        print("用法: mihomo_update_sub.py <订阅解码文件> [旧配置yaml]")
        sys.exit(1)
    sub_file = sys.argv[1]
    old_cfg = sys.argv[2] if len(sys.argv) > 2 else None

    # 1. 读取订阅
    lines = [l.strip() for l in open(sub_file, encoding="utf-8") if l.strip()]
    print(f"订阅节点数: {len(lines)}")

    proxies = []
    for line in lines:
        if not line.startswith("trojan://"):
            continue
        rest = line[len("trojan://"):]
        m = re.match(r"([^@]+)@([^:]+):(\d+)(\?[^#]*)?#?(.*)", rest)
        if not m:
            continue
        password, server, port, params, name = m.groups()
        params = params or ""
        name = urllib.parse.unquote(name).strip() if name else server
        sni = peer = None
        for k, v in re.findall(r"([a-zA-Z]+)=([^&]+)", params):
            v = urllib.parse.unquote(v)
            if k == "sni": sni = v
            elif k == "peer": peer = v
        p = {"name": name, "type": "trojan", "server": server,
             "port": int(port), "password": \"<SECRET>\", "udp": True,
             "skip-cert-verify": True}
        if sni: p["sni"] = sni
        if peer and peer != sni: p["peer"] = peer
        proxies.append(p)
    print(f"解析成功: {len(proxies)}")
    if len(proxies) < 50:
        print("节点太少，中止"); sys.exit(1)

    # 2. 获取旧配置
    tmp_dir = "/tmp/mihomo_sub_work"
    os.makedirs(tmp_dir, exist_ok=True)
    if old_cfg:
        cfg_text = open(old_cfg, encoding="utf-8").read()
    else:
        out = ssh("sudo -A docker cp mihomo:/etc/mihomo/config.yaml /tmp/mihomo_old_config.yaml && cat /tmp/mihomo_old_config.yaml")
        cfg_text = out
    if not cfg_text or "Error response" in cfg_text:
        try:
            cfg_text = open("/tmp/mihomo_old_config.yaml", encoding="utf-8").read()
        except Exception:
            print("获取旧配置失败"); sys.exit(1)
    bak_path = os.path.join(tmp_dir, "config.yaml.bak-" + os.path.basename(sub_file))
    with open(bak_path, "w", encoding="utf-8") as f:
        f.write(cfg_text)
    print(f"旧配置已备份: {bak_path}")

    # 3. 替换 proxies 段
    lines_cfg = cfg_text.split("\n")
    start = next((i for i, l in enumerate(lines_cfg) if re.match(r"^proxies:\s*$", l)), None)
    if start is None:
        print("找不到 proxies: 段"); sys.exit(1)
    end = next((i for i in range(start + 1, len(lines_cfg))
                if re.match(r"^\S", lines_cfg[i]) and not lines_cfg[i].startswith("- ")), len(lines_cfg))

    proxy_lines = ["proxies:"]
    for p in proxies:
        s = f"    - {{ name: '{p['name']}', type: trojan, server: {p['server']}, port: {p['port']}, password: {p['password']}, udp: true"
        if p.get("sni"): s += f", sni: {p['sni']}"
        if p.get("peer"): s += f", peer: {p['peer']}"
        s += ", skip-cert-verify: true }"
        proxy_lines.append(s)
    new_cfg = "\n".join(lines_cfg[:start] + proxy_lines + lines_cfg[end:])

    # 4. 清理策略组失效引用
    try:
        import yaml
        d = yaml.safe_load(new_cfg)
        proxy_names = {p["name"] for p in d["proxies"]}
        group_names = {g["name"] for g in d["proxy-groups"]}
        valid = proxy_names | group_names | {"DIRECT", "REJECT", "REJECT-DROP", "PASS", "PASS-RULE"}
        for g in d["proxy-groups"]:
            before = len(g.get("proxies", []))
            g["proxies"] = [p for p in g.get("proxies", []) if p in valid]
            if len(g["proxies"]) != before:
                print(f"组 {g['name']}: {before} -> {len(g['proxies'])} (清理失效引用)")
        new_cfg = yaml.safe_dump(d, allow_unicode=True, sort_keys=False)
        print("策略组清理完成")
    except ImportError:
        print("无 pyyaml，跳过清理（可能启动失败，需手动清组引用）")

    # 5. 校验 + 部署
    out_path = os.path.join(tmp_dir, "config.yaml.new")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(new_cfg)
    try:
        import yaml
        d = yaml.safe_load(new_cfg)
        print(f"校验 OK: {len(d['proxies'])} proxies / {len(d.get('proxy-groups', []))} groups / {len(d.get('rules', []))} rules")
    except Exception as e:
        print(f"校验失败: {e}"); sys.exit(1)

    # 上传 + 重启
    out = ssh(f"sudo -A docker cp {out_path} mihomo:/etc/mihomo/config.yaml && sudo -A docker restart mihomo")
    print(out[-500:] if out else "部署命令已发出")
    print("完成！等待容器起来后：延迟测试选活节点 → 切「节点选择」→ 验证 bing/google/SearXNG")

if __name__ == "__main__":
    main()
