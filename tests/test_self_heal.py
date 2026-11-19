"""self_heal / recovery 单测。

覆盖降级后的两层: `recovery`(纯策略表) 与 `self_heal`(门面 + 告警去重)。
运行: .venv/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import recovery, self_heal


class TestClassify(unittest.TestCase):
    """错误分类: 判定顺序即优先级, 越"具体"的越先命中。"""

    def test_render_hang_first(self):
        # 该串同时含 "超时"(transient) 与 "渲染挂死", 必须判为渲染挂死
        self.assertEqual(
            recovery.classify(
                "process_friend", "会话列表超时且页面 body 全空(渲染挂死)"
            ),
            recovery.RENDER_HANG,
        )

    def test_selector_on_discovery(self):
        self.assertEqual(
            recovery.classify(
                "discover", "火花检测四策略全部未命中, 疑似抖音页面改版导致选择器失效"
            ),
            recovery.SELECTOR,
        )

    def test_selector_on_coverage_gap(self):
        self.assertEqual(
            recovery.classify("discover", "发现覆盖缺失: 甲, 乙"), recovery.SELECTOR
        )

    def test_transient_timeout(self):
        self.assertEqual(
            recovery.classify("process_friend", "Page.goto: Timeout 30000ms exceeded"),
            recovery.TRANSIENT,
        )

    def test_transient_target_closed(self):
        self.assertEqual(
            recovery.classify("process_friend", "Target closed"), recovery.TRANSIENT
        )

    def test_transient_unconfirmed_send(self):
        # 静默失败(confirm_send 返回 fail 而非抛异常)必须值得重试
        self.assertEqual(
            recovery.classify("process_friend", "发送未确认: 好友A"), recovery.TRANSIENT
        )

    def test_resource_oom(self):
        self.assertEqual(
            recovery.classify("process_friend", "Out of memory"), recovery.RESOURCE
        )

    def test_login(self):
        self.assertEqual(recovery.classify("process_friend", "未登录"), recovery.LOGIN)

    def test_streak_gap_is_unknown(self):
        # 漏发无法在本地恢复 → 未知类 → 告警, 不臆测重试
        self.assertEqual(
            recovery.classify("streak_gap", "昨日 3 个待续好友未发送(账号 main): 甲"),
            recovery.UNKNOWN,
        )

    def test_unknown_default(self):
        self.assertEqual(
            recovery.classify("process_friend", "莫名其妙"), recovery.UNKNOWN
        )


class TestPolicy(unittest.TestCase):
    def test_mapping(self):
        self.assertEqual(recovery.policy(recovery.TRANSIENT), recovery.RETRY)
        self.assertEqual(recovery.policy(recovery.SELECTOR), recovery.RELOAD_RETRY)
        self.assertEqual(
            recovery.policy(recovery.RENDER_HANG), recovery.RESTART_SESSION
        )
        self.assertEqual(recovery.policy(recovery.RESOURCE), recovery.RESTART_SESSION)
        self.assertEqual(recovery.policy(recovery.LOGIN), recovery.ALERT)
        self.assertEqual(recovery.policy(recovery.UNKNOWN), recovery.ALERT)

    def test_unknown_category_alerts(self):
        self.assertEqual(recovery.policy("不存在的类别"), recovery.ALERT)

    def test_should_retry(self):
        for action in (recovery.RETRY, recovery.RELOAD_RETRY, recovery.RESTART_SESSION):
            self.assertTrue(recovery.should_retry(action), action)
        for action in (recovery.SKIP, recovery.ALERT):
            self.assertFalse(recovery.should_retry(action), action)


class TestSelfHealFacade(unittest.TestCase):
    """门面: 开关 → 分类 → 策略 → 告警; 告警账本指向临时目录, 不碰真实 runtime。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._orig_alert_file = self_heal._ALERT_FILE
        self_heal._ALERT_FILE = Path(self._tmp.name) / ".heal_alerted"
        self.addCleanup(self._restore)

    def _restore(self):
        self_heal._ALERT_FILE = self._orig_alert_file

    def test_env_disables(self):
        with patch.dict("os.environ", {"SPARK_SELF_HEAL": "0"}):
            self.assertFalse(self_heal.self_heal("process_friend", "Timeout"))

    def test_env_enables(self):
        with patch.dict("os.environ", {"SPARK_SELF_HEAL": "1"}):
            self.assertTrue(self_heal.self_heal("process_friend", "Timeout"))

    def test_config_disables(self):
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("src.self_heal._config", return_value={"self_heal": False}),
        ):
            self.assertFalse(self_heal.self_heal("process_friend", "Timeout"))

    def test_default_enabled_without_config(self):
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("src.self_heal._config", return_value={}),
        ):
            self.assertTrue(self_heal.self_heal("process_friend", "Timeout"))

    def test_transient_retries_without_alert(self):
        with (
            patch.dict("os.environ", {"SPARK_SELF_HEAL": "1"}),
            patch("src.self_heal._alert") as m_alert,
        ):
            self.assertTrue(self_heal.self_heal("process_friend", "Timeout"))
        m_alert.assert_not_called()

    def test_selector_retries(self):
        with patch.dict("os.environ", {"SPARK_SELF_HEAL": "1"}):
            self.assertTrue(self_heal.self_heal("discover", "四策略全部未命中"))

    def test_unknown_skips_and_alerts(self):
        with (
            patch.dict("os.environ", {"SPARK_SELF_HEAL": "1"}),
            patch("src.self_heal._alert") as m_alert,
        ):
            self.assertFalse(self_heal.self_heal("streak_gap", "昨日未发送"))
        m_alert.assert_called_once()

    def test_login_skips_and_alerts(self):
        with (
            patch.dict("os.environ", {"SPARK_SELF_HEAL": "1"}),
            patch("src.self_heal._alert") as m_alert,
        ):
            self.assertFalse(self_heal.self_heal("process_friend", "未登录"))
        m_alert.assert_called_once()

    def test_screenshot_path_accepted_for_compat(self):
        with patch.dict("os.environ", {"SPARK_SELF_HEAL": "1"}):
            self.assertTrue(
                self_heal.self_heal("discover", "未命中", screenshot_path="/tmp/x.png")
            )

    def test_alert_dedup_same_signature_same_day(self):
        with (
            patch.dict("os.environ", {"SPARK_SELF_HEAL": "1"}),
            patch("src.health_check.notify") as m_notify,
        ):
            self_heal.self_heal("streak_gap", "未知故障A")
            self_heal.self_heal("streak_gap", "未知故障A")
        self.assertEqual(m_notify.call_count, 1, "同一签名每日只告警一次")

    def test_alert_distinct_signatures_both_fire(self):
        with (
            patch.dict("os.environ", {"SPARK_SELF_HEAL": "1"}),
            patch("src.health_check.notify") as m_notify,
        ):
            self_heal.self_heal("streak_gap", "未知故障A")
            self_heal.self_heal("streak_gap", "未知故障B")
        self.assertEqual(m_notify.call_count, 2)

    def test_notify_failure_never_breaks(self):
        with (
            patch.dict("os.environ", {"SPARK_SELF_HEAL": "1"}),
            patch("src.health_check.notify", side_effect=RuntimeError("boom")),
        ):
            self.assertFalse(self_heal.self_heal("streak_gap", "未知故障C"))


if __name__ == "__main__":
    unittest.main()
