#!/usr/bin/env python3
"""HF 大文件分块下载器（显式 Range 起止 + Content-Range 校验 + sha256 校对）。

为什么不用 curl：本机代理链下 `curl -C -` / `--retry` 的 Range 语义不稳，
实测把 6GB 文件追加成 6.85GB（且 --retry 会从头覆盖丢进度）。
本脚本每块校验响应 Content-Range 的起始偏移，不符就截断重来，永不会追加越界。

用法：改 URL/OUT/TOTAL/SHA 后 python3 hf_chunked_dl.py
TOTAL/SHA 从 https://huggingface.co/api/models/<repo>?blobs=true 的 siblings[].size / lfs.sha256 取。
"""
import hashlib, os, socket, time, urllib.request

URL = "https://huggingface.co/prism-ml/Ternary-Bonsai-2-27B-gguf/resolve/main/Ternary-Bonsai-2-27B-PTQ1_0.gguf"
OUT = "/opt/data/llamacpp/models/bonsai-27b/Ternary-Bonsai-2-27B-PTQ1_0.gguf"
TOTAL = 5946648928
SHA = "53107f530aa52eb00912263ab1ee29bd199261c87cd7b4ad4ca1318c1fe33ee3"
PROXY = "http://127.0.0.1:17890"  # 本机 mihomo；不需要就设成 None
CHUNK = 128 << 20

handlers = [urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})] if PROXY else []
opener = urllib.request.build_opener(*handlers)
socket.setdefaulttimeout(90)

if os.path.exists(OUT) and os.path.getsize(OUT) > TOTAL:
    os.truncate(OUT, 0)

deadline = time.time() + 3000
while time.time() < deadline:
    have = os.path.getsize(OUT) if os.path.exists(OUT) else 0
    if have >= TOTAL:
        break
    end = min(have + CHUNK - 1, TOTAL - 1)
    req = urllib.request.Request(URL, headers={"Range": f"bytes={have}-{end}", "User-Agent": "curl/8.5"})
    try:
        with opener.open(req) as r:
            cr = r.headers.get("Content-Range", "")
            if r.status != 206 or not cr.startswith(f"bytes {have}-"):
                print(f"BAD RANGE status={r.status} content-range={cr!r} have={have} -> truncate & retry", flush=True)
                os.truncate(OUT, 0)
                continue
            data = r.read()
            want = end - have + 1
            if len(data) != want:
                print(f"short read {len(data)} != {want}; not writing", flush=True)
                continue
            with open(OUT, "ab") as f:
                f.write(data)
            got = os.path.getsize(OUT)
            print(f"{got}/{TOTAL} ({got * 100.0 / TOTAL:.1f}%)", flush=True)
    except Exception as e:
        print(f"{type(e).__name__}: {str(e)[:100]} retry", flush=True)
        time.sleep(2)

size = os.path.getsize(OUT)
print("SIZE", size, "OK" if size == TOTAL else "MISMATCH", flush=True)
if size == TOTAL and SHA:
    h = hashlib.sha256()
    with open(OUT, "rb") as f:
        for b in iter(lambda: f.read(1 << 24), b""):
            h.update(b)
    print("SHA256", h.hexdigest(), "MATCH" if h.hexdigest() == SHA else "MISMATCH", flush=True)
