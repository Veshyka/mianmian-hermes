"""出站分段单测 —— 锁定 2026-09-23 主人拍板后的规矩。

**规矩只剩一条**：出现 `⁂` 就切；没有 `⁂` 就整段发。**没有长度阈值。**
（旧版抄 AstrBot 的「超过 N 字就不切」，结果字数一多（尤其英文占比高、字符数容易超）
整条不切 → 不切也就不清理 → `⁂` 留在正文里被主人看到。阈值整个删掉，连配置项一起废弃。）

四条硬性用例（对应主人给的验收）：
  ① 短对话含 `⁂` → 切成多条且 `⁂` 不出现；
  ② 任意长度含 `⁂`（含英文占比高的 1000+ 字）→ 照切、`⁂` 不出现；
  ③ 不含 `⁂` 的 1000+ 字 → **一条不切**；
  ④ 异常/降级路径 → 也绝不能出现 `⁂`（`scrub()` 兜底 + 整条只有分隔符时返回空）。

⚠️ 文件名故意**不是** `test_*.py`：Hermes 自带的 `disk-cleanup` 插件把
`test_*` / `tmp_*` 归为「ephemeral test 产物」，任务结束即删（源码
`/opt/hermes/plugins/disk-cleanup/disk_cleanup.py:313` `_TEST_PATTERNS = ("test_", "tmp_")`
→ `:333` 归类为 "test" → `:177` age>=0 即删）。`chat-layer/` 不在它的
`_NEVER_TRACK_TOP_LEVEL` 保护名单里，所以这里一律用 `check_` 前缀。

运行： bash tests/run_tests.sh      或      python3 tests/check_segmentation.py
"""

from __future__ import annotations

import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import segmentation  # noqa: E402
from segmentation import (  # noqa: E402
    DEFAULT_CLEANUP_RULE, DEFAULT_INTERVAL, DEFAULT_REGEX, SEP, SEP_TOLERATED,
    parse_interval, scrub, segment_intervals, split_text,
)


class TestSplitText(unittest.TestCase):
    def test_sep_only_splits_and_is_stripped(self):
        """`⁂` 只当分隔、不进消息（主人明确要的行为）。"""
        self.assertEqual(split_text("第一句⁂第二句⁂第三句"), ["第一句", "第二句", "第三句"])

    def test_no_sep_symbol_leaks_into_any_segment(self):
        for seg in split_text("甲⁂乙⁂丙⁂丁"):
            self.assertNotIn(SEP, seg)

    def test_sentence_punctuation_does_not_split(self):
        """句末标点**不再**是切分点（旧版会切）。

        必须这样：「不含 `⁂` 的 1000+ 字长文要一条不切」——长文里必然有句号和换行。
        """
        self.assertEqual(split_text("你好。今天天气不错。"), ["你好。今天天气不错。"])

    def test_newlines_do_not_split(self):
        self.assertEqual(split_text("第一行\n第二行\n第三行"), ["第一行\n第二行\n第三行"])

    def test_mixed_sep_and_punctuation_splits_only_on_sep(self):
        got = split_text("累了一天。⁂早点睡吧？⁂晚安~")
        self.assertEqual(got, ["累了一天。", "早点睡吧？", "晚安~"])

    def test_trailing_sep_is_removed(self):
        """findall 会把尾随 ⁂ 切进上一条 → 清理层必须带走它。"""
        self.assertEqual(split_text("我们明天再聊吧⁂"), ["我们明天再聊吧"])

    def test_no_separator_single_segment(self):
        self.assertEqual(split_text("单条不带标点"), ["单条不带标点"])

    def test_blank_segments_dropped(self):
        self.assertEqual(split_text("甲⁂ ⁂乙"), ["甲", "乙"])

    def test_each_segment_is_stripped(self):
        for seg in split_text("  甲  ⁂  乙  "):
            self.assertEqual(seg, seg.strip())

    def test_empty_and_whitespace(self):
        self.assertEqual(split_text(""), [])
        self.assertEqual(split_text(None), [])
        self.assertEqual(split_text("   \n  "), [])

    def test_separators_only_is_dropped_not_echoed(self):
        """整条只有 `⁂` → **返回空**，绝不退回原文。

        旧版「全被清空就退回原文」会把 `⁂⁂⁂` 原样发出去 —— 那正是硬红线要挡的。
        """
        self.assertEqual(split_text("⁂⁂⁂"), [])
        self.assertEqual(split_text("⁂ ※ ⸮"), [])

    def test_cleanup_can_be_disabled(self):
        got = split_text("甲⁂乙", cleanup="")
        self.assertEqual(got, ["甲⁂", "乙"])

    def test_broken_regex_falls_back(self):
        got = split_text("甲。⁂乙。", regex="([unclosed")
        self.assertEqual(got, ["甲。", "乙。"])

    def test_capturing_group_regex(self):
        """带捕获组的正则 findall 返回 tuple —— 不能把 tuple 拼进消息。"""
        got = split_text("甲。乙。", regex=r"[^。]+。")
        self.assertEqual(got, ["甲。", "乙。"])


