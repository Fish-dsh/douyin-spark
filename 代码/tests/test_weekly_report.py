"""weekly_report(周报)单测: 日志解析、7 晚窗口、补救窗口天数、不推送。"""

from __future__ import annotations

import gzip
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

from src import weekly_report

TODAY = date(2026, 11, 1)  # 周日; 已结束的夜晚 = 10-31 往前


def _d(month: int, day: int) -> date:
    return date(2026, month, day)


def _write(path: Path, lines: list[str]) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _complete(day: date, account: str, sent: int, failed: int) -> str:
    return (
        f"[{day.isoformat()} 00:10:00] ==== 账号 {account} 完成: "
        f"发送 {sent} 个 / 失败 {failed} 个, 总耗时 9m11.2s ===="
    )


def _ok(day: date, name: str) -> str:
    return f"[{day.isoformat()} 00:04:33] 【{name}】发送成功, state.json 已更新"


def _fail(day: date, name: str) -> str:
    return f"[{day.isoformat()} 00:06:14] 【{name}】发送未确认成功"


class TestLogFiles(unittest.TestCase):
    def test_history_archive_is_not_part_of_rotation_chain(self):
        """一次性的历史归档(spark.log.pre-sync-*)不能混进轮转链, 否则覆盖率失效。"""
        with tempfile.TemporaryDirectory() as tmp:
            base = _write(Path(tmp) / "spark.log", [_complete(_d(11, 1), "main", 1, 0)])
            (Path(tmp) / "spark.log.pre-sync-20260927").write_text(
                _complete(_d(8, 28), "main", 1, 0) + "\n", encoding="utf-8"
            )
            (Path(tmp) / "spark.log.7.gz").write_bytes(
                gzip.compress((_complete(_d(10, 27), "main", 1, 0) + "\n").encode())
            )
            with mock.patch.object(weekly_report, "LOG_FILE", base):
                names = [p.name for p in weekly_report.log_files()]
                self.assertEqual(
                    weekly_report.coverage_start(), _d(10, 27)
                )  # 归档被排除 → 起点是轮转链最早的一天
        self.assertIn("spark.log.7.gz", names)
        self.assertNotIn("spark.log.pre-sync-20260927", names)


class TestCollect(unittest.TestCase):
    def test_parses_accounts_and_friend_events(self):
        day = _d(10, 31)
        lines = [
            _complete(day, "main", 4, 0),
            _ok(day, "好友D"),
            _fail(day, "饭饭"),
            "[2026-10-31 00:02:27] ==================== 账号: main ====================",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp) / "spark.log", lines)
            days = weekly_report.collect(day, day, [path])
        self.assertEqual(days[day].accounts["main"], (4, 0))
        self.assertEqual(days[day].successes, ["好友D"])
        self.assertEqual(days[day].failures, ["饭饭"])
        self.assertEqual(days[day].line_count, 4)

    def test_reads_gz_rotation(self):
        day = _d(10, 30)
        lines = [_complete(day, "main", 3, 0)]
        with tempfile.TemporaryDirectory() as tmp:
            gz = Path(tmp) / "spark.log.3.gz"
            with gzip.open(gz, "wt", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
            days = weekly_report.collect(day, day, [gz])
        self.assertEqual(days[day].accounts["main"], (3, 0))

    def test_ignores_lines_outside_window_and_garbage(self):
        path = _write(
            Path(tempfile.mkdtemp()) / "spark.log",
            [
                "无日期前缀的行",
                _complete(_d(10, 1), "main", 1, 0),
            ],
        )
        days = weekly_report.collect(_d(10, 20), _d(10, 31), [path])
        self.assertEqual(days, {})

    def test_no_pending_and_skipped_count_as_finished(self):
        day = _d(10, 31)
        lines = [
            f"[{day.isoformat()} 00:05:00] 今日({day.isoformat()})没有需要续火花的好友, 账号 main 结束",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp) / "spark.log", lines)
            days = weekly_report.collect(day, day, [path])
        self.assertEqual(days[day].finished, {"main"})


