"""logutil 入口判定单测：非生产入口（单测 / diagnostics 探针）不得写生产监控面。"""

from __future__ import annotations

import types
import unittest
from unittest.mock import patch

import logutil


class TestOfflineEntry(unittest.TestCase):
    """_offline_entry 判据取自 `__main__.__file__`，四类入口各钉一条。"""

    def _judge(self, main_file: str) -> bool:
        fake = types.ModuleType("__main__")
        fake.__file__ = main_file
        with patch.dict("sys.modules", {"__main__": fake}):
            return logutil._offline_entry()

    def test_production_entries_are_not_offline(self):
        self.assertFalse(self._judge("/opt/douyin-spark/src/scheduler.py"))
        self.assertFalse(self._judge("/opt/douyin-spark/src/health_check.py"))

    def test_unittest_and_pytest_mains_are_offline(self):
        self.assertTrue(self._judge("/usr/lib/python3.11/unittest/__main__.py"))
        self.assertTrue(self._judge("/x/site-packages/_pytest/__main__.py"))

    def test_direct_test_module_is_offline(self):
        self.assertTrue(self._judge("/opt/douyin-spark/tests/test_scheduler.py"))

    def test_diagnostics_probe_is_offline(self):
        """探针与主流程共用 discovery/sender 代码路径，落生产 spark.log 会伪造收尾行。"""
        self.assertTrue(self._judge("/opt/douyin-spark/diagnostics/probe_relight3.py"))

    def test_entry_without_main_file_is_not_offline(self):
        fake = types.ModuleType("__main__")
        with patch.dict("sys.modules", {"__main__": fake}):
            self.assertFalse(logutil._offline_entry())


if __name__ == "__main__":
    unittest.main()
