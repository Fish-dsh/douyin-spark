"""config.py 单测：配置加载、状态持久化、发送窗口判定、环境变量读取。"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from src.config import (
    BASE_DIR,
    CHAT_URL,
    CONFIG_FILE,
    ENV_FILE,
    RUNTIME_DIR,
    SEND_STICKER,
    STATE_FILE,
    ConfigError,
    in_window,
    load_config,
    load_env,
    load_state,
    preflight,
    write_state,
)


class TestLoadConfig(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.tmpdir.name) / "config.json"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_missing_config_exits(self):
        with patch("src.config.CONFIG_FILE", Path("/nonexistent/config.json")):
            with self.assertRaises(SystemExit) as cm:
                load_config()
            self.assertEqual(cm.exception.code, 1)

    def test_valid_config(self):
        config_data = {
            "send_window": {"start": "23:30", "end": "00:30"},
            "auto_login": True,
            "block_resources": True,
        }
        self.config_path.write_text(json.dumps(config_data), encoding="utf-8")

        with patch("src.config.CONFIG_FILE", self.config_path):
            cfg = load_config()

        self.assertEqual(cfg["send_window"]["start"], "23:30")
        self.assertTrue(cfg["auto_login"])
        self.assertTrue(cfg["block_resources"])

    def test_missing_send_window_defaults(self):
        config_data = {"auto_login": False}
        self.config_path.write_text(json.dumps(config_data), encoding="utf-8")

        with patch("src.config.CONFIG_FILE", self.config_path):
            cfg = load_config()

        self.assertEqual(cfg["send_window"]["start"], "23:30")
        self.assertEqual(cfg["send_window"]["end"], "00:30")

    def test_missing_block_resources_defaults_true(self):
        config_data = {"send_window": {"start": "22:00", "end": "23:00"}}
        self.config_path.write_text(json.dumps(config_data), encoding="utf-8")

        with patch("src.config.CONFIG_FILE", self.config_path):
            cfg = load_config()

        self.assertTrue(cfg["block_resources"])

    def test_missing_headless_defaults_false(self):
        """没写 headless 时必须补成 false(有头硬约束), 不能靠下游默认值兜。

        2026-09-30 事故: 板子 config.json 丢 headless/display 两键, 于是静默
        跑 headless, 渲染挂死, 当晚全部好友 skip 漏发。"""
        self.config_path.write_text(json.dumps({"send_window": {}}), encoding="utf-8")
        with patch("src.config.CONFIG_FILE", self.config_path):
            cfg = load_config()
        self.assertIs(cfg["headless"], False)

    def test_headless_true_is_kept_but_warned(self):
        """显式写 headless=true 仍照配置生效(桌面档调试要用), 但会告警。"""
        self.config_path.write_text(
            json.dumps({"send_window": {}, "headless": True}), encoding="utf-8"
        )
        with patch("src.config.CONFIG_FILE", self.config_path):
            cfg = load_config()
        self.assertTrue(cfg["headless"])

    def test_invalid_json_exits(self):
        self.config_path.write_text("not valid json", encoding="utf-8")

        with (
            patch("src.config.CONFIG_FILE", self.config_path),
            self.assertRaises(SystemExit) as cm,
        ):
            load_config()
        self.assertEqual(cm.exception.code, 1)


class TestConfigProfiles(unittest.TestCase):
    """perf_profile 与 extends(2026-11-01): 本机开发配置继承生产配置。"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.config_path = self.root / "config.json"

    def tearDown(self):
        self.tmpdir.cleanup()

    def _write(self, name: str, data: dict) -> Path:
        path = self.root / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_perf_profile_defaults_to_low(self):
        self.config_path.write_text(json.dumps({"send_window": {}}), encoding="utf-8")
        with patch("src.config.CONFIG_FILE", self.config_path):
            self.assertEqual(load_config()["perf_profile"], "low")

    def test_perf_profile_passthrough(self):
        self.config_path.write_text(
            json.dumps({"send_window": {}, "perf_profile": "HIGH"}), encoding="utf-8"
        )
        with patch("src.config.CONFIG_FILE", self.config_path):
            self.assertEqual(load_config()["perf_profile"], "high")

    def test_unknown_perf_profile_falls_back_to_low(self):
        self.config_path.write_text(
            json.dumps({"send_window": {}, "perf_profile": "turbo"}), encoding="utf-8"
        )
        with patch("src.config.CONFIG_FILE", self.config_path):
            self.assertEqual(load_config()["perf_profile"], "low")

    def test_extends_merges_base_then_overrides(self):
        self._write(
            "config.json",
            {
                "send_window": {"start": "23:30", "end": "00:30"},
                "headless": False,
                "accounts": [{"name": "main"}],
            },
        )
        dev_path = self._write(
            "config.dev.json",
            {"extends": "config.json", "perf_profile": "high", "headless": True},
        )
        with patch("src.config.CONFIG_FILE", dev_path):
            cfg = load_config()
        self.assertEqual(cfg["perf_profile"], "high")
        self.assertTrue(cfg["headless"])
        self.assertEqual(cfg["send_window"]["start"], "23:30")
        self.assertEqual(cfg["accounts"][0]["name"], "main")

    def test_extends_missing_base_exits(self):
        dev_path = self._write("config.dev.json", {"extends": "nope.json"})
        with patch("src.config.CONFIG_FILE", dev_path):
            with self.assertRaises(SystemExit) as cm:
                load_config()
            self.assertEqual(cm.exception.code, 1)


