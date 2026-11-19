"""login.py 单测：密码登录选择器、自动重登 storage_path 传递。"""

from __future__ import annotations

import contextlib
import io
import os
import unittest
from unittest.mock import MagicMock, patch

from src import login, stealth
from src.login import PASSWORD_INPUT_SELECTORS, auto_login, restore_state


class TestPasswordInputSelectors(unittest.TestCase):
    """PASSWORD_INPUT_SELECTORS 应包含多种候选选择器以适配抖音登录页变动。"""

    def test_not_empty(self):
        self.assertGreater(len(PASSWORD_INPUT_SELECTORS), 1)

    def test_contains_type_password(self):
        self.assertIn("input[type='password']", PASSWORD_INPUT_SELECTORS)

    def test_contains_placeholder_selectors(self):
        self.assertTrue(any("placeholder" in sel for sel in PASSWORD_INPUT_SELECTORS))

    def test_contains_name_and_class_selectors(self):
        self.assertIn("input[name='password']", PASSWORD_INPUT_SELECTORS)
        self.assertIn("input[class*='password']", PASSWORD_INPUT_SELECTORS)


class TestCandidateHomes(unittest.TestCase):
    """浏览器装在「跑安装命令那个用户」的家目录下, 所以要多看几个家目录。

    公开版不能再写死某个机房的家目录(那是我这台机器的现场), 但也不能只看当前
    用户 —— sudo 装一次、普通用户跑 cron 是最常见的踩坑法。"""

    def test_current_home_first(self):
        homes = login._candidate_homes()
        self.assertTrue(homes)
        self.assertEqual(homes[0], str(__import__("pathlib").Path.home()))

    def test_sudo_user_resolved_to_absolute_home(self):
        with patch.dict("os.environ", {"SUDO_USER": "deployer"}):
            homes = login._candidate_homes()
        self.assertIn("/home/deployer", homes)

    def test_sudo_user_with_absolute_path_kept(self):
        with patch.dict("os.environ", {"SUDO_USER": "/srv/app"}):
            homes = login._candidate_homes()
        self.assertIn("/srv/app", homes)

    def test_no_duplicates(self):
        homes = login._candidate_homes()
        self.assertEqual(len(homes), len(set(homes)))


class TestAutoLoginStoragePath(unittest.TestCase):
    """auto_login 应接受 storage_path 并传递给 save_state。"""

    @patch("src.login._write_login_state")
    @patch("src.login._read_login_state", return_value={})
    @patch("src.login.password_login", return_value=True)
    @patch("src.login.save_state")
    def test_auto_login_passes_storage_path(
        self, mock_save, mock_pwd, mock_read, mock_write
    ):
        browser = MagicMock()
        page = MagicMock()
        storage_path = "runtime/storage_13800000002.json"

        result = auto_login(browser, page, "test_acc", "test_pwd", storage_path)

        self.assertTrue(result)
        mock_save.assert_called_once()
        call_args = mock_save.call_args
        self.assertEqual(len(call_args[0]), 2)
        self.assertEqual(call_args[0][1], storage_path)

    @patch("src.login._write_login_state")
    @patch("src.login._read_login_state", return_value={})
    @patch("src.login.password_login", return_value=True)
    @patch("src.login.save_state")
    def test_auto_login_default_storage_path(
        self, mock_save, mock_pwd, mock_read, mock_write
    ):
        """storage_path 为 None 时应传给 save_state(None)。"""
        browser = MagicMock()
        page = MagicMock()

        result = auto_login(browser, page, "test_acc", "test_pwd")

        self.assertTrue(result)
        mock_save.assert_called_once()
        call_args = mock_save.call_args
        self.assertEqual(len(call_args[0]), 2)
        self.assertIsNone(call_args[0][1])


