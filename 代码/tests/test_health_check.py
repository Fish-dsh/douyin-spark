"""health_check.py 单测: 检查逻辑与告警行为(纯函数, 不依赖真实 runtime)。"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src import health_check


class TestHealthCheck(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.old = (
            health_check.STATE_FILE,
            health_check.LOG_FILE,
            health_check.CONFIG_FILE,
            health_check.FAIL_MARKER,
            health_check.PID_FILE,
        )
        health_check.STATE_FILE = self.root / "state.json"
        health_check.LOG_FILE = self.root / "spark.log"
        health_check.CONFIG_FILE = self.root / "config.json"
        health_check.FAIL_MARKER = self.root / ".health_failed"
        # 指向一个必不存在的路径: 否则在板子上(或本机有进程在跑时)跑单测,
        # main() 会走"运行中 → 直接 return 0"分支, 把本该断言 1 的用例变成 0。
        health_check.PID_FILE = self.root / ".spark.pid"

    def tearDown(self):
        (
            health_check.STATE_FILE,
            health_check.LOG_FILE,
            health_check.CONFIG_FILE,
            health_check.FAIL_MARKER,
            health_check.PID_FILE,
        ) = self.old
        self.tmpdir.cleanup()

    def _today(self):
        return health_check.today()

    def _write_state(self, day=None):
        health_check.STATE_FILE.write_text(
            json.dumps({"date": day or self._today(), "sent": []}), encoding="utf-8"
        )

    def _write_log(self, complete=True):
        lines = [f"[{self._today()} 00:00:01] [wait] 聊天列表已就绪"]
        if complete:
            # 生产真实格式(scheduler.py): 多账号串行收尾带账号名
            lines.append(
                f"[{self._today()} 00:00:10] ==== 账号 main 完成: 发送 1 个 / 失败 0 个 ===="
            )
        else:
            lines.append(f"[{self._today()} 00:00:10] [heal] 自愈后重试失败, 返回码 3")
        health_check.LOG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ---- 单项检查 ----

    def test_state_missing(self):
        ok, _ = health_check.check_state_date()
        self.assertFalse(ok)

    def test_state_stale_date(self):
        self._write_state(day="2000-01-01")
        ok, _ = health_check.check_state_date()
        self.assertFalse(ok)

    def test_state_today_ok(self):
        self._write_state()
        ok, _ = health_check.check_state_date()
        self.assertTrue(ok)

    def test_multi_account_state_all_ok(self):
        """多账号: 逐账号 state 文件校验, 全部 date==今天 才健康。"""
        health_check.CONFIG_FILE.write_text(
            json.dumps(
                {
                    "accounts": [
                        {"name": "a", "state_file": "state_a.json"},
                        {"name": "b"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        for fn in ("state_a.json", "state_b.json"):
            (self.root / fn).write_text(
                json.dumps({"date": self._today(), "sent": []}), encoding="utf-8"
            )
        ok, msg = health_check.check_state_date()
        self.assertTrue(ok, msg)

    def test_multi_account_state_stale_detected(self):
        """多账号: 非 main 账号 state 停在昨天必须 FAIL(静默漏检回归防护)。"""
        health_check.CONFIG_FILE.write_text(
            json.dumps({"accounts": [{"name": "a"}, {"name": "b"}]}),
            encoding="utf-8",
        )
        (self.root / "state_a.json").write_text(
            json.dumps({"date": self._today(), "sent": []}), encoding="utf-8"
        )
        (self.root / "state_b.json").write_text(
            json.dumps({"date": "2000-01-01", "sent": []}), encoding="utf-8"
        )
        ok, msg = health_check.check_state_date()
        self.assertFalse(ok)
        self.assertIn("[b]", msg)

    def test_log_missing(self):
        ok, _ = health_check.check_log_has_today()
        self.assertFalse(ok)

    def test_log_no_today(self):
        health_check.LOG_FILE.write_text("[2000-01-01 00:00:01] x\n", encoding="utf-8")
        ok, _ = health_check.check_log_has_today()
        self.assertFalse(ok)

    def test_log_today_complete(self):
        self._write_log(complete=True)
        ok, _ = health_check.check_log_has_today()
        self.assertTrue(ok)

    def test_log_today_no_pending(self):
        # 生产真实格式: 日期插在中间「今日(2026-08-15)没有需要续火花的好友」
        health_check.LOG_FILE.write_text(
            f"[{self._today()} 00:00:01] 今日({self._today()})没有需要续火花的好友, 程序结束\n",
            encoding="utf-8",
        )
        ok, _ = health_check.check_log_has_today()
        self.assertTrue(ok)

    def test_no_pending_mark_matches_production_line(self):
        """防漂移: 无待续标记必须是生产日志行的子串(scheduler.py 输出格式)。"""
        prod_line = f"[{self._today()} 00:00:01] 今日({self._today()})没有需要续火花的好友, 程序结束"
        self.assertIn(health_check._NO_PENDING_MARK, prod_line)

    def test_complete_mark_matches_production_line(self):
        """防漂移: 收尾标记/统计正则必须匹配生产日志行(多账号带账号名)。"""
        prod_line = f"[{self._today()} 00:00:10] ==== 账号 13800000002 完成: 发送 10 个 / 失败 2 个 ===="
        self.assertIn(health_check._COMPLETE_MARK, prod_line)
        self.assertIsNotNone(health_check._COMPLETE_RE.search(prod_line))
        self.assertEqual(
            health_check._ACCOUNT_RE.search(prod_line).group(1), "13800000002"
        )

    def test_log_multi_account_earlier_failure_detected(self):
        """多账号: 前一个账号漏发不能被后一个账号的正常收尾掩盖。"""
        self._write_state()
        day = self._today()
        lines = [
            f"[{day} 00:01:00] 【测试好友A】处理失败(不中断其他好友)",
            f"[{day} 00:02:00] ==== 账号 main 完成: 发送 2 个 / 失败 1 个 ====",
            f"[{day} 00:03:00] ==== 账号 13800000002 完成: 发送 10 个 / 失败 0 个 ====",
        ]
        health_check.LOG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        ok, msg = health_check.check_log_has_today()
        self.assertFalse(ok)
        self.assertIn("测试好友A", msg)

    def test_log_cold_restart_uses_last_stats_per_account(self):
        """同一账号冷启动重跑: 取最后一次统计, 自愈重试成功后不误报。"""
        self._write_state()
        day = self._today()
        lines = [
            f"[{day} 00:01:00] ==== 账号 main 完成: 发送 0 个 / 失败 3 个 ====",
            f"[{day} 00:05:00] ==== 账号 main 完成: 发送 3 个 / 失败 0 个 ====",
        ]
        health_check.LOG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        ok, msg = health_check.check_log_has_today()
        self.assertTrue(ok, msg)

    def test_log_today_without_finish(self):
        self._write_log(complete=False)
        ok, _ = health_check.check_log_has_today()
        self.assertFalse(ok)

    # ---- 逐账号收尾(2026-10-04 事故) ----

    def _write_accounts_config(self, *names: str):
        health_check.CONFIG_FILE.write_text(
            json.dumps(
                {
                    "accounts": [
                        {"name": n, "storage_file": f"storage_{n}.json"} for n in names
                    ]
                }
            ),
            encoding="utf-8",
        )

    def test_log_second_account_killed_midway_detected(self):
        """复现 2026-10-04: main 正常收尾, 另一账号发到一半被 timeout 杀掉。"""
        self._write_accounts_config("main", "13800000001")
        day = self._today()
        lines = [
            f"[{day} 00:18:24] ==== 账号 main 完成: 发送 5 个 / 失败 0 个 ====",
            f"[{day} 00:45:01] [run_cron] 运行失败(rc=124), 非重试退出码, 直接退出",
        ]
        health_check.LOG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        ok, msg = health_check.check_log_has_today()
        self.assertFalse(ok, "跑了一半必须判失败, 不能被另一个账号的正常收尾掩盖")
        self.assertIn("无收尾记录", msg)
        self.assertIn("13800000001", msg)
        self.assertIn("已收尾: main", msg)

    def test_log_all_configured_accounts_complete_ok(self):
        self._write_accounts_config("main", "13800000001")
        day = self._today()
        lines = [
            f"[{day} 00:18:24] ==== 账号 main 完成: 发送 5 个 / 失败 0 个 ====",
            f"[{day} 00:41:00] ==== 账号 13800000001 完成: 发送 8 个 / 失败 0 个 ====",
        ]
        health_check.LOG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        ok, msg = health_check.check_log_has_today()
        self.assertTrue(ok, msg)

    def test_log_no_pending_line_counts_as_account_finished(self):
        """单账号「无待续」收尾与另一账号的完成行都算数。"""
        self._write_accounts_config("main", "13800000001")
        day = self._today()
        lines = [
            f"[{day} 00:01:00] 今日({day})没有需要续火花的好友, 账号 main 结束",
            f"[{day} 00:02:00] ==== 账号 13800000001 完成: 发送 3 个 / 失败 0 个 ====",
        ]
        health_check.LOG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        ok, msg = health_check.check_log_has_today()
        self.assertTrue(ok, msg)

    def test_log_skipped_account_is_failure(self):
        """跳过 = 该账号今日没续火花(登录态失效场景), 不能算健康。"""
        self._write_accounts_config("main", "13800000001")
        day = self._today()
        lines = [
            f"[{day} 00:01:00] 【账号 main 未配置 account/password 且无登录态, 跳过】",
            f"[{day} 00:02:00] ==== 账号 13800000001 完成: 发送 3 个 / 失败 0 个 ====",
        ]
        health_check.LOG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        ok, msg = health_check.check_log_has_today()
        self.assertFalse(ok, msg)
        self.assertIn("跳过", msg)
        self.assertIn("main", msg)

    def test_log_other_accounts_test_forgery_does_not_satisfy(self):
        """伪造别的账号名的完成行, 不能顶替真正缺失的那个账号。"""
        self._write_accounts_config("main", "13800000001")
        day = self._today()
        lines = [
            # 单测/孤儿进程伪造的完成行(2026-10-04 板子上出现过 42 条)
            f"[{day} 00:45:29] ==== 账号 main 完成: 发送 2 个 / 失败 0 个 ====",
        ]
        health_check.LOG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        ok, msg = health_check.check_log_has_today()
        self.assertFalse(ok)
        self.assertIn("13800000001", msg)

    # ---- main() 退出码与告警 ----

    def test_main_exit_zero_when_healthy(self):
        self._write_state()
        self._write_log()
        with patch("sys.argv", ["health_check.py", "--quiet"]):
            self.assertEqual(health_check.main(), 0)

    def test_main_exit_one_when_failed(self):
        with (
            patch("sys.argv", ["health_check.py", "--quiet"]),
            patch("src.health_check.notify"),
        ):
            self.assertEqual(health_check.main(), 1)

    def test_main_writes_fail_marker(self):
        self._write_state(day="2000-01-01")
        self._write_log()
        with (
            patch("sys.argv", ["health_check.py", "--quiet"]),
            patch("src.health_check.notify"),
        ):
            self.assertEqual(health_check.main(), 1)
        self.assertTrue(health_check.FAIL_MARKER.is_file())

    def test_recovery_clears_marker(self):
        self._write_state(day="2000-01-01")
        self._write_log()
        with (
            patch("sys.argv", ["health_check.py", "--quiet"]),
            patch("src.health_check.notify"),
        ):
            self.assertEqual(health_check.main(), 1)
        self.assertTrue(health_check.FAIL_MARKER.is_file())
        # 恢复健康 → 清除标记
        self._write_state()
        with patch("sys.argv", ["health_check.py", "--quiet"]):
            self.assertEqual(health_check.main(), 0)
        self.assertFalse(health_check.FAIL_MARKER.is_file())

    def test_notify_only_on_change_skips_repeat(self):
        self._write_state(day="2000-01-01")
        self._write_log()
        with (
            patch(
                "sys.argv", ["health_check.py", "--quiet", "--notify-only-on-change"]
            ),
            patch("src.health_check.notify") as mock_notify,
        ):
            self.assertEqual(health_check.main(), 1)
            self.assertEqual(mock_notify.call_count, 1)
            self.assertEqual(health_check.main(), 1)
            self.assertEqual(mock_notify.call_count, 1, "连续失败不应重复提醒")

    def test_notify_cmd_read_from_config(self):
        health_check.CONFIG_FILE.write_text(
            json.dumps({"notify_cmd": "echo hi > /dev/null"}), encoding="utf-8"
        )
        self.assertEqual(health_check.load_notify_cmd(), "echo hi > /dev/null")

    def test_notify_cmd_missing_returns_none(self):
        self.assertIsNone(health_check.load_notify_cmd())

    # ---- notify 送达确认(2026-10-04: 告警链路不再静默失败) ----

    def test_notify_confirms_delivery_on_zero_exit(self):
        health_check.CONFIG_FILE.write_text(
            json.dumps({"notify_cmd": "true"}), encoding="utf-8"
        )
        with patch(
            "src.health_check.subprocess.run",
            return_value=MagicMock(returncode=0),
        ):
            self.assertTrue(health_check.notify("boom"))

    def test_notify_false_on_nonzero_exit(self):
        health_check.CONFIG_FILE.write_text(
            json.dumps({"notify_cmd": "false"}), encoding="utf-8"
        )
        with patch(
            "src.health_check.subprocess.run",
            return_value=MagicMock(returncode=1),
        ):
            self.assertFalse(health_check.notify("boom"))

    def test_notify_false_on_exception(self):
        health_check.CONFIG_FILE.write_text(
            json.dumps({"notify_cmd": "boom"}), encoding="utf-8"
        )
        with patch(
            "src.health_check.subprocess.run", side_effect=RuntimeError("no sh")
        ):
            self.assertFalse(health_check.notify("boom"))

    def test_notify_false_when_unconfigured(self):
        self.assertFalse(health_check.notify("boom"))

    def test_main_no_marker_when_notify_not_confirmed(self):
        """告警未确认送达时不得写 FAIL_MARKER, 否则下一晚会被永久静音。"""
        self._write_state(day="2000-01-01")
        self._write_log()
        with (
            patch("sys.argv", ["health_check.py", "--quiet"]),
            patch("src.health_check.notify", return_value=False),
        ):
            self.assertEqual(health_check.main(), 1)
        self.assertFalse(
            health_check.FAIL_MARKER.is_file(), "告警未送达不得写已提醒标记"
        )

    def test_notify_only_on_change_retries_when_not_confirmed(self):
        """notify 未确认送达 → 不写标记 → 下一次仍会尝试告警(不静音)。"""
        self._write_state(day="2000-01-01")
        self._write_log()
        with (
            patch(
                "sys.argv", ["health_check.py", "--quiet", "--notify-only-on-change"]
            ),
            patch("src.health_check.notify", return_value=False) as mock_notify,
        ):
            self.assertEqual(health_check.main(), 1)
            self.assertFalse(health_check.FAIL_MARKER.is_file())
            self.assertEqual(health_check.main(), 1)
            self.assertEqual(mock_notify.call_count, 2, "未送达应每次都重试告警")

    # ---- 送达判据本身要能被证伪(2026-10-07 审查 #99) ----

    def test_notify_curl_without_fail_flag_is_not_delivery_proof(self):
        """curl 不加 -f 时 HTTP 4xx/5xx 也返回 0 → 「退出码 0」是假证据, 按未送达处理。

        但**命令必须照常执行**: 板端 config.json 现就是 `curl -s ...`(无 -f),
        若因「无法证实」而不发, 修一个假绿就制造了一个静默 —— 更糟。
        """
        health_check.CONFIG_FILE.write_text(
            json.dumps({"notify_cmd": "curl -s -X POST https://example.invalid/t"}),
            encoding="utf-8",
        )
        with patch(
            "src.health_check.subprocess.run", return_value=MagicMock(returncode=0)
        ) as mrun:
            self.assertFalse(health_check.notify("boom"))
        mrun.assert_called_once()

    def test_notify_curl_with_fail_flag_confirms(self):
        for cmd in ("curl -fsS -X POST https://example.invalid/t", "curl -s --fail t"):
            health_check.CONFIG_FILE.write_text(
                json.dumps({"notify_cmd": cmd}), encoding="utf-8"
            )
            with patch(
                "src.health_check.subprocess.run",
                return_value=MagicMock(returncode=0),
            ):
                self.assertTrue(health_check.notify("boom"), cmd)

    def test_notify_non_curl_trusts_exit_code(self):
        """非 curl 命令(如自写脚本)仍按退出码判定, 不被 curl 规则误伤。"""
        health_check.CONFIG_FILE.write_text(
            json.dumps({"notify_cmd": "echo hi"}), encoding="utf-8"
        )
        with patch(
            "src.health_check.subprocess.run", return_value=MagicMock(returncode=0)
        ):
            self.assertTrue(health_check.notify("boom"))

    def test_alert_ledger_records_every_attempt(self):
        """送达与否都落台账: 告警链路自己坏了时, 事后仍查得到报过什么。"""
        self._write_state(day="2000-01-01")
        self._write_log()
        ledger = health_check.alerts_log_path()
        with (
            patch("sys.argv", ["health_check.py", "--quiet"]),
            patch("src.health_check.notify", return_value=True),
        ):
            self.assertEqual(health_check.main(), 1)
        with patch(
            "sys.argv", ["health_check.py", "--quiet", "--notify-only-on-change"]
        ):
            # 标记已写 → 跳过提醒, 不该再叠一行
            self.assertEqual(health_check.main(), 1)
        lines = ledger.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("delivered=1", lines[0])
        self.assertIn("state", lines[0], "台账必须带上失败摘要原文")

    def test_alert_ledger_records_undelivered(self):
        self._write_state(day="2000-01-01")
        self._write_log()
        ledger = health_check.alerts_log_path()
        with (
            patch("sys.argv", ["health_check.py", "--quiet"]),
            patch("src.health_check.notify", return_value=False),
        ):
            self.assertEqual(health_check.main(), 1)
        line = ledger.read_text(encoding="utf-8").splitlines()[-1]
        self.assertIn("delivered=0", line)

    def test_alert_ledger_records_recovery(self):
        """恢复健康时补一行 RECOVERED, 台账才是完整的事故时间线。"""
        self._write_state()
        self._write_log()
        health_check.FAIL_MARKER.write_text("2000-01-01T00:00:00", encoding="utf-8")
        ledger = health_check.alerts_log_path()
        with patch("sys.argv", ["health_check.py", "--quiet"]):
            self.assertEqual(health_check.main(), 0)
        self.assertIn("RECOVERED", ledger.read_text(encoding="utf-8"))


class TestHeadedConfigCheck(unittest.TestCase):
    """P1: 有头模式配置断言(2026-09-30 事故: 板子 config.json 丢两键)。"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.old_config = health_check.CONFIG_FILE
        health_check.CONFIG_FILE = self.root / "config.json"

    def tearDown(self):
        health_check.CONFIG_FILE = self.old_config
        self.tmpdir.cleanup()

    def _cfg(self, cfg):
        health_check.CONFIG_FILE.write_text(json.dumps(cfg), encoding="utf-8")
        return health_check.check_headed_config()

    def test_missing_config_skips(self):
        ok, _ = health_check.check_headed_config()
        self.assertTrue(ok)

    def test_correct_headed_config_ok(self):
        ok, _ = self._cfg({"headless": False, "display": ":99"})
        self.assertTrue(ok)

    def test_missing_keys_fails(self):
        ok, msg = self._cfg({"send_window": {}})
        self.assertFalse(ok)
        self.assertIn("headless", msg)

    def test_headless_true_fails(self):
        ok, _ = self._cfg({"headless": True, "display": ":99"})
        self.assertFalse(ok)


