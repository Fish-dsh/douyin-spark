"""spark.py --doctor 的用例。

每个用例只钉一件事: 哪一项检查在什么前提下会 fail / warn / ok, 以及 fix 那行
是不是一条能直接照做的命令(Agent 读的就是它)。
"""

import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from src import doctor

NOW = datetime(2026, 11, 22, 10, 0)


class DoctorCase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.cfg = self.dir / "config.json"
        for target, value in (
            ("CONFIG_FILE", self.cfg),
            ("RUNTIME_DIR", self.dir),
            ("BASE_DIR", self.dir),
        ):
            patcher = mock.patch.object(doctor, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_cfg(self, **over):
        base = {
            "send_window": {"start": "23:30", "end": "00:30"},
            "headless": False,
            "display": ":99",
            "perf_profile": "low",
            "accounts": [{"name": "main", "storage_file": "storage_main.json"}],
        }
        base.update(over)
        self.cfg.write_text(json.dumps(base), encoding="utf-8")
        storage = self.dir / "storage_main.json"
        if not storage.exists():
            storage.write_bytes(b"x" * 4096)
        return base

    def levels(self, checks):
        return {c.name: c.level for c in checks}


class TestConfig(DoctorCase):
    def test_missing_config(self):
        checks = doctor.build(NOW)["checks"]
        self.assertEqual(checks[0]["level"], "fail")
        self.assertIn("config.json", checks[0]["message"])

    def test_bad_json(self):
        self.cfg.write_text("{ not json", encoding="utf-8")
        checks = doctor.build(NOW)["checks"]
        self.assertEqual(checks[0]["level"], "fail")
        self.assertIn("解析失败", checks[0]["message"])

    def test_empty_accounts_is_fail(self):
        """2026-11-02 断火一整晚的形状: 配置里没账号, 跑完 rc=0 什么都不发。"""
        self.write_cfg(accounts=[])
        checks = doctor.build(NOW)["checks"]
        self.assertEqual(checks[0]["level"], "fail")
        self.assertIn("0 个人", checks[0]["message"])

    def test_accounts_listed(self):
        self.write_cfg()
        checks = doctor.build(NOW)["checks"]
        self.assertEqual(checks[0]["level"], "ok")
        self.assertIn("main", checks[0]["message"])

    def test_window_bad_format_warns(self):
        self.write_cfg(send_window={"start": "23:95", "end": "00:30"})
        found = [c for c in doctor.build(NOW)["checks"] if c["name"] == "发送窗口"]
        self.assertEqual(found[0]["level"], "warn")
        self.assertIn("整天", found[0]["message"])

    def test_unknown_profile_warns(self):
        self.write_cfg(perf_profile="turbo")
        found = [c for c in doctor.build(NOW)["checks"] if c["name"] == "档位"]
        self.assertEqual(found[0]["level"], "warn")

    def test_extends_one_layer(self):
        base = self.dir / "config.base.json"
        base.write_text(
            json.dumps({"perf_profile": "mid", "accounts": [{"name": "base"}]}),
            encoding="utf-8",
        )
        self.cfg.write_text(
            json.dumps({"extends": "config.base.json", "headless": False}),
            encoding="utf-8",
        )
        found = [c for c in doctor.build(NOW)["checks"] if c["name"] == "配置"]
        self.assertIn("base", found[0]["message"])


class TestLogin(DoctorCase):
    def test_missing_storage(self):
        self.write_cfg()
        (self.dir / "storage_main.json").unlink()
        found = [
            c for c in doctor.build(NOW)["checks"] if c["name"].startswith("登录态")
        ]
        self.assertEqual(found[0]["level"], "fail")
        self.assertIn("login.py", found[0]["fix"])

    def test_tiny_storage(self):
        self.write_cfg()
        (self.dir / "storage_main.json").write_bytes(b"{}")
        found = [
            c for c in doctor.build(NOW)["checks"] if c["name"].startswith("登录态")
        ]
        self.assertEqual(found[0]["level"], "fail")

    def test_stale_storage_warns(self):
        self.write_cfg()
        storage = self.dir / "storage_main.json"
        old = (NOW - timedelta(days=40)).timestamp()
        os.utime(storage, (old, old))
        found = [
            c for c in doctor.build(NOW)["checks"] if c["name"].startswith("登录态")
        ]
        self.assertEqual(found[0]["level"], "warn")
        self.assertIn("40 天", found[0]["message"])

    def test_fresh_storage_ok(self):
        self.write_cfg()
        found = [
            c for c in doctor.build(NOW)["checks"] if c["name"].startswith("登录态")
        ]
        self.assertEqual(found[0]["level"], "ok")


class TestCron(DoctorCase):
    def test_found(self):
        self.write_cfg()
        texts = {"crontab -l": "0 0 * * * /opt/douyin-spark/run_cron.sh"}
        with (
            mock.patch.object(doctor.sys, "platform", "linux"),
            mock.patch.object(doctor, "_cron_texts", return_value=texts),
        ):
            check = doctor._check_cron()
        self.assertEqual(check.level, "ok")
        self.assertIn("crontab -l", check.message)

    def test_missing(self):
        self.write_cfg()
        texts = {"crontab -l": "MAILTO="}
        with (
            mock.patch.object(doctor.sys, "platform", "linux"),
            mock.patch.object(doctor, "_cron_texts", return_value=texts),
        ):
            check = doctor._check_cron()
        self.assertEqual(check.level, "fail")
        self.assertIn("crontab -e", check.fix)
        self.assertIn("run_cron.sh", check.fix)

    def test_no_cron_at_all_is_skip(self):
        self.write_cfg()
        with (
            mock.patch.object(doctor.sys, "platform", "linux"),
            mock.patch.object(doctor, "_cron_texts", return_value={}),
        ):
            check = doctor._check_cron()
        self.assertEqual(check.level, "skip")


class TestDisplay(DoctorCase):
    def test_headless_true_fails(self):
        self.write_cfg(headless=True)
        with mock.patch.object(doctor.sys, "platform", "linux"):
            check = doctor._check_display({"headless": True, "display": ":99"})
        self.assertEqual(check.level, "fail")
        self.assertIn("headless", check.message)

    def test_no_display_fails(self):
        with mock.patch.object(doctor.sys, "platform", "linux"):
            check = doctor._check_display({"headless": False})
        self.assertEqual(check.level, "fail")
        self.assertIn("display", check.message)

    def test_missing_socket_fails(self):
        sock = self.dir / "no-such-X99"
        with (
            mock.patch.object(doctor.sys, "platform", "linux"),
            mock.patch.object(doctor, "_x_socket", return_value=sock),
        ):
            check = doctor._check_display({"headless": False, "display": ":99"})
        self.assertEqual(check.level, "fail")
        self.assertIn("Xvfb", check.fix)

    def test_socket_present_ok(self):
        sock = self.dir / "X99"
        sock.write_bytes(b"")
        with (
            mock.patch.object(doctor.sys, "platform", "linux"),
            mock.patch.object(doctor, "_x_socket", return_value=sock),
        ):
            check = doctor._check_display({"headless": False, "display": ":99"})
        self.assertEqual(check.level, "ok")

    def test_windows_skips(self):
        with mock.patch.object(doctor.sys, "platform", "win32"):
            check = doctor._check_display({"headless": True})
        self.assertEqual(check.level, "skip")


class TestMisc(DoctorCase):
    def test_timezone_ok(self):
        tz = timezone(timedelta(hours=8))
        check = doctor._check_timezone(NOW.replace(tzinfo=tz))
        self.assertEqual(check.level, "ok")

    def test_timezone_off_translates_to_window_risk(self):
        check = doctor._check_timezone(NOW.replace(tzinfo=timezone.utc))
        self.assertEqual(check.level, "warn")
        self.assertIn("错位", check.message)

    def test_lock_running(self):
        lock = self.dir / ".spark.pid"
        lock.write_text("4242", encoding="utf-8")
        with (
            mock.patch.object(doctor.runtime_lock, "lock_path", return_value=str(lock)),
            mock.patch.object(doctor.runtime_lock, "pid_alive", return_value=True),
        ):
            check = doctor._check_lock()
        self.assertEqual(check.level, "ok")
        self.assertIn("4242", check.message)

    def test_lock_stale_is_harmless(self):
        lock = self.dir / ".spark.pid"
        lock.write_text("4242", encoding="utf-8")
        with (
            mock.patch.object(doctor.runtime_lock, "lock_path", return_value=str(lock)),
            mock.patch.object(doctor.runtime_lock, "pid_alive", return_value=False),
        ):
            check = doctor._check_lock()
        self.assertEqual(check.level, "ok")
        self.assertIn("自动接管", check.message)

    def test_window_note_counts_down(self):
        note = doctor._window_note(
            {"send_window": {"start": "23:30", "end": "00:30"}}, NOW
        )
        self.assertIn("13 小时", note)

    def test_window_note_inside_window(self):
        late = datetime(2026, 11, 22, 23, 45)
        note = doctor._window_note(
            {"send_window": {"start": "23:30", "end": "00:30"}}, late
        )
        self.assertIn("窗口内", note)


class TestMain(DoctorCase):
    def _healthy(self):
        self.write_cfg()
        sock = self.dir / "X99"
        sock.write_bytes(b"")
        texts = {"crontab -l": "0 0 * * * run_cron.sh"}
        patchers = [
            mock.patch.object(doctor.sys, "platform", "linux"),
            mock.patch.object(doctor, "_x_socket", return_value=sock),
            mock.patch.object(doctor, "_cron_texts", return_value=texts),
            mock.patch.object(
                doctor,
                "_check_timezone",
                return_value=doctor.Check("时区", doctor._OK, "+08:00"),
            ),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def test_exit_zero_when_healthy(self):
        self._healthy()
        with mock.patch("sys.stdout"):
            self.assertEqual(doctor.main([]), 0)

    def test_exit_one_when_fail(self):
        with mock.patch("sys.stdout"):
            self.assertEqual(doctor.main([]), 1)

    def test_json_has_all_fields(self):
        self.write_cfg()
        data = doctor.build(NOW)
        for key in (
            "generated_at",
            "ok",
            "passes",
            "warns",
            "fails",
            "checks",
            "window_note",
        ):
            self.assertIn(key, data)
        self.assertIsInstance(data["checks"], list)

    def test_render_prints_fix_line(self):
        self.write_cfg(accounts=[])
        text = doctor.render(doctor.build(NOW))
        self.assertIn("[fix]", text)
        self.assertIn("→", text)