class TestNoLengthThreshold(unittest.TestCase):
    """**阈值不许回来**（2026-09-23 主人拍板删掉的规则）。

    这组测试的作用是「防复活」：谁要是再把长度判断加进 `split_text` 或把
    `DEFAULT_THRESHOLD` 加回来，这里立刻红。
    """

    def test_threshold_symbols_are_gone(self):
        for name in ("DEFAULT_THRESHOLD", "THRESHOLD_BAND", "parse_threshold"):
            self.assertFalse(hasattr(segmentation, name),
                             f"{name} 又回来了 —— 阈值已废弃，别再引入长度判断")

    def test_split_text_takes_no_threshold(self):
        params = set(inspect.signature(split_text).parameters)
        self.assertNotIn("threshold", params)

    def test_length_never_changes_the_outcome(self):
        """同一段内容，加长到任意倍数，切分结果只由 `⁂` 决定。"""
        base = "很短的句子⁂第二句"
        self.assertEqual(split_text(base), ["很短的句子", "第二句"])
        self.assertEqual(split_text("啊" * 5000 + base), ["啊" * 5000 + "很短的句子", "第二句"])

    def test_long_text_without_sep_stays_whole(self):
        long_no_sep = "啊" * 3000
        self.assertEqual(split_text(long_no_sep), [long_no_sep])


class TestScrubFallback(unittest.TestCase):
    """硬兜底 `scrub()` —— 异常/降级路径的最后一道闸。"""

    def test_removes_all_tolerated_separators(self):
        self.assertEqual(scrub("甲⁂乙※丙⸮丁"), "甲乙丙丁")

    def test_keeps_everything_else(self):
        text = "正常正文，带标点。还有 English words 和数字 123"
        self.assertEqual(scrub(text), text)

    def test_broken_cleanup_rule_still_filters(self):
        """清理正则坏了也不能放符号出去：退回逐字符白名单过滤。"""
        got = scrub("甲⁂乙※丙", cleanup="([unclosed")
        self.assertEqual(got, "甲乙丙")

    def test_empty_inputs(self):
        self.assertEqual(scrub(""), "")
        self.assertEqual(scrub(None), "")

    def test_scrub_is_idempotent(self):
        once = scrub("甲⁂乙")
        self.assertEqual(scrub(once), once)


