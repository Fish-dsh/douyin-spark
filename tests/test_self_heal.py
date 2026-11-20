"""self_heal 门面的单测: 开关 → 分类 → 策略 → 告警去重。

分类表与策略表本身在 tests/test_recovery.py 里测(那里是纯函数, 覆盖面更全),
这里只管门面: 读哪个开关、什么时候告警、告警失败会不会连坐。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import self_heal


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
