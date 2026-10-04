#!/usr/bin/env python3
"""实测 llama.cpp 服务端的 decode/prefill 速度与上下文。

为什么不用墙钟：请求在队列里等、被别的请求占卡的时间会全算进 wall。
实测同一实例连打两次同一请求，wall 差 150s（157.7s vs 5.7s），
而 decode 只差 0.7 t/s——所以一律取服务端自报的 timings。

用法：
  python3 llama_speed.py                     # 默认本机 8081
  python3 llama_speed.py --base http://172.17.0.1:8082 --rounds 3

读什么：
  /props      -> default_generation_settings.n_ctx、total_slots、model_alias、build_info
  /completion -> timings.predicted_per_second / prompt_per_second / predicted_n

注意：
  * 测基准要挑空闲时。后台有重跑/批处理占卡时 decode 低 ~4 t/s，不是常态值。
  * n_predict 别给太小（<100 会被首 token 开销污染）；cache_prompt=false 避免命中缓存。
"""
import argparse
import json
import time
import urllib.request


def _open(url, data=None, timeout=600):
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 绕开容器代理
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json", "Accept-Encoding": "identity"},
    )
    with op.open(req, timeout=timeout) as f:
        raw = f.read()
    return json.loads(raw) if raw.strip() else {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://172.17.0.1:8081")
    ap.add_argument("--prompt", default="用一句话解释什么是 KV cache。")
    ap.add_argument("--n-predict", type=int, default=128)
    ap.add_argument("--rounds", type=int, default=3)
    args = ap.parse_args()

    props = _open(args.base + "/props")
    gen = props.get("default_generation_settings") or {}
    print("== 上下文 ==")
    print("  n_ctx        =", gen.get("n_ctx"))
    print("  total_slots  =", props.get("total_slots"))
    print("  model_alias  =", props.get("model_alias"), "|", props.get("build_info"))
    if props.get("total_slots"):
        print("  ⇒ 每槽上下文 = n_ctx / total_slots = %s" % (
            (gen.get("n_ctx") or 0) // props["total_slots"]))

    print("\n== decode 实测（%d 轮，取服务端 timings）==" % args.rounds)
    for i in range(args.rounds):
        t0 = time.time()
        r = _open(args.base + "/completion", {
            "prompt": args.prompt,
            "n_predict": args.n_predict,
            "temperature": 0.0,
            "cache_prompt": False,
        })
        wall = time.time() - t0
        t = r.get("timings") or {}
        print("  轮%d: decode %.1f t/s | prefill %.0f t/s | 出 %s tok | wall %.1fs"
              % (i + 1, t.get("predicted_per_second") or -1,
                 t.get("prompt_per_second") or -1,
                 t.get("predicted_n"), wall))
    print("\n提示：wall 含排队时间，别当速度；decode 以 timings 为准。")


if __name__ == "__main__":
    main()
