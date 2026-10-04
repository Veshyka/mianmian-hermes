#!/usr/bin/env python3
"""把 Ollama 的 GGUF blob 复制成 llama.cpp 能直接用的命名模型文件。

Ollama 的 blob 就是原样 GGUF，直接复制即可；本脚本只做三件事：
  1) 读 manifest，按 layers[].mediaType 认出 model / projector 层
  2) 复制成人类可读的文件名（复制，不是移动/映射——旧服务后续要删）
  3) 复制后校验 sha256 与 manifest 记录一致

改下面 MANIFESTS 表即可换模型。

用法：
    python3 ollama-blobs-to-gguf.py --check   # 只列计划，不写盘
    python3 ollama-blobs-to-gguf.py           # 真搬
"""
import hashlib
import json
import os
import shutil
import sys

SRC = "/vol2/@apphome/ai_installer/models"
BLOBS = os.path.join(SRC, "blobs")
DST = "/vol1/1000/<USER>"

# (manifest 相对路径, mediaType 结尾, 目标文件名)
MANIFESTS = [
    ("library/qwen3/8b", "model", "qwen3-8b-Q4_K_M.gguf"),
    ("library/qwen3-embedding/4b", "model", "qwen3-embedding-4b-Q4_K_M.gguf"),
    ("library/minicpm-v4.6/1b", "model", "minicpm-v4.6-1b-Q4_K_M.gguf"),
    ("library/minicpm-v4.6/1b", "projector", "minicpm-v4.6-1b-mmproj.gguf"),
]


def sha256_of(path, chunk=8 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def plan():
    out = []
    for mrel, kind, outname in MANIFESTS:
        mf = os.path.join(SRC, "manifests/registry.ollama.ai", mrel)
        with open(mf) as f:
            man = json.load(f)
        hit = [L for L in man["layers"] if L["mediaType"].endswith("image." + kind)]
        if not hit:
            print("!! manifest {} 里没有 image.{} 层，跳过".format(mrel, kind))
            continue
        layer = hit[0]
        out.append((os.path.join(BLOBS, layer["digest"].replace(":", "-")),
                    os.path.join(DST, outname), outname, layer["digest"]))
    return out


def main():
    dry = "--check" in sys.argv
    os.makedirs(DST, exist_ok=True)
    items = plan()
    for src, _dst, outname, _dg in items:
        print("[计划] {:<38s} {:5.2f} GB  <- {}".format(
            outname, os.path.getsize(src) / 1e9, os.path.basename(src)))
    if dry:
        print("\n(--check 模式，没有写盘)")
        return

    bad = []
    for src, dst, outname, digest in items:
        if os.path.exists(dst) and os.path.getsize(dst) == os.path.getsize(src):
            print("[跳过] {} 已存在且大小一致".format(outname))
            continue
        print("[复制] {} ...".format(outname), flush=True)
        tmp = dst + ".part"
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
        got = "sha256:" + sha256_of(dst)
        if got == digest:
            print("     ✓ sha256 一致")
        else:
            print("     ✗ sha256 不一致！期望 {} 实得 {}".format(digest, got))
            bad.append(outname)

    print("\n=== 结果 ===")
    for f in sorted(os.listdir(DST)):
        print("  {:<40s} {:6.2f} GB".format(f, os.path.getsize(os.path.join(DST, f)) / 1e9))
    print("失败 {} 个".format(len(bad)))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