class TestRealContentBehaviour(unittest.TestCase):
    """用**真实形状**的内容锁行为（主人报的问题就在这几个形状上）。"""

    # 一段 1000+ 字的「技能清单」（真实形状：分点 + 类别 + 说明）。
    LONG_CHECKLIST = """以下是当前可用的技能清单，按类别列出，每一类下面给出的是技能名和它能干的事：
一、沟通类：聊天门技能（改提示词、验证加载）、主动消息规则（心跳 cron、引用消息解读、群聊漏消息排查）、陪伴型提示词的写法（少规则多示例、写节奏、不给结论、工具面要窄）。
二、创作类：信息图（21 种布局 × 21 种风格）、ASCII 艺术与 ASCII 视频、架构图与云图、演示文稿生成与排版优化、网页设计样例库（Stripe/Linear/Vercel 那种真实设计系统）、手绘风流程图、网页原型对比。
三、运维类：Hermes 容器运维手册（重建、挂载、权限、平台接入、重启恢复）、更新前固化自检（易失文件、常驻进程、自启项）、自建服务换后端的安全流程、网络诊断、代理与路由器排障、机场订阅与节点切换。
四、文档类：Word 读写与模板、Excel 读写与公式、PDF 处理与文字编辑、扫描件整页 OCR（中文、纯 CPU、内存紧）、中文正式文稿排版（字体字号行距规范）与交付。
五、研究类：论文检索、院校投档线与办学条件核查、公司动态与竞品监控、RSS 订阅、网页正文提取（含公众号反爬）、有出处的引用式写作。
六、媒体类：视频下载、B 站元数据与分章字幕、音频频谱与特征分析、图片与视频生成、媒体库自动追番（AutoBangumi + qB + 影视端）与字幕整理。
七、记忆类：记忆引擎读写（retain/recall/reflect）、知识图谱、Obsidian 笔记、历史会话检索与归档、跨会话的项目进度接力。
八、代码与协作类：GitHub 工作流（issue → PR → 评审 → 合并）、代码审查与安全扫描、派活给别的 agent、任务书模板与格式闸、多智能体并行与合并冲突协调。
九、生活与其他：番茄钟与下载器管理、NAS 文件与相册、智能家居灯具控制、节假日与调休查询、地图与路线、价格与航班监控、邮箱分诊。
十、说明：清单里的每一项都可以单独派活去做，路径、参数、依赖都由干活门那边负责；如果某一项需要登录态，或者要中途改方向，也应该走派活，不要在这边硬做，也不要假装自己做完了。清单只是索引，具体怎么用要看当时的上下文，别照抄，也别把它当成必须逐条念出来的稿子。
十一、验收与交付：改完要自己跑单测（必须全过）、跑自检（退出码必须是 0）、拿一段真实长内容和一段真实短对话各过一遍分段函数，把结论写进交付说明；不许拿猜的结论当证据，也不许把没跑的项写成跑过了。
十二、分工边界：这边只做能一步做完的事；要登录态、要多步、要中途改方向的一律派出去。派活的时候要给清目标、验收标准、以及「不要动什么」，回来先核验再转述，不要直接照搬。
"""

    # 真实形状的短对话（主人那边看到的常常就是这种三连）
    SHORT_TURNS = "在啊⁂\n棉棉又不会自己跑掉⁂\n倒是你，这个点才冒出来⁂"

    # 英文占比高的长内容（主人报的那个形状：英文一多，字符数蹭蹭涨）
    ENGLISH_BLOCK = "The quick brown fox jumps over the lazy dog. " * 5

    # ── ① 短对话：切成多条、无符号 ────────────────────────────────────────
    def test_short_real_dialogue_splits_into_three(self):
        got = split_text(self.SHORT_TURNS)
        self.assertEqual(got, ["在啊", "棉棉又不会自己跑掉", "倒是你，这个点才冒出来"])
        for seg in got:
            self.assertNotIn(SEP, seg)
        self.assertFalse(any("\n" in s for s in got))

    # ── ② 任意长度含 ⁂：照切、无符号（英文多的 1000+ 字也照切）─────────────
    def test_long_english_heavy_with_sep_is_split_and_clean(self):
        text = SEP.join([self.ENGLISH_BLOCK] * 6)
        self.assertGreater(len(text), 1000, "样本得真够长")
        self.assertGreater(text.count(SEP), 1)
        got = split_text(text)
        self.assertEqual(len(got), 6, "有 ⁂ 就该照切，不管多长、多少英文")
        for seg in got:
            self.assertNotIn(SEP, seg)
        self.assertIn("quick brown fox", got[0])

    def test_long_chinese_with_trailing_seps_splits_and_cleans(self):
        text = self.LONG_CHECKLIST.replace("十、说明", SEP + "十、说明")
        self.assertGreater(len(text), 1000)
        got = split_text(text)
        self.assertEqual(len(got), 2)
        for seg in got:
            for ch in SEP_TOLERATED:
                self.assertNotIn(ch, seg, f"{ch!r} 漏进了长内容正文")

    # ── ③ 不含 ⁂ 的 1000+ 字：一条不切 ───────────────────────────────────
    def test_long_real_content_without_sep_is_one_message(self):
        self.assertGreater(len(self.LONG_CHECKLIST), 1000)
        self.assertNotIn(SEP, self.LONG_CHECKLIST)
        got = split_text(self.LONG_CHECKLIST)
        self.assertEqual(len(got), 1, "长内容整段发 —— 靠「她不写 ⁂」实现，不再靠阈值")
        self.assertIn("NAS 文件与相册", got[0])

    def test_long_english_without_sep_is_one_message(self):
        text = self.ENGLISH_BLOCK * 5
        self.assertGreater(len(text), 1000)
        self.assertEqual(split_text(text), [text.rstrip()])

    def test_huge_text_without_sep_still_one_message(self):
        text = self.LONG_CHECKLIST * 3
        self.assertGreater(len(text), 3000)
        self.assertEqual(len(split_text(text)), 1)

    # ── ④ 异常/降级路径 ──────────────────────────────────────────────────
    def test_degraded_paths_never_leak_symbols(self):
        """所有「非正常切分」的输入形状都过一遍：输出里不许有任何一个容错符号。"""
        weird = [
            SEP * 10,
            "只有分隔符" + SEP,
            "⁂" + self.LONG_CHECKLIST + "⁂",
            "\n⁂\n※\n⸮\n",
            SEP.join(["", "", ""]),
        ]
        for text in weird:
            for seg in split_text(text):
                for ch in SEP_TOLERATED:
                    self.assertNotIn(ch, seg, f"{ch!r} 漏进了 {seg[:40]!r}")
                # 降级洗一遍也不能出符号
                for ch in SEP_TOLERATED:
                    self.assertNotIn(ch, scrub(seg))