class TestXvfbCheck(unittest.TestCase):
    def test_skips_without_systemctl(self):
        with patch("src.health_check.shutil.which", return_value=None):
            ok, msg = health_check.check_xvfb_display()
        self.assertTrue(ok)
        self.assertIn("跳过", msg)

    def test_active_ok(self):
        with (
            patch("src.health_check.shutil.which", return_value="/usr/bin/systemctl"),
            patch(
                "src.health_check.subprocess.run",
                return_value=MagicMock(returncode=0, stdout="active\n", stderr=""),
            ),
        ):
            ok, _ = health_check.check_xvfb_display()
        self.assertTrue(ok)

    def test_inactive_fails(self):
        with (
            patch("src.health_check.shutil.which", return_value="/usr/bin/systemctl"),
            patch(
                "src.health_check.subprocess.run",
                return_value=MagicMock(returncode=3, stdout="inactive\n", stderr=""),
            ),
        ):
            ok, msg = health_check.check_xvfb_display()
        self.assertFalse(ok)
        self.assertIn("xvfb", msg)


class TestWorklogDriftCheck(unittest.TestCase):
    """记档对账项: 默认关(板子开了会被自愈基线刷成天天误报)。"""

    def test_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            ok, msg = health_check.check_worklog_drift()
        self.assertTrue(ok)
        self.assertIn("未启用", msg)

    def test_enabled_but_tool_missing_skips(self):
        with tempfile.TemporaryDirectory() as tmpd:
            with (
                patch.dict(os.environ, {"HEALTH_WORKLOG_DRIFT": "1"}),
                patch("src.health_check.BASE_DIR", Path(tmpd)),
            ):
                ok, msg = health_check.check_worklog_drift()
            self.assertTrue(ok)
            self.assertIn("跳过", msg)

    def test_enabled_reports_debt_as_fail(self):
        proc = MagicMock(
            returncode=1, stdout="", stderr="  - 2026-10-02 abc123 refactor: 换引擎"
        )
        with (
            patch.dict(os.environ, {"HEALTH_WORKLOG_DRIFT": "1"}),
            patch("src.health_check.subprocess.run", return_value=proc),
        ):
            ok, msg = health_check.check_worklog_drift()
        self.assertFalse(ok)
        self.assertIn("abc123", msg)