class TestLoadState(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmpdir.name) / "state.json"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_valid_state(self):
        state_data = {"date": "2026-08-13", "sent": ["好友1", "好友2"]}
        self.state_path.write_text(json.dumps(state_data), encoding="utf-8")

        with patch("src.config.STATE_FILE", self.state_path):
            state = load_state()

        self.assertEqual(state["date"], "2026-08-13")
        self.assertEqual(state["sent"], ["好友1", "好友2"])

    def test_missing_state_returns_default(self):
        with patch("src.config.STATE_FILE", self.state_path):
            state = load_state()

        self.assertEqual(state, {"date": "", "sent": []})

    def test_corrupted_state_returns_default(self):
        self.state_path.write_text("invalid json", encoding="utf-8")

        with patch("src.config.STATE_FILE", self.state_path):
            state = load_state()

        self.assertEqual(state, {"date": "", "sent": []})

    def test_state_without_sent_key_returns_default(self):
        state_data = {"date": "2026-08-13"}
        self.state_path.write_text(json.dumps(state_data), encoding="utf-8")

        with patch("src.config.STATE_FILE", self.state_path):
            state = load_state()

        self.assertEqual(state, {"date": "", "sent": []})


class TestWriteState(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmpdir.name) / "state.json"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_write_state_atomic(self):
        state = {"date": "2026-08-13", "sent": ["好友1"]}

        with patch("src.config.STATE_FILE", self.state_path):
            write_state(state)

        self.assertTrue(self.state_path.is_file())
        loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(loaded, state)


class TestInWindow(unittest.TestCase):
    def test_same_day_window(self):
        now = datetime(2026, 8, 13, 23, 45)
        self.assertTrue(in_window(now, "23:30", "00:30"))

    def test_before_window(self):
        now = datetime(2026, 8, 13, 22, 00)
        self.assertFalse(in_window(now, "23:30", "00:30"))

    def test_after_window_same_day(self):
        now = datetime(2026, 8, 13, 12, 00)
        self.assertFalse(in_window(now, "23:30", "00:30"))

    def test_cross_midnight_window(self):
        # Window 23:30 - 00:30 crosses midnight
        now = datetime(2026, 8, 14, 0, 15)  # Next day 00:15
        self.assertTrue(in_window(now, "23:30", "00:30"))

    def test_cross_midnight_before_start(self):
        now = datetime(2026, 8, 13, 22, 00)
        self.assertFalse(in_window(now, "23:30", "00:30"))

    def test_cross_midnight_after_end(self):
        now = datetime(2026, 8, 14, 1, 00)
        self.assertFalse(in_window(now, "23:30", "00:30"))

    def test_same_start_end_full_day(self):
        now = datetime(2026, 8, 13, 12, 00)
        self.assertTrue(in_window(now, "12:00", "12:00"))

    def test_invalid_time_format_returns_true(self):
        now = datetime(2026, 8, 13, 12, 00)
        self.assertTrue(in_window(now, "invalid", "format"))