class TestSegmentIntervals(unittest.TestCase):
    def test_count_and_first_is_zero_by_default(self):
        d = segment_intervals(3)
        self.assertEqual(len(d), 3)
        self.assertEqual(d[0], 0.0)  # 默认不做首条延迟

    def test_range_respected(self):
        for _ in range(200):
            d = segment_intervals(4)
            for x in d[1:]:
                self.assertGreaterEqual(x, DEFAULT_INTERVAL[0])
                self.assertLessEqual(x, DEFAULT_INTERVAL[1])

    def test_leading_delay_matches_astrbot(self):
        """leading_delay=True 时逐字复刻 AstrBot：连第一条也延迟。"""
        for _ in range(100):
            d = segment_intervals(3, leading_delay=True)
            for x in d:
                self.assertGreaterEqual(x, DEFAULT_INTERVAL[0])
                self.assertLessEqual(x, DEFAULT_INTERVAL[1])

    def test_injected_rng_is_used(self):
        d = segment_intervals(3, rng=lambda lo, hi: 9.9)
        self.assertEqual(d, [0.0, 9.9, 9.9])

    def test_zero_and_negative(self):
        self.assertEqual(segment_intervals(0), [])
        self.assertEqual(segment_intervals(-5), [])

    def test_reversed_interval_is_normalised(self):
        d = segment_intervals(2, interval=(3.5, 1.5), rng=lambda lo, hi: lo)
        self.assertEqual(d[1], 1.5)