class TestRunInProgressGate(unittest.TestCase):
    """任务还没跑完就不下结论(判定过早 = 假告警 = 告警疲劳)。

    陈旧锁(PID 已死)和 PID 复用(锁里的 PID 被别的进程占了)都**不算**运行中,
    否则一个死锁文件就能让告警永久静音 —— 那比假告警严重得多。
    """

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self._old = (
            health_check.PID_FILE,
            health_check._PROC_ROOT,
            health_check.HEARTBEAT_FILE,
        )
        health_check.PID_FILE = self.root / ".spark.pid"
        health_check._PROC_ROOT = self.root / "proc"
        health_check.HEARTBEAT_FILE = self.root / "heartbeat.json"
        health_check._PROC_ROOT.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        (
            health_check.PID_FILE,
            health_check._PROC_ROOT,
            health_check.HEARTBEAT_FILE,
        ) = self._old
        self.tmpdir.cleanup()

    def _write_proc(self, pid: int, cmdline: str):
        d = health_check._PROC_ROOT / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "cmdline").write_bytes(cmdline.encode())

    def _write_heartbeat(self, age_min: float = 0.0):
        import json as _json

        health_check.HEARTBEAT_FILE.write_text(
            _json.dumps(
                {
                    "ts": time.time() - age_min * 60,
                    "iso": "2026-10-06 00:30:00",
                    "stage": "friend_start:某某",
                    "account": "main",
                    "friend": "某某",
                }
            ),
            encoding="utf-8",
        )

    def test_live_spark_pid_is_run_in_progress(self):
        health_check.PID_FILE.write_text("4242", encoding="utf-8")
        self._write_proc(4242, "python\x00/opt/douyin-spark/spark.py\x00")
        self.assertEqual(health_check._live_run_pid(), 4242)

    def test_dead_pid_is_not_run_in_progress(self):
        health_check.PID_FILE.write_text("4242", encoding="utf-8")  # 无 /proc 条目
        self.assertIsNone(health_check._live_run_pid())

    def test_reused_pid_of_unrelated_process_is_not_run_in_progress(self):
        health_check.PID_FILE.write_text("4242", encoding="utf-8")
        self._write_proc(4242, "bash\x00/usr/bin/vim\x00foo")
        self.assertIsNone(health_check._live_run_pid())

    def test_orphan_heal_agent_is_not_run_in_progress(self):
        """项目路径本身就含 `douyin-spark` —— 判据写成裸 `spark` 会把自愈孤儿当"主任务还在跑"。

        2026-10-04 评审: rc=124 后被杀的主进程留下过无主 pi 子代理, 若它复用/撞上
        那个 PID, 健康检查就会"不判定"地退 0, 整晚没人判过 —— 和静默断火同一种后果。
        """
        health_check.PID_FILE.write_text("4242", encoding="utf-8")
        self._write_proc(
            4242,
            "pi\x00--tools\x00read,write,edit,bash,grep,find,ls\x00cwd=/opt/douyin-spark\x00",
        )
        self.assertIsNone(health_check._live_run_pid())

    def test_missing_or_garbled_pid_file_is_not_run_in_progress(self):
        health_check.PID_FILE.write_text("not-a-pid", encoding="utf-8")
        self.assertIsNone(health_check._live_run_pid())
        health_check.PID_FILE.write_text("", encoding="utf-8")
        self.assertIsNone(health_check._live_run_pid())

    def test_main_skips_judgement_and_keeps_marker_while_running(self):
        health_check.PID_FILE.write_text("4242", encoding="utf-8")
        self._write_proc(4242, "python\x00spark.py\x00")
        self._write_heartbeat(0.0)  # 心跳新鲜 = 业务在推进, 才允许豁免
        with (
            patch("sys.argv", ["health_check.py", "--quiet"]),
            patch("src.health_check.notify") as mock_notify,
            patch.object(health_check, "STATE_FILE", self.root / "state.json"),
            patch.object(health_check, "LOG_FILE", self.root / "spark.log"),
            patch.object(health_check, "CONFIG_FILE", self.root / "config.json"),
            patch.object(health_check, "FAIL_MARKER", self.root / ".health_failed"),
        ):
            # 日志/状态全缺(否则必判 FAIL) —— 运行中既不判定也不碰失败标记
            (self.root / ".health_failed").write_text("x", encoding="utf-8")
            self.assertEqual(health_check.main(), 0)
            self.assertTrue(
                (self.root / ".health_failed").is_file(), "运行中不得改失败标记"
            )
            mock_notify.assert_not_called()

    def test_long_running_spark_is_not_exempt(self):
        """PID 仍活且确为 spark.py, 但锁文件年龄超过 _MAX_LEGIT_RUN_MIN → 视为挂死, 继续判定。"""
        pid_file = health_check.PID_FILE
        pid_file.write_text("4242", encoding="utf-8")
        self._write_proc(4242, "python\x00spark.py\x00")
        self._write_heartbeat(0.0)  # 心跳新鲜也挡不住进程龄超限
        old = time.time() - (health_check._MAX_LEGIT_RUN_MIN + 50) * 60
        os.utime(pid_file, (old, old))
        with (
            patch("sys.argv", ["health_check.py", "--quiet"]),
            patch("src.health_check.notify") as mock_notify,
            patch.object(health_check, "STATE_FILE", self.root / "state.json"),
            patch.object(health_check, "LOG_FILE", self.root / "spark.log"),
            patch.object(health_check, "CONFIG_FILE", self.root / "config.json"),
            patch.object(health_check, "FAIL_MARKER", self.root / ".health_failed"),
        ):
            # 状态/日志全缺 → 豁免解除后必须继续判定并 FAIL, 且真的告警
            self.assertEqual(health_check.main(), 1)
            mock_notify.assert_called_once()

    # ---- 心跳豁免升级(2026-10-06, ChatGPT round-2 ④): 「活着但卡死」不再静音 ----

    def test_heartbeat_fresh_status_ok(self):
        self._write_heartbeat(2.0)
        fresh, where = health_check._heartbeat_status()
        self.assertTrue(fresh)
        self.assertIn("friend_start", where)

    def test_heartbeat_stale_when_over_threshold(self):
        self._write_heartbeat(health_check._HEARTBEAT_FRESH_MIN + 10)
        fresh, where = health_check._heartbeat_status()
        self.assertFalse(fresh)
        self.assertIn("分钟前", where)

    def test_heartbeat_missing_file_is_not_fresh(self):
        fresh, where = health_check._heartbeat_status()
        self.assertFalse(fresh)
        self.assertIn("缺失", where)

    def test_heartbeat_garbled_is_not_fresh(self):
        health_check.HEARTBEAT_FILE.write_text("{not-json", encoding="utf-8")
        fresh, _ = health_check._heartbeat_status()
        self.assertFalse(fresh)

    def test_alive_process_with_stale_heartbeat_is_not_exempt(self):
        """核心回归: 进程活着、进程龄未超限, 但心跳陈旧 → 疑似卡死, 继续判定并告警。"""
        health_check.PID_FILE.write_text("4242", encoding="utf-8")
        self._write_proc(4242, "python\x00spark.py\x00")
        self._write_heartbeat(health_check._HEARTBEAT_FRESH_MIN + 10)
        with (
            patch("sys.argv", ["health_check.py", "--quiet"]),
            patch("src.health_check.notify") as mock_notify,
            patch.object(health_check, "STATE_FILE", self.root / "state.json"),
            patch.object(health_check, "LOG_FILE", self.root / "spark.log"),
            patch.object(health_check, "CONFIG_FILE", self.root / "config.json"),
            patch.object(health_check, "FAIL_MARKER", self.root / ".health_failed"),
        ):
            self.assertEqual(health_check.main(), 1)
            mock_notify.assert_called_once()

    def test_alive_process_without_heartbeat_file_is_not_exempt(self):
        """无心跳文件同样不得豁免(否则「锁被挂死实例持有」继续整晚静音)。"""
        health_check.PID_FILE.write_text("4242", encoding="utf-8")
        self._write_proc(4242, "python\x00spark.py\x00")
        with (
            patch("sys.argv", ["health_check.py", "--quiet"]),
            patch("src.health_check.notify") as mock_notify,
            patch.object(health_check, "STATE_FILE", self.root / "state.json"),
            patch.object(health_check, "LOG_FILE", self.root / "spark.log"),
            patch.object(health_check, "CONFIG_FILE", self.root / "config.json"),
            patch.object(health_check, "FAIL_MARKER", self.root / ".health_failed"),
        ):
            self.assertEqual(health_check.main(), 1)
            mock_notify.assert_called_once()