class TestLoadEnv(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.env_path = Path(self.tmpdir.name) / ".env"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_load_account_password(self):
        self.env_path.write_text(
            "DOUYIN_ACCOUNT=13800000002\nDOUYIN_PASSWORD=secret123\n", encoding="utf-8"
        )

        with patch("src.config.ENV_FILE", self.env_path):
            account, password = load_env()

        self.assertEqual(account, "13800000002")
        self.assertEqual(password, "secret123")

    def test_missing_env_file(self):
        with patch("src.config.ENV_FILE", self.env_path):
            account, password = load_env()

        self.assertIsNone(account)
        self.assertIsNone(password)

    def test_strips_quotes(self):
        self.env_path.write_text(
            "DOUYIN_ACCOUNT=\"13800000002\"\nDOUYIN_PASSWORD='secret123'\n",
            encoding="utf-8",
        )

        with patch("src.config.ENV_FILE", self.env_path):
            account, password = load_env()

        self.assertEqual(account, "13800000002")
        self.assertEqual(password, "secret123")

    def test_ignores_comments_and_empty(self):
        self.env_path.write_text(
            "# This is a comment\n\nDOUYIN_ACCOUNT=13800000002\n\n# Another comment\nDOUYIN_PASSWORD=secret\n",
            encoding="utf-8",
        )

        with patch("src.config.ENV_FILE", self.env_path):
            account, password = load_env()

        self.assertEqual(account, "13800000002")
        self.assertEqual(password, "secret")

    def test_first_match_wins(self):
        self.env_path.write_text(
            "DOUYIN_ACCOUNT=first\nDOUYIN_ACCOUNT=second\n", encoding="utf-8"
        )

        with patch("src.config.ENV_FILE", self.env_path):
            account, _ = load_env()

        self.assertEqual(account, "first")


class TestConstants(unittest.TestCase):
    def test_base_dir(self):
        self.assertTrue(BASE_DIR.is_dir())

    def test_config_file_path(self):
        self.assertEqual(CONFIG_FILE, BASE_DIR / "config.json")

    def test_state_file_path(self):
        # RUNTIME_DIR 可被 SPARK_RUNTIME_DIR 重定向(tests/conftest.py 就把它指到
        # 临时目录), 故断言 STATE_FILE 跟随 RUNTIME_DIR 而不是写死 BASE_DIR/runtime。
        self.assertEqual(STATE_FILE, RUNTIME_DIR / "state.json")

    def test_env_file_path(self):
        self.assertEqual(ENV_FILE, BASE_DIR / ".env")

    def test_chat_url(self):
        self.assertEqual(CHAT_URL, "https://www.douyin.com/chat")

    def test_send_sticker(self):
        self.assertEqual(SEND_STICKER, "续火花")


class TestPerfProfileNormalization(unittest.TestCase):
    def _load(self, data):
        import json
        import tempfile
        from unittest.mock import patch

        from src.config import load_config

        p = Path(tempfile.mkdtemp()) / "config.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        with patch("src.config.CONFIG_FILE", p):
            return load_config()

    def _base(self, profile):
        return {
            "send_window": {"start": "23:30", "end": "00:30"},
            "accounts": [],
            "perf_profile": profile,
        }

    def test_strip_and_lower(self):
        self.assertEqual(self._load(self._base(" High "))["perf_profile"], "high")

    def test_unknown_falls_back_to_low(self):
        self.assertEqual(self._load(self._base("turbo"))["perf_profile"], "low")


class TestPreflight(unittest.TestCase):
    """2026-11-02 事故门禁: 配置缺到跑下去也没意义时必须硬失败, 不许 rc=0 静默。"""

    def test_empty_accounts_raises(self):
        with self.assertRaises(ConfigError) as cm:
            preflight({"accounts": []})
        self.assertIn("accounts", str(cm.exception))

    def test_missing_accounts_raises(self):
        with self.assertRaises(ConfigError):
            preflight({})

    def test_account_not_dict_raises(self):
        with self.assertRaises(ConfigError) as cm:
            preflight({"accounts": ["main"]})
        self.assertIn("accounts[0]", str(cm.exception))

    def test_account_without_name_raises(self):
        with self.assertRaises(ConfigError) as cm:
            preflight({"accounts": [{"storage_file": "s.json"}]})
        self.assertIn("name", str(cm.exception))

    def test_blank_name_raises(self):
        with self.assertRaises(ConfigError):
            preflight({"accounts": [{"name": "   "}]})

    # 有头模式是硬门禁: accounts 齐了还不够, headless/display 写错同样拒绝启动。
    # 下面这组用例把「配置写错」和「跑起来」之间那道以前的空缺钉住。
    def test_good_config_passes(self):
        preflight(
            {
                "accounts": [{"name": "main"}, {"name": "ACCOUNT_B"}],
                "headless": False,
                "display": ":99",
            }
        )

    def test_headless_true_raises(self):
        with self.assertRaises(ConfigError) as cm:
            preflight(
                {"accounts": [{"name": "main"}], "headless": True, "display": ":99"}
            )
        self.assertIn("headless", str(cm.exception))

    def test_missing_headless_raises(self):
        """没写 headless 不能当"默认有头"放行 —— 那正是 2026-09-30 事故的形态。"""
        with self.assertRaises(ConfigError) as cm:
            preflight({"accounts": [{"name": "main"}], "display": ":99"})
        self.assertIn("headless", str(cm.exception))

    def test_missing_display_raises(self):
        with self.assertRaises(ConfigError) as cm:
            preflight({"accounts": [{"name": "main"}], "headless": False})
        self.assertIn("display", str(cm.exception))

    def test_bad_display_raises(self):
        """display 必须是 :N 形式, 写成路径或裸数字都不行。

        这里只管**格式**: "是否存在那个显示" 是运行时的事(check_xvfb_display 查
        /tmp/.X11-unix 里的 socket), 配置层不该去猜机器上开了哪些显示号。
        """
        # 注意 "  " 会先 strip 成空串再判, 而 ":99 " 是允许的(前后空白无意义),
        # 所以这两个都不该出现在 bad 里 —— 前者是真错, 后者是真对。
        for bad in (":abc", "99", "/tmp/.X11-unix/X99", "", "  "):
            with self.subTest(display=bad):
                with self.assertRaises(ConfigError) as cm:
                    preflight(
                        {"accounts": [{"name": "main"}], "headless": False, "display": bad}
                    )
                self.assertIn("display", str(cm.exception))

    def test_display_with_screen_suffix_passes(self):
        preflight(
            {"accounts": [{"name": "main"}], "headless": False, "display": ":99.0"}
        )

    def test_headless_allowed_by_env_escape_hatch(self):
        """桌面档调试的唯一出口: SPARK_ALLOW_HEADLESS=1 时放行 headless。"""
        from unittest.mock import patch

        from src import config

        with patch.dict("os.environ", {config.ALLOW_HEADLESS_ENV: "1"}):
            preflight({"accounts": [{"name": "main"}], "headless": True})

    def test_env_escape_hatch_still_requires_accounts(self):
        """逃生口只放开有头门禁, accounts 为空仍然硬失败。"""
        from unittest.mock import patch

        from src import config

        with (
            patch.dict("os.environ", {config.ALLOW_HEADLESS_ENV: "1"}),
            self.assertRaises(ConfigError),
        ):
            preflight({"accounts": [], "headless": True})


class TestEnvPath(unittest.TestCase):
    def test_relative_resolves_under_project_root(self):
        from unittest.mock import patch

        from src import config

        default = config.BASE_DIR / "runtime"
        with patch.dict("os.environ", {"SPARK_RUNTIME_DIR": "runtime-dev"}):
            self.assertEqual(
                config._env_path("SPARK_RUNTIME_DIR", default),
                config.BASE_DIR / "runtime-dev",
            )

    def test_blank_uses_default(self):
        from unittest.mock import patch

        from src import config

        default = config.BASE_DIR / "runtime"
        with patch.dict("os.environ", {"SPARK_RUNTIME_DIR": "   "}):
            self.assertEqual(config._env_path("SPARK_RUNTIME_DIR", default), default)

    def test_absolute_passthrough(self):
        """绝对路径原样透传, 不拼到项目根下。

        用临时目录而不是写死 /tmp: Windows 上 Path("/tmp/x").is_absolute() 为
        False(缺盘符), 写死 POSIX 路径的断言在开发机上必挂, 属测试自身的平台
        假设, 不是被测逻辑错。"""
        from unittest.mock import patch

        from src import config

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict("os.environ", {"SPARK_RUNTIME_DIR": tmp}),
        ):
            self.assertEqual(
                config._env_path("SPARK_RUNTIME_DIR", config.BASE_DIR / "runtime"),
                Path(tmp),
            )


if __name__ == "__main__":
    unittest.main()