class TestBuildReport(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(
            weekly_report, "_account_names", return_value=["main"]
        )
        self.addCleanup(patcher.stop)
        self.account_names = patcher.start()
        self.start, self.end = _d(10, 25), _d(10, 31)

    def _week_ok(self) -> dict[date, weekly_report.Day]:
        days: dict[date, weekly_report.Day] = {}
        for i in range(7):
            day = self.start + timedelta(days=i)
            bucket = weekly_report.Day()
            bucket.accounts["main"] = (4, 0)
            bucket.successes = ["好友D"]
            days[day] = bucket
        return days

    def test_all_ok_report(self):
        report = weekly_report.build_report(
            TODAY, self.start, self.end, self._week_ok()
        )
        self.assertIn("这 7 晚", report)
        self.assertIn("✅ 全部正常", report)
        self.assertIn("需要你动手的：没有", report)

    def test_missing_night_reported(self):
        days = self._week_ok()
        del days[_d(10, 29)]
        report = weekly_report.build_report(TODAY, self.start, self.end, days)
        self.assertIn("完全没有日志", report)
        self.assertIn("10-29", report)

    def test_failed_friend_shows_grace_days(self):
        days = self._week_ok()
        fail_day = _d(10, 29)
        days[fail_day].accounts["main"] = (3, 1)
        days[fail_day].failures = ["饭饭"]
        report = weekly_report.build_report(TODAY, self.start, self.end, days)
        self.assertIn("10-29 有 1 个没发出去", report)
        self.assertIn("饭饭", report)
        self.assertIn("已 3 天", report)
        self.assertIn("还剩 4 天", report)

    def test_friend_beyond_grace_window(self):
        days = self._week_ok()
        fail_day = _d(10, 20)
        days[fail_day] = weekly_report.Day(accounts={"main": (3, 1)}, failures=["饭饭"])
        report = weekly_report.build_report(TODAY, self.start, self.end, days)
        self.assertIn("超过约 7 天", report)

    def test_friend_later_rescued_not_reported(self):
        days = self._week_ok()
        fail_day = _d(10, 29)
        days[fail_day].accounts["main"] = (3, 1)
        days[fail_day].failures = ["饭饭"]
        # 次日又发送成功 → 不算仍断着
        days[_d(10, 30)].successes = ["好友D", "饭饭"]
        report = weekly_report.build_report(TODAY, self.start, self.end, days)
        self.assertNotIn("👉 饭饭", report)

    def test_unfinished_account_reported(self):
        days = self._week_ok()
        days[_d(10, 30)].accounts = {}
        report = weekly_report.build_report(TODAY, self.start, self.end, days)
        self.assertIn("没跑完", report)


class TestRun(unittest.TestCase):
    def test_run_writes_report_and_never_notifies(self):
        today = datetime.now().date()
        day = today - timedelta(days=1)
        with tempfile.TemporaryDirectory() as tmp:
            log = _write(
                Path(tmp) / "spark.log",
                [_complete(day, "main", 2, 0), _ok(day, "好友D")],
            )
            report_file = Path(tmp) / "weekly_report.txt"
            with (
                mock.patch.object(weekly_report, "LOG_FILE", log),
                mock.patch.object(weekly_report, "REPORT_FILE", report_file),
                mock.patch.object(
                    weekly_report, "_account_names", return_value=["main"]
                ),
                mock.patch("src.health_check.notify") as notify,
            ):
                self.assertEqual(weekly_report.run(days_back=7), 0)
            written = report_file.read_text(encoding="utf-8")
        self.assertIn("【续火花周报】", written)
        # 只有 1 晚日志 → 窗口必须收缩到覆盖起点, 不能诬告其余 6 晚"没跑"
        self.assertIn("这 2 晚", written)
        self.assertIn("✅ 全部正常", written)
        self.assertIn("更早的夜晚没有记录", written)
        notify.assert_not_called()  # 决定性约束: 周报只落盘, 绝不推送

    def test_last_night_is_counted(self):
        """最近一晚(日志日期=今天)必须计入, 不能被"今天还没到"逻辑吞掉。"""
        today = datetime.now().date()
        with tempfile.TemporaryDirectory() as tmp:
            log = _write(
                Path(tmp) / "spark.log",
                [
                    _complete(today - timedelta(days=1), "main", 4, 0),
                    _complete(today, "main", 5, 0),
                ],
            )
            report_file = Path(tmp) / "weekly_report.txt"
            with (
                mock.patch.object(weekly_report, "LOG_FILE", log),
                mock.patch.object(weekly_report, "REPORT_FILE", report_file),
                mock.patch.object(
                    weekly_report, "_account_names", return_value=["main"]
                ),
            ):
                self.assertEqual(weekly_report.run(days_back=7), 0)
            written = report_file.read_text(encoding="utf-8")
        self.assertIn("共续上 9 次", written)
        self.assertNotIn("完全没有日志", written)

    def test_missing_night_inside_coverage_still_reported(self):
        """日志覆盖得够久时, 中间的空档仍必须报出来(不能被覆盖逻辑吞掉)。"""
        today = datetime.now().date()
        oldest = today - timedelta(days=9)
        with tempfile.TemporaryDirectory() as tmp:
            log = _write(
                Path(tmp) / "spark.log",
                [
                    _complete(oldest, "main", 1, 0),
                    _complete(today - timedelta(days=2), "main", 1, 0),
                    _complete(today - timedelta(days=1), "main", 1, 0),
                ],
            )
            report_file = Path(tmp) / "weekly_report.txt"
            with (
                mock.patch.object(weekly_report, "LOG_FILE", log),
                mock.patch.object(weekly_report, "REPORT_FILE", report_file),
                mock.patch.object(
                    weekly_report, "_account_names", return_value=["main"]
                ),
            ):
                self.assertEqual(weekly_report.run(days_back=7), 0)
            written = report_file.read_text(encoding="utf-8")
        self.assertIn("这 7 晚", written)
        self.assertIn("完全没有日志", written)
        self.assertNotIn("更早的夜晚没有记录", written)


if __name__ == "__main__":
    unittest.main()
