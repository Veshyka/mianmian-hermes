#!/usr/bin/env python3
"""读 GGUF 头部元数据（不依赖 gguf 库，按 GGUF v3 规范手撸 KV 段）。

用途：拿架构事实——层数、KV 头数、head dim、原生 ctx、量化 file_type、arch。
这些是选型/搬模型时要核对的硬事实（例如决定某模型能不能塞进 12G 卡）。

⚠️ 它打印的 KV/token 只是「按元数据推算的假设值」，不是依据。
   本机实测过这套推算与真实显存对不上：按元数据算出 256 KiB/token，
   而 extract 总占用扣掉权重与 mmproj 后只剩 ~0.9 GiB，两者不可能同时成立。
   KV 的真实字节数只认服务端启动日志那行：
       sudo docker logs <llama 容器> 2>&1 | grep -iE "KV self size|llama_kv_cache_init|type_k"
   拿不到日志时，以「实测总占用 − 权重 − mmproj − 缓冲」反推的量级为准，别用手算值拍板。

用法：
  python3 gguf_meta.py /opt/data/llamacpp/models/<dir>/<model>.gguf
"""
import struct
import sys

# GGUF value type -> (struct code, size)
_T = {0: ("B", 1), 1: ("b", 1), 2: ("H", 2), 3: ("h", 2), 4: ("I", 4),
      5: ("i", 4), 6: ("f", 4), 7: ("?", 1), 10: ("Q", 8), 11: ("q", 8),
      12: ("d", 8)}


def _str(f):
    n = struct.unpack("<Q", f.read(8))[0]
    return f.read(n).decode("utf-8", "replace")


def _val(f, vt):
    if vt == 8:
        return _str(f)
    if vt == 9:  # array
        et = struct.unpack("<I", f.read(4))[0]
        n = struct.unpack("<Q", f.read(8))[0]
        return [_val(f, et) for _ in range(n)]
    code, size = _T[vt]
    return struct.unpack("<" + code, f.read(size))[0]


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    path = sys.argv[1]

    meta = {}
    with open(path, "rb") as f:
        magic = f.read(4)
        if magic != b"GGUF":
            sys.exit("不是 GGUF 文件（magic=%r）" % magic)
        ver = struct.unpack("<I", f.read(4))[0]
        n_tensors = struct.unpack("<Q", f.read(8))[0]
        n_kv = struct.unpack("<Q", f.read(8))[0]
        print("GGUF v%d  tensors=%d  kv=%d" % (ver, n_tensors, n_kv))
        for _ in range(n_kv):
            k = _str(f)
            v = _val(f, struct.unpack("<I", f.read(4))[0])
            meta[k] = v

    print("\n== 全部元数据（长数组只显示长度）==")
    for k in sorted(meta):
        v = meta[k]
        if isinstance(v, list) and len(v) > 16:
            v = "list len=%d" % len(v)
        print("  %-46s %s" % (k, v))

    def g(name, dflt=None):
        for k, v in meta.items():
            if k == name or k.endswith("." + name):
                return v
        return dflt

    arch = g("general.architecture")
    n_layer, n_kv_heads = g("block_count"), g("attention.head_count_kv")
    klen, vlen = g("attention.key_length"), g("attention.value_length")
    n_head = g("attention.head_count")

    print("\n== 关键事实 ==")
    print("  arch            = %s" % arch)
    print("  block_count     = %s" % n_layer)
    print("  head_count      = %s" % n_head)
    print("  head_count_kv   = %s" % n_kv_heads)
    print("  key/value_length= %s / %s" % (klen, vlen))
    print("  ctx_length(原生) = %s" % g("context_length"))
    print("  file_type       = %s" % g("general.file_type"))

    if n_layer and n_kv_heads and klen and vlen:
        per_tok_f16 = 2 * n_layer * n_kv_heads * (klen + vlen)
        print("\n  ⚠️ 假设值（非依据）f16 KV ≈ %.0f KiB/token" % (per_tok_f16 / 1024))
        print("     推算前提：key_length 就是每头维度、且 KV 未量化。")
        print("     真实字节数请读服务端日志的 `KV self size`，见本文件顶部说明。")


if __name__ == "__main__":
    main()
