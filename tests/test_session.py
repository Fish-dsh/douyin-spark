"""session.py 单测：浏览器会话/资源拦截/内存/登录保底。"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from src.session import (
    LAUNCH_ARGS,
    LoginRequiredError,
    _ensure_login,
    available_mb,
    block_resources,
    close_session,
    free_page_memory,
    launch_browser,
    memory_report,
    open_session,
    save_session_state,
)


class TestBlockResources(unittest.TestCase):
    def test_blocks_media_and_domains(self):
        page = MagicMock()
        route = MagicMock()
        request = MagicMock()

        # Test media type blocked
        request.resource_type = "media"
        request.url = "https://example.com/video.mp4"
        route.request = request

        block_resources(page)
        # Get the route handler
        handler = page.route.call_args[0][1]
        handler(route)
        route.abort.assert_called()

    def test_blocks_third_party_ads(self):
        page = MagicMock()
        route = MagicMock()
        request = MagicMock()

        request.resource_type = "script"
        request.url = "https://ad.toutiao.com/x"
        route.request = request

        block_resources(page)
        handler = page.route.call_args[0][1]
        handler(route)
        route.abort.assert_called()

    def test_allows_douyin_own_tracking(self):
        """抖音自家的埋点/风控域必须放行: 拦自家埋点等于自证机器人(2026-10-05)。"""
        page = MagicMock()
        route = MagicMock()
        request = MagicMock()

        request.resource_type = "script"
        request.url = "https://mon.snssdk.com/track"
        route.request = request

        block_resources(page)
        handler = page.route.call_args[0][1]
        handler(route)
        route.abort.assert_not_called()
        route.continue_.assert_called()

    def test_allows_images_and_fonts(self):
        """图片/字体不再拦: 实测对内存无影响, 且"一张图都不加载"是强机器人特征。"""
        page = MagicMock()
        for rtype, url in (
            ("image", "https://p3.douyinpic.com/a.webp"),
            ("font", "https://lf3-static.bytednsdoc.com/a.woff2"),
        ):
            route = MagicMock()
            request = MagicMock()
            request.resource_type = rtype
            request.url = url
            route.request = request

            block_resources(page)
            handler = page.route.call_args[0][1]
            handler(route)
            route.abort.assert_not_called()
            route.continue_.assert_called()

    def test_allows_other_requests(self):
        page = MagicMock()
        route = MagicMock()
        request = MagicMock()

        request.resource_type = "script"
        request.url = "https://www.douyin.com/api/chat"
        route.request = request

        block_resources(page)
        handler = page.route.call_args[0][1]
        handler(route)
        route.continue_.assert_called()


class TestMemoryReport(unittest.TestCase):
    @patch("builtins.open")
    def test_reports_memory(self, mock_open):
        mock_open.return_value.__enter__.return_value = iter(
            [
                "MemTotal: 15747000 kB\n",
                "MemAvailable: 13509000 kB\n",
            ]
        )

        with patch("src.session.log") as mock_log:
            memory_report("test-tag")
            mock_log.assert_called()
            call_args = mock_log.call_args[0][0]
            self.assertIn("mem:test-tag", call_args)
            # Values are in MB (divided by 1024)
            self.assertIn("13192", call_args)  # 13509000 / 1024 ≈ 13192
            self.assertIn("15378", call_args)  # 15747000 / 1024 ≈ 15378

    @patch("builtins.open", side_effect=OSError("no /proc/meminfo"))
    def test_handles_missing_meminfo(self, mock_open):
        with patch("src.session.log") as mock_log:
            memory_report("test-tag")
            mock_log.assert_called_once()
            call_args = mock_log.call_args[0][0]
            self.assertIn("mem:test-tag", call_args)
            self.assertIn("读取内存信息失败", call_args)


class TestAvailableMb(unittest.TestCase):
    @patch("builtins.open")
    def test_returns_memavailable_in_mb(self, mock_open):
        mock_open.return_value.__enter__.return_value = iter(
            [
                "MemTotal: 15747000 kB\n",
                "MemAvailable: 13509000 kB\n",
            ]
        )
        self.assertAlmostEqual(available_mb(), 13509000 / 1024)

    @patch("builtins.open", side_effect=OSError("no /proc/meminfo"))
    def test_returns_none_when_unreadable(self, mock_open):
        self.assertIsNone(available_mb())


class TestFreePageMemory(unittest.TestCase):
    def test_calls_gc(self):
        page = MagicMock()
        free_page_memory(page)
        self.assertEqual(page.evaluate.call_count, 1)


class TestOpenSession(unittest.TestCase):
    def test_open_session_exists(self):
        self.assertTrue(callable(open_session))


class TestCloseSession(unittest.TestCase):
    def test_closes_context_and_browser(self):
        context = MagicMock()
        browser = MagicMock()

        close_session(browser, context)

        context.close.assert_called_once()
        browser.close.assert_called_once()

    def test_handles_none_gracefully(self):
        close_session(None, None)  # Should not raise

    def test_handles_exceptions_in_close(self):
        context = MagicMock()
        context.close.side_effect = Exception("close failed")
        browser = MagicMock()
        browser.close.side_effect = Exception("close failed")

        close_session(browser, context)  # Should not raise


class TestSaveSessionState(unittest.TestCase):
    @patch("src.session.save_state")
    def test_saves_state(self, mock_save_state):
        context = MagicMock()
        account = {"name": "main"}

        save_session_state(context, account, " (测试)")

        mock_save_state.assert_called_once_with(context, unittest.mock.ANY)

    @patch("src.session.save_state", side_effect=Exception("save failed"))
    def test_handles_save_exception(self, mock_save_state):
        context = MagicMock()
        account = {"name": "main"}

        save_session_state(context, account)  # Should not raise


class TestEnsureLogin(unittest.TestCase):
    def test_no_auto_login_config_false(self):
        browser = MagicMock()
        page = MagicMock()
        cfg = {"auto_login": False}
        account = {"name": "main", "account": "acc", "password": "pwd"}

        with patch("src.session.dump_page") as mock_dump:
            mock_dump.return_value = ""
            with self.assertRaises(LoginRequiredError):
                _ensure_login(browser, page, cfg, account)

    @patch("src.session.auto_login")
    def test_auto_login_success(self, mock_auto_login):
        browser = MagicMock()
        page = MagicMock()
        cfg = {"auto_login": True}
        account = {"name": "main", "account": "13800000002", "password": "secret"}
        mock_auto_login.return_value = True

        _ensure_login(browser, page, cfg, account)

        mock_auto_login.assert_called_once_with(
            browser, page, "13800000002", "secret", unittest.mock.ANY
        )

    @patch("src.session.auto_login")
    def test_auto_login_with_storage_path(self, mock_auto_login):
        browser = MagicMock()
        page = MagicMock()
        cfg = {"auto_login": True}
        account = {
            "name": "13800000002",
            "account": "13800000002",
            "password": "test_password",
            "storage_file": "storage_13800000002.json",
            "state_file": "state_13800000002.json",
        }
        mock_auto_login.return_value = True

        _ensure_login(browser, page, cfg, account)

        mock_auto_login.assert_called_once_with(
            browser, page, "13800000002", "test_password", unittest.mock.ANY
        )
        call_arg = str(mock_auto_login.call_args[0][4])
        self.assertIn("storage_13800000002", call_arg)

    @patch("src.session.auto_login")
    @patch("src.session.dump_page")
    def test_auto_login_fails_raises(self, mock_dump, mock_auto_login):
        browser = MagicMock()
        page = MagicMock()
        cfg = {"auto_login": True}
        account = {"name": "main", "account": "13800000002", "password": "secret"}
        mock_auto_login.return_value = False
        mock_dump.return_value = ""

        with self.assertRaises(LoginRequiredError):
            _ensure_login(browser, page, cfg, account)
        # 不再自己关 browser: 复用模式下它是多好友共享的实例, 关掉等于让下一个
        # 好友踩尸体; 会话生命周期由 open_session 的 except 分支统一收。
        browser.close.assert_not_called()

    @patch("src.session.auto_login")
    @patch("src.session.dump_page")
    def test_no_credentials_skips_dotenv_fallback(self, mock_dump, mock_auto_login):
        """account dict 缺凭据时, 严禁从 .env 兜底(2026-09-24 修复)。

        即使 .env 里存在 DOUYIN_ACCOUNT/DOUYIN_PASSWORD, 也必须直接抛
        LoginRequiredError —— 避免「.env 凭据写入另一账号的 storage 文件」事故。
        """
        browser = MagicMock()
        page = MagicMock()
        cfg = {"auto_login": True}
        account = {"name": "main"}  # 无 account/password
        mock_dump.return_value = ""

        # 写一个伪造的 .env 临时文件, 验证它真的不会被读
        with patch(
            "src.config.load_env", return_value=("envacc", "envpwd")
        ) as mock_load_env:
            with self.assertRaises(LoginRequiredError):
                _ensure_login(browser, page, cfg, account)
            mock_load_env.assert_not_called()  # ← 关键: 不读 .env
            mock_auto_login.assert_not_called()  # ← 关键: 不登录

    @patch("src.session.auto_login")
    @patch("src.session.dump_page")
    def test_partial_credentials_still_raises(self, mock_dump, mock_auto_login):
        """仅 account 或仅 password 都不算完整凭据, 必须 fail loudly。"""
        browser = MagicMock()
        page = MagicMock()
        cfg = {"auto_login": True}
        mock_dump.return_value = ""

        for partial in ({"name": "a", "account": "x"}, {"name": "a", "password": "y"}):
            with self.assertRaises(LoginRequiredError):
                _ensure_login(browser, page, cfg, partial)
        mock_auto_login.assert_not_called()


class TestOpenSessionLoginIsolation(unittest.TestCase):
    """未登录必须是**可捕获的异常**, 不能是 SystemExit。

    2026-10-07 审查: _ensure_login 原先 sys.exit(2) —— SystemExit 属 BaseException,
    绕过调度层全部 `except Exception`, 于是多账号串行时第一个账号掉线会让整进程
    消失, 后面本来健康的账号当晚一个好友都没跑。
    """

    def test_open_session_raises_not_exits_and_closes_session(self):
        browser = MagicMock()
        context = MagicMock()
        page = MagicMock()
        p = MagicMock()
        p.chromium.launch.return_value = browser
        context.new_page.return_value = page
        cfg = {"auto_login": False, "net_sniff": False}
        account = {"name": "main"}
        with (
            patch("src.session.restore_state", return_value=context),
            patch("src.session.is_logged_in", return_value=False),
            patch("src.session.dump_page", return_value=""),
            patch("src.session.memory_report"),
            self.assertRaises(LoginRequiredError),
        ):
            open_session(p, cfg, account)
        page.goto.assert_called_once()
        context.close.assert_called_once()
        browser.close.assert_called_once()

    def test_systemexit_would_have_bypassed_scheduler_except_exception(self):
        """守卫语义: LoginRequiredError 必须是 Exception 的子类(而非 BaseException 直连)。"""
        self.assertTrue(issubclass(LoginRequiredError, Exception))


class TestLaunchArgs(unittest.TestCase):
    def test_launch_args_contains_key_flags(self):
        self.assertIn("--disable-blink-features=AutomationControlled", LAUNCH_ARGS)
        self.assertIn("--disable-gpu", LAUNCH_ARGS)
        self.assertIn("--no-sandbox", LAUNCH_ARGS)
        self.assertIn("--disable-dev-shm-usage", LAUNCH_ARGS)
        self.assertIn("--renderer-process-limit=1", LAUNCH_ARGS)


class TestLaunchBrowser(unittest.TestCase):
    """headless/display 走 cfg 驱动(板子 Xvfb headed 模式, 2026-09-24)。"""

    def test_default_headless_true(self):
        p = MagicMock()
        with patch("src.session.memory_report"):
            launch_browser(p, {})
        _, kwargs = p.chromium.launch.call_args
        self.assertTrue(kwargs["headless"])
        self.assertIs(kwargs["args"], LAUNCH_ARGS)

    def test_cfg_headed_with_display(self):
        import os

        p = MagicMock()
        with (
            patch("src.session.memory_report"),
            patch.dict("src.session.os.environ", {"DISPLAY": ":0"}, clear=True),
        ):
            launch_browser(p, {"headless": False, "display": ":99"})
            self.assertEqual(os.environ["DISPLAY"], ":99")
        _, kwargs = p.chromium.launch.call_args
        self.assertFalse(kwargs["headless"])

    def test_headed_swiftgl_args(self):
        """headed + 虚拟显示: 必须去掉禁用软件光栅器, 补 SwiftShader 参数。"""
        p = MagicMock()
        with patch("src.session.memory_report"):
            launch_browser(p, {"headless": False})
        _, kwargs = p.chromium.launch.call_args
        args = kwargs["args"]
        self.assertNotIn("--disable-software-rasterizer", args)
        for flag in (
            "--enable-unsafe-swiftshader",
            "--use-gl=angle",
            "--use-angle=swiftshader",
            "--disable-gpu-compositing",
        ):
            self.assertIn(flag, args)

    def test_headless_keeps_software_rasterizer_disabled(self):
        """headless 路径参数必须与旧行为完全一致(零回归)。"""
        p = MagicMock()
        with patch("src.session.memory_report"):
            launch_browser(p, {"headless": True})
        _, kwargs = p.chromium.launch.call_args
        self.assertIs(kwargs["args"], LAUNCH_ARGS)
        self.assertIn("--disable-software-rasterizer", LAUNCH_ARGS)

    def test_headed_without_display_keeps_env(self):
        import os

        p = MagicMock()
        with (
            patch("src.session.memory_report"),
            patch.dict("src.session.os.environ", {"DISPLAY": ":0"}, clear=True),
        ):
            launch_browser(p, {"headless": False})
            self.assertEqual(os.environ["DISPLAY"], ":0")


class TestBuildLaunchArgs(unittest.TestCase):
    """档位选择(2026-11-01): low=板端档零回归, high=本机桌面档放开资源上限。"""

    def test_low_profile_headless_is_unchanged(self):
        from src.session import build_launch_args

        self.assertIs(build_launch_args({}, True), LAUNCH_ARGS)
        self.assertIs(build_launch_args({"perf_profile": "low"}, True), LAUNCH_ARGS)

    def test_unknown_profile_falls_back_to_low(self):
        from src.session import build_launch_args

        self.assertIs(build_launch_args({"perf_profile": "turbo"}, True), LAUNCH_ARGS)

    def test_high_profile_drops_board_resource_caps(self):
        from src.session import build_launch_args

        args = build_launch_args({"perf_profile": "high"}, True)
        self.assertNotIn("--disable-gpu", args)
        self.assertNotIn("--renderer-process-limit=1", args)
        self.assertNotIn("--disable-software-rasterizer", args)
        self.assertIn(
            "--js-flags=--max-old-space-size=2048 --max-semi-space-size=128", args
        )
        self.assertIn("--renderer-process-limit=4", args)

    def test_high_profile_keeps_detection_and_safety_flags(self):
        from src.session import build_launch_args

        args = build_launch_args({"perf_profile": "high"}, True)
        for flag in (
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ):
            self.assertIn(flag, args)

    def test_high_profile_headless_has_no_swiftshader(self):
        from src.session import build_launch_args

        args = build_launch_args({"perf_profile": "high"}, True)
        self.assertNotIn("--enable-unsafe-swiftshader", args)

    def test_high_profile_with_virtual_display_adds_swiftshader(self):
        from src.session import build_launch_args

        args = build_launch_args(
            {"perf_profile": "high", "display": ":99", "headless": False}, False
        )
        self.assertIn("--enable-unsafe-swiftshader", args)

    def test_anti_throttle_flags_present_in_both_profiles(self):
        """后台节流三连是板端"面板打开慢/点到旧布局"的嫌疑, 两档都要有。"""
        from src.session import build_launch_args

        for profile in ("low", "high"):
            args = build_launch_args({"perf_profile": profile}, True)
            for flag in (
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows",
            ):
                self.assertIn(flag, args)


if __name__ == "__main__":
    unittest.main()


class TestLaunchArgsProfilesHeaded(unittest.TestCase):
    """板端实跑形态是 low + headed, 之前两档都只测了 headless。"""

    def test_low_headed_swaps_rasterizer_for_swiftshader(self):
        from src.session import LAUNCH_ARGS, build_launch_args

        args = build_launch_args({"perf_profile": "low"}, headless=False)
        self.assertNotIn("--disable-software-rasterizer", args)
        self.assertIn("--enable-unsafe-swiftshader", args)
        self.assertIn("--disable-gpu", args)
        self.assertIsNot(args, LAUNCH_ARGS)

    def test_high_with_display_drops_compositing_flag(self):
        from src.session import build_launch_args

        args = build_launch_args(
            {"perf_profile": "high", "display": ":99"}, headless=False
        )
        self.assertNotIn("--disable-gpu-compositing", args)
        self.assertIn("--use-angle=swiftshader", args)
        self.assertNotIn("--disable-gpu", args)


class TestBuildLaunchArgsMid(unittest.TestCase):
    """mid 档(2026-11-01): 普通笔记本 / 无独显。"""

    def test_mid_profile_drops_single_renderer_and_small_heap(self):
        from src.session import build_launch_args

        args = build_launch_args({"perf_profile": "mid"}, True)
        self.assertNotIn("--renderer-process-limit=1", args)
        self.assertNotIn(
            "--js-flags=--max-old-space-size=256 --max-semi-space-size=32", args
        )
        self.assertIn("--renderer-process-limit=2", args)
        self.assertIn(
            "--js-flags=--max-old-space-size=1024 --max-semi-space-size=64", args
        )

    def test_mid_profile_keeps_gpu_caps_for_software_rendering(self):
        """无独显时软件合成反而更稳, 所以 mid 不放开 GPU 相关开关。"""
        from src.session import build_launch_args

        args = build_launch_args({"perf_profile": "mid"}, True)
        self.assertIn("--disable-gpu", args)
        self.assertIn("--disable-software-rasterizer", args)
        self.assertNotIn("--enable-unsafe-swiftshader", args)

    def test_mid_profile_keeps_detection_flags(self):
        from src.session import build_launch_args

        args = build_launch_args({"perf_profile": "mid"}, True)
        self.assertIn("--disable-blink-features=AutomationControlled", args)
        self.assertIn("--no-sandbox", args)
        self.assertIn("--disable-dev-shm-usage", args)

    def test_mid_is_its_own_object_not_shared_constant(self):
        """mid 必须返回新列表, 否则调用方 append 会污染全局 LAUNCH_ARGS。"""
        from src.session import LAUNCH_ARGS, build_launch_args

        args = build_launch_args({"perf_profile": "mid"}, True)
        self.assertIsNot(args, LAUNCH_ARGS)
        args.append("--probe")
        self.assertNotIn("--probe", LAUNCH_ARGS)

    def test_mid_with_virtual_display_adds_swiftshader_like_low(self):
        from src.session import build_launch_args

        args = build_launch_args(
            {"perf_profile": "mid", "display": ":99", "headless": False}, False
        )
        self.assertIn("--enable-unsafe-swiftshader", args)


if __name__ == "__main__":
    unittest.main()
