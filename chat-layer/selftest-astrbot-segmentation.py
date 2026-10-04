#!/usr/bin/env python3
"""分段自测：**走 AstrBot 真身的 ResultDecorateStage**，用线上 cmd_config.json 里的真正则。

不是另抄一份正则来测（那只能测抄得对不对）——这里：
  1. `ResultDecorateStage().initialize(ctx)` 走的就是 AstrBot 的 `initialize`，
     `self.regex` / `self.content_cleanup_rule` 从 `/AstrBot/data/cmd_config.json` 里读；
     先断言它读到的就是我们改成的 `⁂` 版。
  2. `stage.process(event)` 就是官方的分段代码路径
     （`/AstrBot/astrbot/core/pipeline/result_decorate/stage.py:219-268`），
     用的假 event 只提供 `get_result/get_platform_name/is_stopped`。
  3. 分段发生在 `result.chain = new_chain`，之后是 TTS/发送（要真 ctx 的服务，这里没有）——
     所以 process 跑到后面抛错不影响判定：链在那之前就改完了。

用法（容器内）：python3 /AstrBot/data/_selftest_segmentation.py
"""

import asyncio
import inspect
import json
import sys

sys.path.insert(0, "/AstrBot")

from astrbot.core.message.components import Plain  # noqa: E402
from astrbot.core.message.message_event_result import (  # noqa: E402
    MessageEventResult,
    ResultContentType,
)
from astrbot.core.pipeline.result_decorate.stage import ResultDecorateStage  # noqa: E402

CFG_PATH = "/AstrBot/data/cmd_config.json"
NEW_REGEX = ".*?[。？！~…⁂※⸮]+|.+$"
NEW_CLEANUP = "[⁂※⸮]"
SEPS = "⁂※⸮"


class FakeCtx:
    def __init__(self, cfg):
        self.astrbot_config = cfg


class FakeEvent:
    """只给分段需要的那几个接口。真 event 要 provider/platform 一堆东西。"""

    plugins_name = None

    def __init__(self, text):
        self.unified_msg_origin = "aiocqhttp:FriendMessage:<OWNER_QQ>"
        self._r = MessageEventResult().message(text)
        self._r.result_content_type = ResultContentType.LLM_RESULT

    def get_result(self):
        return self._r

    def get_platform_name(self):
        return "aiocqhttp"  # 不在排除名单（qq_official_webhook / weixin_official_account / dingtalk）

    def is_stopped(self):
        return False

    def get_extra(self, key, default=None):
        return default


async def split_via_stage(stage, text):
    ev = FakeEvent(text)
    gen = stage.process(ev)
    if hasattr(gen, "__anext__"):
        try:
            await gen.__anext__()
        except StopAsyncIteration:
            pass
        except Exception as exc:  # noqa: BLE001
            # 分段之后的 TTS/发送阶段要真 ctx —— 链已改完，这里可以忽略
            print("   (process 后段抛错，已忽略: %s: %s)" % (type(exc).__name__, exc))
    else:
        await gen
    return [c.text for c in ev.get_result().chain if isinstance(c, Plain)]


PASS, FAIL = [], []


def check(name, got, want):
    if got == want:
        PASS.append(name)
        print("  ✅ %s -> %r" % (name, got))
    else:
        FAIL.append(name)
        print("  ❌ %s\n     得到 %r\n     期望 %r" % (name, got, want))


def no_sep(name, segs):
    bad = [s for s in segs if any(ch in s for ch in SEPS)]
    if bad:
        FAIL.append(name + "(分隔符漏进消息)")
        print("  ❌ %s 分隔符漏进消息: %r" % (name, bad))
    else:
        print("  ✅ %s 分隔符没漏进消息" % name)


async def main():
    cfg = json.load(open(CFG_PATH, encoding="utf-8-sig"))
    stage = ResultDecorateStage()
    await stage.initialize(FakeCtx(cfg))

    print("== 0. 真身从线上配置读到的分段参数 ==")
    print("   regex            =", repr(stage.regex))
    print("   content_cleanup  =", repr(stage.content_cleanup_rule))
    print("   enable=%s only_llm_result=%s threshold=%s split_mode=%s" % (
        stage.enable_segmented_reply, stage.only_llm_result,
        stage.words_count_threshold, stage.split_mode))
    check("stage.regex 是 ⁂ 版", stage.regex, NEW_REGEX)
    check("stage.content_cleanup_rule 是 ⁂ 版", stage.content_cleanup_rule, NEW_CLEANUP)
    check("分段开关已开", stage.enable_segmented_reply, True)

    print("\n== 1. 主人自测文本（含 ⁂）→ 应切成多条 ==")
    t1 = "在啊⁂ 棉棉又不会自己跑掉⁂ 倒是你，这个点才冒出来⁂"
    s1 = await split_via_stage(stage, t1)
    check("含 ⁂ 的文本切成 3 条", s1, ["在啊", "棉棉又不会自己跑掉", "倒是你，这个点才冒出来"])
    no_sep("case1", s1)

    print("\n== 2. 旧话术容错（※ / ⸮）→ 也切、也不漏 ==")
    t2 = "旧话术还在用※ 变体⸮也切⁂"
    s2 = await split_via_stage(stage, t2)
    check("※ 与 ⸮ 同样当分隔点", s2, ["旧话术还在用", "变体", "也切"])
    no_sep("case2", s2)

    print("\n== 3. 真实聊天形状 ==")
    t3 = "？⁂刚才那个展我看了⁂到月底就结束了⁂要不要去⁂"
    s3 = await split_via_stage(stage, t3)
    check("混合标点 + ⁂", s3, ["？", "刚才那个展我看了", "到月底就结束了", "要不要去"])
    no_sep("case3", s3)

    print("\n== 4. 没有分隔符 → 整条一条 ==")
    t4 = "没有分隔符的一句话"
    check("不分段", await split_via_stage(stage, t4), [t4])

    print("\n== 5. 换行不分段（SOUL.md 里「别用换行分段」的依据） ==")
    t5 = "第一行\n第二行\n第三行"
    s5 = await split_via_stage(stage, t5)
    check("换行不切", s5, [t5])

    print("\n== 6. 超过 150 字阈值 → 整段一起发（阈值是上界） ==")
    t6 = "啊" * 200
    s6 = await split_via_stage(stage, t6)
    check("超阈值不切", s6, [t6])
    t6b = "短句⁂" + "啊" * 200
    s6b = await split_via_stage(stage, t6b)
    check("超阈值整段（连 ⁂ 一起不切）", s6b, [t6b])

    print("\n==== 结果：%d 通过 / %d 失败 ====" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项:", FAIL)
        raise SystemExit(1)
    print("SEG_SELFTEST_OK")


asyncio.run(main())