class TestLoginPageNotMisjudgedAsLoggedIn(unittest.TestCase):
    """登录页文案藏在隐藏节点里时, 不得因 cookie 存在而误判为已登录。"""

    def _page(self, inner: str, content: str, cookies: list[dict] | None = None):
        page = MagicMock()
        page.url = "https://www.douyin.com/chat"

        def _eval(script: str):
            if "innerText" in script:
                return inner
            if "textContent" in script:
                return content
            return ""

        page.evaluate.side_effect = _eval
        page.context.cookies.return_value = cookies or [
            {"name": "sessionid", "value": "x"},
            {"name": "sessionid_ss", "value": "y"},
            {"name": "uid_tt", "value": "z"},
        ]
        return page

    def test_hidden_login_panel_makes_it_not_logged_in(self):
        """innerText 看不到登录文案, 但 textContent 有 -> 判定未登录。"""
        page = self._page(
            inner="抖音聊天 登录后免费畅享高清视频",  # 可见文本里没有标记词
            content="抖音聊天 扫码登录 验证码登录 登录即代表同意用户协议",  # 隐藏节点里有
        )
        with patch.object(login, "_any", return_value=False):
            self.assertFalse(login.is_logged_in(page))

    def test_real_logged_in_still_true(self):
        """真正登录时(用户信息可见, 页面无登录文案)仍判定已登录。"""
        page = self._page(inner="抖音 消息 好友列表", content="抖音 消息 好友列表")
        # _any 依次被调用: LOGIN_PANEL / QR_CODE / USER_INFO -> 仅 user_info 命中
        with patch.object(login, "_any", side_effect=[False, False, True]):
            self.assertTrue(login.is_logged_in(page))


class TestWaitForContentBeforeDetect(unittest.TestCase):
    """骨架阶段不得判定 —— 必须先等页面出现实质内容。"""

    def test_waits_until_content_then_detects_login_page(self):
        page = MagicMock()
        page.url = "https://www.douyin.com/chat"
        texts = [
            "开启读屏标签 读屏标签已关闭",
            "登录后免费畅享高清视频 扫码登录 验证码登录 登录即代表同意用户协议",
        ]
        calls = {"n": 0}

        def _eval(script: str):
            if "innerText" in script:
                i = min(calls["n"], len(texts) - 1)
                calls["n"] += 1
                return texts[i]
            if "textContent" in script:
                return texts[-1]
            return ""

        page.evaluate.side_effect = _eval
        page.context.cookies.return_value = [{"name": "sessionid", "value": "x"}]
        with patch.object(login, "_any", return_value=False):
            self.assertFalse(login.is_logged_in(page))
        self.assertGreaterEqual(calls["n"], 2, "应至少读取两次文本(等待内容)")


class TestPasswordLoginSlowRender(unittest.TestCase):
    """弱设备渲染慢: 「密码登录」tab 晚出现也必须能进入(2026-10-05 板子事故)。"""

    def test_tab_appears_after_delay_still_fills(self):
        page = MagicMock()
        calls = {"tab": 0}

        def _click(_page, _text):
            calls["tab"] += 1
            return calls["tab"] >= 2  # 第一轮没渲染出来, 第二轮才点到

        with (
            patch("src.login._show_login_panel"),
            patch("src.login._click_text", side_effect=_click),
            patch("src.login._element_visible", return_value=True),
            patch("src.login._fill_input", return_value=True),
            patch("src.login._captcha_visible", return_value=False),
            patch("src.login.is_logged_in", return_value=True),
            patch("src.login._dismiss_save_login_prompt"),
        ):
            self.assertTrue(login.password_login(page, "acc", "pwd"))
        self.assertGreaterEqual(calls["tab"], 2)


class TestStealth(unittest.TestCase):
    """自动化特征伪装(2026-10-05): 覆盖抖音会读的关键指纹。"""

    def test_apply_stealth_registers_init_script(self):
        context = MagicMock()
        stealth.apply_stealth(context)
        context.add_init_script.assert_called_once()
        script = context.add_init_script.call_args[0][0]
        self.assertIn("webdriver", script)
        self.assertIn("plugins", script)
        self.assertIn("languages", script)
        self.assertIn("chrome", script)
        # 关键: 渲染器改返回真实显卡串, 不再暴露软件渲染(37446=渲染器)
        self.assertIn("37446", script)
        self.assertIn("ANGLE (Intel", script)

    def test_apply_stealth_swallows_exception(self):
        context = MagicMock()
        context.add_init_script.side_effect = Exception("boom")
        stealth.apply_stealth(context)  # 不应抛出

    def test_restore_state_injects_stealth(self):
        """所有 context 的统一出口都必须注入伪装, 缺失 storage 也不例外。"""
        browser = MagicMock()
        with patch.object(login, "STORAGE_PATH", MagicMock(is_file=lambda: False)):
            context = restore_state(browser)
        context.add_init_script.assert_called_once()

    def test_restore_state_with_storage_injects_stealth(self):
        browser = MagicMock()
        storage = MagicMock(is_file=lambda: True)
        with patch.object(login, "STORAGE_PATH", storage):
            context = restore_state(browser)
        context.add_init_script.assert_called_once()


