"""login.py 单测：密码登录选择器、自动重登 storage_path 传递。"""

from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
