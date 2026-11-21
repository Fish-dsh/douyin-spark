"""src/status.py: 一句话状态。

用 tmp 日志喂假记录, 不碰真 runtime —— 这个模块本身只读, 测试也保持只读。
"""

from __future__ import annotations

import json
import unittest
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from src import status, weekly_report

NOW = datetime(2026, 11, 21, 9, 0)  # 昨晚那轮已结束, 今天还没到窗口

GOOD_NIGHT = """
[2026-11-20 00:03:00] [perf] profile=low
[2026-11-20 00:05:00] 【好友A】发送成功, state.json 已更新
[2026-11-20 00:06:00] 【好友B】发送成功, state.json 已更新
[2026-11-20 00:08:00] ==== 账号 main 完成: 发送 2 个 / 失败 0 个, 总耗时 2m11.8s ====
[2026-11-20 00:10:00] [timing] 本次运行总耗时 5m30.0s
"""

BAD_NIGHT = """
[2026-11-20 00:03:00] 【好友A】发送成功, state.json 已更新
[2026-11-20 00:05:00] 【好友B】发送未确认成功
[2026-11-20 00:06:00] 【好友C】处理失败(本地策略: 重试), 转入补扫
[2026-11-20 00:08:00] ==== 账号 main 完成: 发送 1 个 / 失败 2 个, 总耗时 3m00.0s ====
"""

EXPIRED_NIGHT = """
[2026-11-19 00:03:00] 【好友A】处理失败(登录态失效, 本轮未处理)
[2026-11-19 00:04:00] ==== 账号 main 完成: 发送 0 个 / 失败 1 个, 总耗时 1m00.0s ====
"""

HALF_DEAD = """
[2026-11-20 00:03:00] [perf] profile=low
[2026-11-20 00:04:00] 发现阶段开始
"""


class StatusCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.log = root / "spark.log"
        self.runtime = root
        self._orig = (
            weekly_report.LOG_FILE,
            status.RUNTIME_DIR,
            status.CONFIG_FILE,
        )
        weekly_report.LOG_FILE = self.log
        status.RUNTIME_DIR = root
        status.CONFIG_FILE = root / "config.json"

    def tearDown(self) -> None:
        weekly_report.LOG_FILE, status.RUNTIME_DIR, status.CONFIG_FILE = self._orig

    def write(self, text: str) -> None:
        self.log.write_text(text.strip() + "\n", encoding="utf-8")

    def test_good_night_counts(self) -> None:
        self.write(GOOD_NIGHT)
        data = status.build(NOW)
        self.assertTrue(data["has_record"])
        self.assertEqual(data["night"], "2026-11-20")
        self.assertEqual(data["night_label"], "昨晚")
        self.assertEqual((data["sent"], data["failed"]), (2, 0))
        self.assertEqual(data["failures"], [])
        self.assertEqual(data["timing"], "5m30.0s")
        self.assertEqual(data["advice"], [])

    def test_failures_carry_reason_and_advice(self) -> None:
        self.write(BAD_NIGHT)
        data = status.build(NOW)
        self.assertEqual((data["sent"], data["failed"]), (1, 2))
        friends = {f["friend"]: f["reason"] for f in data["failures"]}
        self.assertEqual(friends["好友B"], "发送未确认成功")
        self.assertIn("重试", friends["好友C"])
        # 两条失败各命中一条建议, 且不重复
        joined = " ".join(data["advice"])
        self.assertIn("截图", joined)
        self.assertIn("本地重试", joined)

    def test_login_expired_advice(self) -> None:
        self.write(EXPIRED_NIGHT)
        data = status.build(NOW)
        # 11-19 是两天前
        self.assertEqual(data["night_label"], "2 天前")
        self.assertTrue(any("login.py" in tip for tip in data["advice"]))

    def test_half_dead_night_is_skipped(self) -> None:
        # 11-20 那夜只有开头没有完成行, 应回退到 11-19
        self.write(HALF_DEAD + EXPIRED_NIGHT)
        data = status.build(NOW)
        self.assertEqual(data["night"], "2026-11-19")

    def test_no_record(self) -> None:
        self.log.write_text("", encoding="utf-8")
        data = status.build(NOW)
        self.assertFalse(data["has_record"])
        self.assertIsNone(data["night"])
        self.assertEqual(len(data["advice"]), 2)
        self.assertIn("cron", data["advice"][0])

    def test_stale_record_beyond_lookback(self) -> None:
        self.log.write_text(
            "[2026-10-01 00:03:00] ==== 账号 main 完成: 发送 1 个 / 失败 0 个 ====\n",
            encoding="utf-8",
        )
        data = status.build(NOW)
        self.assertFalse(data["has_record"])
        self.assertEqual(data["last_log_date"], "2026-10-01")

    def test_render_mentions_numbers(self) -> None:
        self.write(BAD_NIGHT)
        text = status.render(status.build(NOW))
        self.assertIn("昨晚(2026-11-20)", text)
        self.assertIn("发送 1 / 3, 失败 2", text)
        self.assertIn("好友B", text)
        self.assertIn("→", text)

    def test_json_round_trip(self) -> None:
        self.write(GOOD_NIGHT)
        data = status.build(NOW)
        again = json.loads(json.dumps(data, ensure_ascii=False))
        self.assertEqual(again["sent"], 2)

    def test_heartbeat_age(self) -> None:
        self.write(GOOD_NIGHT)
        (self.runtime / "heartbeat.json").write_text(
            json.dumps({"ts": NOW.timestamp() - 120, "stage": "send", "iso": "x"}),
            encoding="utf-8",
        )
        data = status.build(NOW)
        self.assertEqual(data["heartbeat"]["age_min"], 2)

    def test_window_from_config(self) -> None:
        self.write(GOOD_NIGHT)
        status.CONFIG_FILE.write_text(
            json.dumps({"send_window": {"start": "23:40", "end": "00:20"}}),
            encoding="utf-8",
        )
        self.assertEqual(status.build(NOW)["window"], ["23:40", "00:20"])

    def test_main_returns_zero(self) -> None:
        self.write(GOOD_NIGHT)
        self.assertEqual(status.main([]), 0)
        self.assertEqual(status.main(["--json"]), 0)


class LabelCase(unittest.TestCase):
    def test_label_today(self) -> None:
        now = datetime(2026, 11, 21, 0, 30)
        self.assertEqual(status._label(date(2026, 11, 21), now), "今天凌晨")
        self.assertEqual(status._label(date(2026, 11, 20), now), "昨晚")
        self.assertEqual(status._label(date(2026, 11, 18), now), "3 天前")


if __name__ == "__main__":
    unittest.main()