class TestParseInterval(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(parse_interval("1.5,3.5"), (1.5, 3.5))
        self.assertEqual(parse_interval("1.5, 3.5"), (1.5, 3.5))
        self.assertEqual(parse_interval("1.5-3.5"), (1.5, 3.5))
        self.assertEqual(parse_interval([1.5, 3.5]), (1.5, 3.5))
        self.assertEqual(parse_interval("2"), (2.0, 2.0))

    def test_defaults_on_garbage(self):
        self.assertEqual(parse_interval(None), DEFAULT_INTERVAL)
        self.assertEqual(parse_interval(""), DEFAULT_INTERVAL)
        self.assertEqual(parse_interval("abc"), DEFAULT_INTERVAL)


class TestSeparatorTolerance(unittest.TestCase):
    """换符号后的容错：旧 `※`(U+203B) 与变体 `⸮`(U+2E2E) 也必须能分段并被清掉。

    动机：模型可能沿用旧话术（人格里长期写的是 ※），或者输出形近字。
    容错集合一旦缺失，症状是「她发了一整段没切开」或「分隔符露在消息里」——
    两种都很难归因，所以在切分层直接兜住。
    """

    def test_legacy_asterisk_still_splits_and_is_cleaned(self):
        self.assertEqual(split_text("第一句※第二句※第三句"), ["第一句", "第二句", "第三句"])

    def test_variant_reversed_question_mark_is_cleaned(self):
        self.assertEqual(split_text("甲⸮乙"), ["甲", "乙"])

    def test_mixed_separators_all_cleaned(self):
        self.assertEqual(split_text("甲⁂乙※丙⸮丁"), ["甲", "乙", "丙", "丁"])

    def test_primary_separator_is_in_regex_and_cleanup(self):
        """⁂ 是主符号，必须真的出现在切分正则与清理规则里（防止只改了一处）。"""
        self.assertIn("⁂", DEFAULT_REGEX)
        self.assertIn("⁂", DEFAULT_CLEANUP_RULE)
        for ch in SEP_TOLERATED:
            self.assertIn(ch, DEFAULT_REGEX, f"{ch!r} 不在切分正则里")
            self.assertIn(ch, DEFAULT_CLEANUP_RULE, f"{ch!r} 不在清理规则里")

    def test_regex_has_no_sentence_punctuation(self):
        """切分正则里**不许**留句末标点（否则「不含 ⁂ 的长文一条不切」做不到）。"""
        for ch in "。？！~…":
            self.assertNotIn(ch, DEFAULT_REGEX, f"{ch!r} 又回到切分正则里了")

    def test_no_separator_leaks_into_output(self):
        """无论哪一路，发出去的正文里都不许有分隔符。"""
        for text in ("甲⁂乙※丙⸮丁", "只有一句※", "a⁂b"):
            for seg in split_text(text):
                for ch in SEP_TOLERATED:
                    self.assertNotIn(ch, seg, f"{ch!r} 漏进了 {seg!r}")

    def test_known_tradeoff_asterisk_in_document_still_splits(self):
        """⚠️ 已知取舍（不是 bug，是容错的必然代价，已在文档登记）：

        `※` 留在**切分正则**里当容错 → 正文里真出现 `※` 的文档会被切成多条。
        收窄（`※` 只留 cleanup、不进 regex）会让「旧话术漏出 ※」整段不切；
        权衡后保留现状。好处是符号一定会被清掉，主人看不到。
        """
        doc = "这是一段文档，里面有 ※ 符号，讲的是注释写法。"
        got = split_text(doc)
        self.assertGreater(len(got), 1, "按当前 spec，※ 仍会切分 —— 若这里变成 1 段，说明已切成正则收窄版")
        for seg in got:
            self.assertNotIn("※", seg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