if __name__ == "__main__":
    unittest.main()


class TestRotatedLog(unittest.TestCase):
    """spark.log 被 logrotate 切走后, 健康检查仍要认到当日的收尾记录。

    2026-11-01 实例: 板子 BOARD-truncate-logs 因 /var/log 用量超 75% 触发
    logrotate --force, 把当日日志轮转成 spark.log.1, live 文件归零, 于是
    check_log_has_today() 误报「今日无运行记录」。
    """

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.old = (
            health_check.LOG_FILE,
            health_check.STATE_FILE,
            health_check.CONFIG_FILE,
        )
        health_check.LOG_FILE = self.root / "spark.log"
        health_check.STATE_FILE = self.root / "state.json"
        # 必须指向不存在的路径: 板子上真 config.json 声明了两个账号, 不隔离就会
        # 按「逐账号收尾」判 13800000001 缺失 -> 在板子上跑单测时用例假失败。
        health_check.CONFIG_FILE = self.root / "config.json"

    def tearDown(self):
        (
            health_check.LOG_FILE,
            health_check.STATE_FILE,
            health_check.CONFIG_FILE,
        ) = self.old
        self.tmpdir.cleanup()

    def test_rotated_log_still_counts(self):
        day = health_check.today()
        rotated = self.root / "spark.log.1"
        rotated.write_text(
            f"[{day} 00:02:27] ==================== 账号: main ====================\n"
            f"[{day} 00:24:13] ==== 账号 main 完成: 发送 4 个 / 失败 0 个 ====\n",
            encoding="utf-8",
        )
        health_check.LOG_FILE.write_text("", encoding="utf-8")
        health_check.STATE_FILE.write_text(
            json.dumps({"date": day, "sent": []}), encoding="utf-8"
        )

        ok, msg = health_check.check_log_has_today()

        self.assertTrue(ok, msg)

    def test_empty_live_and_no_rotation_is_still_failure(self):
        health_check.LOG_FILE.write_text("", encoding="utf-8")

        ok, msg = health_check.check_log_has_today()

        self.assertFalse(ok)
        self.assertIn("无", msg)