class TestPrepareDisplay(unittest.TestCase):
    """首次登录最常见的坑: config.json 写了 display=":99" 但机器上只有 :0(或啥都没有)。

    人工登录走的是当前 shell 的 DISPLAY, 不读 config.json —— 所以必须有这层检查,
    否则新人拿到的是 Playwright 的 "Missing X server or $DISPLAY" 栈。
    日志走 logutil.log(print 到 stdout), 所以用 redirect_stdout 取文案。
    """

    def setUp(self):
        self._saved = os.environ.pop("DISPLAY", None)

    def tearDown(self):
        os.environ.pop("DISPLAY", None)
        if self._saved is not None:
            os.environ["DISPLAY"] = self._saved

    def _run(self, explicit, needs_display):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ok = login._prepare_display(explicit, needs_display)
        return ok, buf.getvalue()

    def test_explicit_display_wins_and_is_exported(self):
        os.environ["DISPLAY"] = ":0"
        ok, out = self._run(":99", True)
        self.assertTrue(ok)
        self.assertEqual(os.environ["DISPLAY"], ":99")
        self.assertIn(":99", out)

    def test_existing_shell_display_is_reused(self):
        os.environ["DISPLAY"] = ":0"
        ok, _ = self._run(None, True)
        self.assertTrue(ok)
        self.assertEqual(os.environ["DISPLAY"], ":0")

    def test_missing_display_refuses_with_actionable_hint(self):
        ok, out = self._run(None, True)
        self.assertFalse(ok)
        self.assertIn("DISPLAY", out)
        self.assertIn("xvfb", out.lower())
        self.assertIn("--display :99", out)
        # 顺带提醒 sudo 会丢 DISPLAY —— 这是同一现象的第二大成因
        self.assertIn("sudo", out)

    def test_headless_needs_no_display(self):
        ok, _ = self._run(None, False)
        self.assertTrue(ok)
        self.assertNotIn("DISPLAY", os.environ)

    def test_blank_display_counts_as_missing(self):
        os.environ["DISPLAY"] = "   "
        ok, _ = self._run(None, True)
        self.assertFalse(ok)

    def test_malformed_explicit_display_is_refused(self):
        """--display 99 / --display /tmp/.X11-unix/X99 都是常见手误。

        以前只判"有没有传值", 于是这类值会被原样写进 DISPLAY 交给浏览器,
        报出来的是底层连接错误, 与"参数写错"隔了好几层。
        """
        for bad in ("99", "/tmp/.X11-unix/X99", ":abc", "localhost:99", ":"):
            with self.subTest(display=bad):
                ok, out = self._run(bad, True)
                self.assertFalse(ok)
                self.assertIn("显示号", out)

    def test_screen_suffix_is_accepted(self):
        ok, _ = self._run(":99.0", True)
        self.assertTrue(ok)
        self.assertEqual(os.environ["DISPLAY"], ":99.0")

    def test_malformed_env_display_is_refused(self):
        """环境里的 DISPLAY 也可能是手误, 不能在"有值"这一步就放行。"""
        for bad in ("99", "localhost:0", ":"):
            with self.subTest(display=bad):
                os.environ["DISPLAY"] = bad
                ok, out = self._run(None, True)
                self.assertFalse(ok)
                self.assertIn("显示号", out)


class TestDisplayFlagParsing(unittest.TestCase):
    """--display 的值在参数解析阶段就要拒掉, 不能拖到浏览器启动。"""

    def setUp(self):
        self._saved = os.environ.pop("DISPLAY", None)

    def tearDown(self):
        os.environ.pop("DISPLAY", None)
        if self._saved is not None:
            os.environ["DISPLAY"] = self._saved

    def _main(self, argv):
        buf = io.StringIO()
        with (
            contextlib.redirect_stdout(buf),
            patch.object(login.sys, "argv", ["login.py"] + argv),
            patch.object(login, "sync_playwright") as pw,
        ):
            try:
                login.main()
            except SystemExit as exc:
                return exc.code, buf.getvalue(), pw
        return None, buf.getvalue(), pw

    def test_bad_display_exits_before_browser(self):
        code, out, pw = self._main(["--manual", "--display", ":abc"])
        self.assertEqual(code, 2)
        self.assertIn("显示号", out)
        pw.assert_not_called()

    def test_missing_display_value_exits_before_browser(self):
        code, out, pw = self._main(["--manual", "--display"])
        self.assertEqual(code, 2)
        self.assertIn("--display", out)
        pw.assert_not_called()


if __name__ == "__main__":
    unittest.main()
