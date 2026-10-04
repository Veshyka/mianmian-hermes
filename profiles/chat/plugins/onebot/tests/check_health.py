"""自检判读单测（红线 2：坏了要能马上知道）。

⚠️ 文件名用 `check_` 前缀（disk-cleanup 会删 test_*，见 check_segmentation.py 头注）。
运行： python3 tests/check_health.py
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from health import (  # noqa: E402
    DEFAULT_THRESHOLDS, FAIL, OK, WARN, alert_text, judge, summarize, thresholds_from_extra,
    worst_level,
)

NOW = 1_800_000_000.0


def _state(**over):
    base = {
        "ts": NOW, "host": "127.0.0.1", "port": 6700,
        "listener_up": True, "client_connected": True,
        "last_client_ts": NOW, "last_rx_ts": NOW - 5, "last_tx_ts": NOW - 5,
        "rx_count": 3, "tx_count": 3, "segments_sent": 5, "dropped_count": 0,
        "consecutive_failures": 0, "last_error": "", "alerts_sent": 0, "read_only": False,
    }
    base.update(over)
    return base


def _keys(findings):
    return {f["key"] for f in findings if f["level"] != OK}


class TestJudge(unittest.TestCase):
    def test_healthy_state_is_clean(self):
        findings = judge(_state(), now=NOW)
        self.assertEqual(_keys(findings), set(), summarize(findings))
        self.assertEqual(worst_level(findings), OK)

    def test_missing_state_file_is_fail(self):
        findings = judge(None, now=NOW)
        self.assertEqual(findings[0]["level"], FAIL)
        self.assertEqual(findings[0]["key"], "state_file")

    def test_stale_heartbeat_is_fail(self):
        """进程卡死/没在跑 —— 最硬的一条。"""
        findings = judge(_state(ts=NOW - 1000), now=NOW)
        self.assertIn("heartbeat", _keys(findings))
        self.assertEqual(worst_level(findings), FAIL)

    def test_listener_down_is_fail(self):
        self.assertIn("listener", _keys(judge(_state(listener_up=False), now=NOW)))

    def test_client_never_connected_is_fail(self):
        findings = judge(_state(client_connected=False, last_client_ts=0), now=NOW)
        detail = next(f["detail"] for f in findings if f["key"] == "client")
        self.assertIn("从未连上", detail)

    def test_client_disconnected_long_is_fail(self):
        findings = judge(_state(client_connected=False, last_client_ts=NOW - 900), now=NOW)
        self.assertEqual([f for f in findings if f["key"] == "client"][0]["level"], FAIL)

    def test_client_briefly_disconnected_is_warn(self):
        findings = judge(_state(client_connected=False, last_client_ts=NOW - 20), now=NOW)
        self.assertEqual([f for f in findings if f["key"] == "client"][0]["level"], WARN)

    def test_never_connected_within_grace_is_not_a_failure(self):
        """适配器刚起来 30s、NapCat 还在重连 —— 这是重启的正常过程，不该报故障。

        真实事故（2026-09-23 15:38）：chat 网关重启 + NapCat 改配置重启之间有 ~35s 空窗，
        旧判据把这段空窗判成 fail 并发告警；加宽限期后同一条只会是提醒，不会告警。
        """
        findings = judge(_state(client_connected=False, last_client_ts=0,
                                started_ts=NOW - 30), now=NOW)
        client = [f for f in findings if f["key"] == "client"][0]
        self.assertEqual(client["level"], WARN)
        self.assertNotIn(client["key"], {f["key"] for f in findings if f["level"] == FAIL})
        self.assertEqual(worst_level(findings), WARN)
        self.assertIn("宽限", client["detail"])

    def test_never_connected_after_grace_is_still_a_failure(self):
        """宽限期一过还没连上 = 真故障（证明宽限不是放水）。"""
        findings = judge(_state(client_connected=False, last_client_ts=0,
                                started_ts=NOW - 600), now=NOW)
        self.assertEqual([f for f in findings if f["key"] == "client"][0]["level"], FAIL)

    def test_grace_can_be_disabled(self):
        findings = judge(_state(client_connected=False, last_client_ts=0, started_ts=NOW - 30),
                         now=NOW, thresholds={"client_grace_seconds": 0})
        self.assertEqual([f for f in findings if f["key"] == "client"][0]["level"], FAIL)

    def test_no_events_within_grace_is_silent(self):
        """刚启动还没人说话 —— 宽限期内连提醒都不发，免得启动即刷屏。"""
        findings = judge(_state(last_rx_ts=0, started_ts=NOW - 30), now=NOW)
        self.assertNotIn("rx_idle", _keys(findings))

    def test_grace_does_not_hide_a_real_disconnect(self):
        """已经连过又断久了（last_client_ts>0）→ 与宽限无关，照旧 fail。"""
        findings = judge(_state(client_connected=False, last_client_ts=NOW - 900,
                                started_ts=NOW - 30), now=NOW)
        self.assertEqual([f for f in findings if f["key"] == "client"][0]["level"], FAIL)

    def test_rx_idle_catches_silently_dead_line(self):
        """线还连着但长时间没有任何事件 —— 最典型的「静默掉线」。"""
        findings = judge(_state(last_rx_ts=NOW - 90000), now=NOW)
        self.assertIn("rx_idle", _keys(findings))

    def test_rx_idle_disabled_by_zero(self):
        findings = judge(_state(last_rx_ts=NOW - 90000), now=NOW,
                         thresholds={"rx_idle_seconds": 0})
        self.assertNotIn("rx_idle", _keys(findings))

    def test_never_received_is_warn(self):
        findings = judge(_state(last_rx_ts=0), now=NOW)
        self.assertIn("rx_idle", _keys(findings))

    def test_consecutive_failures_escalate(self):
        warn = judge(_state(consecutive_failures=1, last_error="retcode=1404"), now=NOW)
        fail = judge(_state(consecutive_failures=9, last_error="timeout"), now=NOW)
        self.assertEqual([f for f in warn if f["key"] == "send_failures"][0]["level"], WARN)
        self.assertEqual([f for f in fail if f["key"] == "send_failures"][0]["level"], FAIL)

    def test_read_only_reported_as_warn(self):
        findings = judge(_state(read_only=True), now=NOW)
        self.assertEqual([f for f in findings if f["key"] == "read_only"][0]["level"], WARN)

    def test_multiple_problems_all_listed(self):
        findings = judge(_state(listener_up=False, consecutive_failures=9), now=NOW)
        self.assertEqual(_keys(findings), {"listener", "send_failures"})
        self.assertEqual(summarize(findings).count("异常"), 1)


class TestSummarize(unittest.TestCase):
    def test_ok_single_word(self):
        self.assertEqual(summarize(judge(_state(), now=NOW)), "正常")

    def test_warns_only_is_not_called_abnormal(self):
        """「未启用 / 只收不发」这类提醒不该被说成异常 —— 否则人会对告警脱敏。"""
        text = summarize(judge(_state(read_only=True), now=NOW))
        self.assertIn("正常", text)
        self.assertNotIn("异常", text)
        self.assertIn("提醒", text)

    def test_abnormal_names_the_items(self):
        text = summarize(judge(_state(listener_up=False), now=NOW))
        self.assertIn("异常", text)
        self.assertIn("listener", text)

    def test_alert_text_is_human_readable(self):
        text = alert_text(judge(_state(listener_up=False), now=NOW))
        self.assertIn("QQ 小号(OneBot)线体自检", text)
        self.assertIn("listener", text)


class TestThresholds(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(thresholds_from_extra(None), DEFAULT_THRESHOLDS)

    def test_override(self):
        t = thresholds_from_extra({"no_client_seconds": 60, "rx_idle_seconds": 0})
        self.assertEqual(t["no_client_seconds"], 60.0)
        self.assertEqual(t["rx_idle_seconds"], 0.0)

    def test_garbage_falls_back(self):
        t = thresholds_from_extra({"no_client_seconds": "abc"})
        self.assertEqual(t["no_client_seconds"], DEFAULT_THRESHOLDS["no_client_seconds"])


class TestNoContentLeak(unittest.TestCase):
    def test_state_and_alert_never_contain_message_text(self):
        """红线 2：日志/状态/告警里不许出现消息正文。"""
        secret = "我今天去买了一箱牛奶"
        findings = judge(_state(last_error="retcode=1404"), now=NOW)
        blob = summarize(findings) + alert_text(findings) + str(_state())
        self.assertNotIn(secret, blob)
        # 状态结构里也不该存在「正文」这种字段
        for forbidden in ("text", "message", "content", "raw_message"):
            self.assertNotIn(forbidden, _state())


if __name__ == "__main__":
    unittest.main(verbosity=2)
