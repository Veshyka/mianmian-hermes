"""hermes_lookup 离线自检（证据集）：直调 _do_* 走真网络。

用法（容器内）：python3 /AstrBot/data/_verify_lookup.py
"""

import asyncio
import importlib.util
import sys
import traceback

PLUGIN = "/AstrBot/data/plugins/hermes_lookup/main.py"

spec = importlib.util.spec_from_file_location("hermes_lookup_probe", PLUGIN)
mod = importlib.util.module_from_spec(spec)
sys.modules["hermes_lookup_probe"] = mod
spec.loader.exec_module(mod)

# 真实公众号文章（SearXNG 搜 site:mp.weixin.qq.com 得到的链接）
WX_URL = "https://mp.weixin.qq.com/s/_wqWNSqb-YPiH6Knpq3y0g"


def make():
    obj = mod.Main.__new__(mod.Main)
    obj.config = dict(mod.DEFAULTS)
    obj._session = None
    obj._bili_cookie_at = 0.0
    return obj


async def main():
    obj = make()
    cases = [
        ("web_search", lambda: obj._do_web_search("OpenClaw 是什么", 3)),
        ("fetch_page 境内(baidu)", lambda: obj._do_fetch_page("https://www.baidu.com/", 400)),
        ("fetch_page 境外示例(example.com 代理兜底)",
         lambda: obj._do_fetch_page("https://example.com/", 800)),
        ("fetch_page 境外大页(wiki 代理兜底)",
         lambda: obj._do_fetch_page("https://zh.wikipedia.org/wiki/OpenClaw", 1200)),
        ("fetch_page 公众号(微信 UA)", lambda: obj._do_fetch_page(WX_URL, 900)),
        ("fetch_page 公众号(乱 id → 要说话)",
         lambda: obj._do_fetch_page("https://mp.weixin.qq.com/s/abcdef", 500)),
        ("bilibili_lookup BV1dDub6PE31", lambda: obj._do_bilibili_lookup("BV1dDub6PE31")),
        ("bilibili_lookup 链接形式",
         lambda: obj._do_bilibili_lookup("https://www.bilibili.com/video/BV1jEAaz3E6K/?spm_id_from=333")),
        ("bilibili_search", lambda: obj._do_bilibili_search("openclaw", 3)),
        ("web_search 空关键词", lambda: obj._do_web_search("   ", 3)),
        ("bilibili_lookup 乱输入", lambda: obj._do_bilibili_lookup("abc")),
    ]
    for i, (name, fn) in enumerate(cases, 1):
        print("=" * 70)
        print(f"### [{i}] {name}")
        try:
            out = await fn()
        except Exception:  # noqa: BLE001
            out = "!!! EXCEPTION\n" + traceback.format_exc()
        print((out or "")[:1800])
    if obj._session and not obj._session.closed:
        await obj._session.close()


asyncio.run(main())
