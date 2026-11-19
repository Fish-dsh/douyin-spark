"""streak_history.py 单测: P5 发现覆盖对照、冷却、近3天已续好友提取。"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from src import streak_history


class TestCoverage(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self._olds = (
            streak_history.HISTORY_FILE,
            streak_history._COVERAGE_COOLDOWN_SEC,
        )
        streak_history.HISTORY_FILE = self.root / "runtime" / "streak_history.json"

    def tearDown(self):
        streak_history.HISTORY_FILE, streak_history._COVERAGE_COOLDOWN_SEC = self._olds
        self.tmpdir.cleanup()

    def _write_history(self, records: dict):
        # load_history(account) 按 per-account 文件读取(streak_history_main.json),
        # 必须写入 _history_file("main") 而非 HISTORY_FILE 本身。
        hf = streak_history._history_file("main")
        hf.parent.mkdir(parents=True, exist_ok=True)
        hf.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")

    def test_recent_renewed_only_discovered_and_sent(self):
        from datetime import datetime, timedelta

        d1 = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        d2 = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
        self._write_history(
            {
                d1: {
                    "discovered": ["甲", "乙"],
                    "sent": ["甲"],
                    "failed": ["乙"],
                },
                d2: {
                    "discovered": ["丙"],
                    "sent": ["丙"],
                    "failed": [],
                },
            }
        )
        names = streak_history.recent_renewed_friends("main", days=3)
        # 按日期升序(d2 丙 在 d1 甲 之前), 只 discovered∩sent, 已漏发的乙不算
        self.assertEqual(names, ["丙", "甲"], "只 discovered∩sent, 已漏发的乙不算")

    def test_recent_renewed_excludes_today(self):
        from datetime import datetime

        today = datetime.now().strftime("%Y-%m-%d")
        self._write_history(
            {today: {"discovered": ["甲"], "sent": ["甲"], "failed": []}}
        )
        self.assertEqual(streak_history.recent_renewed_friends("main", days=3), [])

    def test_check_recent_coverage_missing(self):
        from datetime import datetime, timedelta

        d1 = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        self._write_history(
            {
                d1: {
                    "discovered": ["甲", "乙"],
                    "sent": ["甲", "乙"],
                    "failed": [],
                }
            }
        )
        missing = streak_history.check_recent_coverage("main", ["甲"])
        self.assertEqual(missing, ["乙"])

    def test_check_recent_coverage_sent_today_not_missing(self):
        from datetime import datetime, timedelta

        d1 = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        self._write_history({d1: {"discovered": ["甲"], "sent": ["甲"], "failed": []}})
        # 今日已发送的也不算缺失(today_seen 传入含已发送名单)
        self.assertEqual(streak_history.check_recent_coverage("main", ["甲"]), [])

    def test_no_history_no_missing(self):
        self.assertEqual(streak_history.check_recent_coverage("main", ["甲"]), [])

    def test_renamed_friend_is_not_missing(self):
        """改名漂移不算漏检(2026-10-07: 精确比对把改名判成列表漏检, 熔断整账号发送)。"""
        from datetime import datetime, timedelta

        d1 = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        self._write_history(
            {d1: {"discovered": ["是个英语天才", "甲"], "sent": ["是个英语天才", "甲"]}}
        )
        # 今日列表里是加了前缀的新昵称
        missing = streak_history.check_recent_coverage(
            "main", ["小明是个英语天才", "甲"]
        )
        self.assertEqual(missing, [])

    def test_short_names_are_not_fuzzy_merged(self):
        """4 字以下不做模糊合并: 短名共用前后缀概率高, 宁缺勿误。"""
        from datetime import datetime, timedelta

        d1 = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        self._write_history({d1: {"discovered": ["小天"], "sent": ["小天"]}})
        self.assertEqual(
            streak_history.check_recent_coverage("main", ["大地天"]), ["小天"]
        )

    def test_unrelated_long_name_still_missing(self):
        from datetime import datetime, timedelta

        d1 = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        self._write_history({d1: {"discovered": ["好友E"], "sent": ["好友E"]}})
        self.assertEqual(
            streak_history.check_recent_coverage("main", ["两个美人"]), ["好友E"]
        )

    def test_coverage_cooldown_lifecycle(self):
        self.assertFalse(streak_history.coverage_heal_on_cooldown("main"))
        streak_history.record_coverage_heal("main")
        self.assertTrue(streak_history.coverage_heal_on_cooldown("main"))
        # 过期冷却
        f = streak_history._coverage_cooldown_file("main")
        f.write_text(str(time.time() - 86401), encoding="utf-8")
        self.assertFalse(streak_history.coverage_heal_on_cooldown("main"))

    def test_cooldown_is_per_account(self):
        streak_history.record_coverage_heal("main")
        self.assertFalse(streak_history.coverage_heal_on_cooldown("好友F"))


class TestCheckStreakHistory(unittest.TestCase):
    """漏发判定口径(2026-10-04 事故)。

    老口径 `今日discovered − 昨日sent` 会把**今天新形成的火花**判成漏发 ——
    新火花昨天不在待续名单里, 自然也不在昨天的 sent 里, 于是每天必然误报一次,
    每次都白烧一整轮 AI 自愈。正确口径是 `昨日discovered − 昨日sent`。
    """

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self._old = streak_history.HISTORY_FILE
        streak_history.HISTORY_FILE = self.root / "runtime" / "streak_history.json"

    def tearDown(self):
        streak_history.HISTORY_FILE = self._old
        self.tmpdir.cleanup()

    def _write_history(self, records: dict):
        hf = streak_history._history_file("main")
        hf.parent.mkdir(parents=True, exist_ok=True)
        hf.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def _days():
        from datetime import datetime, timedelta

        today = datetime.now().strftime("%Y-%m-%d")
        prev = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        return today, prev

    def test_new_spark_today_is_not_a_miss(self):
        """回归: 今天新出现的火花不是昨天漏发 —— 这是 10-04 白烧自愈的直接成因。"""
        today, prev = self._days()
        self._write_history(
            {
                prev: {"discovered": ["甲"], "sent": ["甲"], "failed": []},
                today: {"discovered": ["甲", "丹", "依旧"], "sent": [], "failed": []},
            }
        )
        missing, detail = streak_history.check_streak_history("main")
        self.assertEqual(missing, [], f"新火花不该被判漏发: {detail}")
        self.assertIn("无漏发", detail)

    def test_yesterday_discovered_but_not_sent_is_a_real_miss(self):
        today, prev = self._days()
        self._write_history(
            {
                prev: {
                    "discovered": ["甲", "乙"],
                    "sent": ["甲"],
                    "failed": ["乙"],
                },
                today: {"discovered": ["甲", "乙"], "sent": [], "failed": []},
            }
        )
        missing, detail = streak_history.check_streak_history("main")
        self.assertEqual(missing, ["乙"])
        self.assertIn("昨日漏发", detail)

    def test_no_previous_record_skips_comparison(self):
        today = self._days()[0]
        self._write_history({today: {"discovered": ["甲"], "sent": [], "failed": []}})
        missing, detail = streak_history.check_streak_history("main")
        self.assertEqual(missing, [])
        self.assertIn("无昨日记录", detail)

    def test_no_records_at_all(self):
        missing, detail = streak_history.check_streak_history("main")
        self.assertEqual(missing, [])
        self.assertIn("无今日记录", detail)

    def test_trigger_only_fires_self_heal_on_real_miss(self):
        """误报不能触发自愈: 新火花场景下 self_heal 一次都不该被调。"""
        from unittest.mock import Mock

        today, prev = self._days()
        self._write_history(
            {
                prev: {"discovered": ["甲"], "sent": ["甲"], "failed": []},
                today: {"discovered": ["甲", "丹"], "sent": [], "failed": []},
            }
        )
        fn = Mock(return_value=True)
        self.assertFalse(streak_history.trigger_if_gap_detected("main", fn))
        fn.assert_not_called()

        self._write_history(
            {
                prev: {"discovered": ["甲", "乙"], "sent": ["甲"], "failed": ["乙"]},
                today: {"discovered": ["甲", "乙"], "sent": [], "failed": []},
            }
        )
        self.assertTrue(streak_history.trigger_if_gap_detected("main", fn))
        fn.assert_called_once()


if __name__ == "__main__":
    unittest.main()
