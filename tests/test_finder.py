"""finder.py 单测：选择器候选、聊天区等待、会话列表等待。"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from src.finder import (
    CHAT_UI_SELECTORS,
    find,
    page_render_state,
    wait_chat_list,
    wait_chat_ui,
)


class TestFind(unittest.TestCase):
    def setUp(self):
        self.page = MagicMock()
        # 2026-11-01 性能改动: find() 先做一次零等待探测(query_selector),
        # 默认给 None 才能测到原来的等待循环; 快路径另有用例覆盖。
        self.page.query_selector.return_value = None

    def test_first_selector_matches(self):
        locator_mock = MagicMock()
        self.page.wait_for_selector.return_value = MagicMock()  # handle
        self.page.locator.return_value.first = locator_mock

        result = find(self.page, "[data-test='first']")

        self.assertEqual(result, locator_mock)
        self.page.wait_for_selector.assert_called_with(
            "[data-test='first']", state="attached", timeout=1500
        )

    def test_second_selector_matches(self):
        handle1 = None
        MagicMock()
        self.page.wait_for_selector.side_effect = [handle1, MagicMock()]
        locator_mock = MagicMock()
        self.page.locator.return_value.first = locator_mock

        result = find(self.page, ["[data-test='first']", "[data-test='second']"])

        self.assertEqual(result, locator_mock)
        self.assertEqual(self.page.wait_for_selector.call_count, 2)

    def test_all_selectors_fail(self):
        self.page.wait_for_selector.return_value = None

        with patch("src.finder.dump_page") as mock_dump:
            mock_dump.return_value = ""
            result = find(self.page, ["[data-test='first']", "[data-test='second']"])

        self.assertIsNone(result)
        self.assertEqual(self.page.wait_for_selector.call_count, 2)

    def test_single_string_selector(self):
        locator_mock = MagicMock()
        self.page.wait_for_selector.return_value = MagicMock()
        self.page.locator.return_value.first = locator_mock

        result = find(self.page, "[data-test='single']")

        self.assertEqual(result, locator_mock)


class TestWaitChatUI(unittest.TestCase):
    def setUp(self):
        self.page = MagicMock()

    @patch("src.finder.find")
    def test_find_returns_locator(self, mock_find):
        mock_find.return_value = MagicMock()

        result = wait_chat_ui(self.page)

        self.assertTrue(result)
        mock_find.assert_called_once_with(self.page, CHAT_UI_SELECTORS)

    @patch("src.finder.find")
    def test_find_returns_none_url_matches(self, mock_find):
        mock_find.return_value = None
        self.page.wait_for_url.return_value = None

        result = wait_chat_ui(self.page)

        self.assertTrue(result)
        self.page.wait_for_url.assert_called()

    @patch("src.finder.find")
    def test_both_fail(self, mock_find):
        mock_find.return_value = None
        self.page.wait_for_url.side_effect = Exception("timeout")

        result = wait_chat_ui(self.page)

        self.assertFalse(result)


class TestWaitChatList(unittest.TestCase):
    def setUp(self):
        self.page = MagicMock()

    def test_list_renders_quickly(self):
        self.page.evaluate.return_value = 5

        wait_chat_list(self.page, tries=16, interval_ms=2500)

        self.page.evaluate.assert_called()
        self.page.wait_for_timeout.assert_not_called()

    def test_list_renders_after_wait(self):
        self.page.evaluate.side_effect = [0, 0, 3]

        wait_chat_list(self.page, tries=16, interval_ms=2500)

        self.assertEqual(self.page.evaluate.call_count, 3)
        self.assertEqual(self.page.wait_for_timeout.call_count, 2)

    def test_times_out(self):
        self.page.evaluate.return_value = 0

        wait_chat_list(self.page, tries=3, interval_ms=100)

        # 性能改写(2026-11-01): 总预算 tries*interval_ms=300ms 不变, 但轮询粒度固定
        # 250ms 且首拍立刻查(旧实现按 3 轮各固定睡 100ms)。故超时前恰好查 2 次。
        self.assertEqual(self.page.evaluate.call_count, 2)
        self.assertEqual(self.page.wait_for_timeout.call_count, 2)

    def test_evaluate_exception_handled(self):
        self.page.evaluate.side_effect = [Exception("error"), 2]

        wait_chat_list(self.page, tries=16, interval_ms=2500)

        self.assertEqual(self.page.evaluate.call_count, 2)

    def test_returns_true_when_populated(self):
        self.page.evaluate.return_value = 3
        self.assertTrue(wait_chat_list(self.page, tries=2, interval_ms=10))

    def test_returns_false_on_timeout(self):
        self.page.evaluate.return_value = 0
        self.assertFalse(wait_chat_list(self.page, tries=2, interval_ms=10))

    def test_predicate_requires_nonempty_text(self):
        """新判定必须过滤掉空骨架容器(2026-09-24 板子 Xvfb 误判事故)。"""
        import inspect

        src = inspect.getsource(wait_chat_list)
        self.assertIn("innerText", src)
        self.assertIn("trim()", src)


class TestChatUISelectors(unittest.TestCase):
    def test_selectors_not_empty(self):
        self.assertGreater(len(CHAT_UI_SELECTORS), 0)

    def test_contains_expected_selectors(self):
        self.assertIn("[data-e2e='chat']", CHAT_UI_SELECTORS)
        self.assertIn("[data-e2e*='chat']", CHAT_UI_SELECTORS)
        self.assertIn("[class*='chat']", CHAT_UI_SELECTORS)


class TestPageRenderState(unittest.TestCase):
    """P3 三态判别: body 全空=渲染挂死, 有内容=可能选择器失效。"""

    def setUp(self):
        self.page = MagicMock()

    def test_blank_when_body_empty(self):
        self.page.evaluate.return_value = 0
        self.assertEqual(page_render_state(self.page), "blank")

    def test_content_when_body_has_text(self):
        self.page.evaluate.return_value = 200
        self.assertEqual(page_render_state(self.page), "content")

    def test_threshold_is_50(self):
        self.page.evaluate.return_value = 49
        self.assertEqual(page_render_state(self.page), "blank")
        self.page.evaluate.return_value = 50
        self.assertEqual(page_render_state(self.page), "content")

    def test_evaluate_exception_is_blank(self):
        self.page.evaluate.side_effect = Exception("target closed")
        self.assertEqual(page_render_state(self.page), "blank")

    def test_wait_chat_list_defaults_cover_soft_render(self):
        """默认预算须覆盖板子软渲染最慢 ~75s(2026-09-30 事故)。

        2026-11-01: 默认值从写死的 30×5s 改为性能档位(list_budget_ms),
        断言随之改为「各档位预算都 >= 90s 安全垫 / low 档仍是 150s」。
        """
        import inspect

        from src import perf

        sig = inspect.signature(wait_chat_list)
        self.assertIsNone(sig.parameters["tries"].default)
        self.assertIsNone(sig.parameters["interval_ms"].default)
        old = perf.active_profile()
        try:
            for name, floor in (("low", 150_000), ("mid", 90_000), ("high", 60_000)):
                perf.set_profile(name)
                self.assertGreaterEqual(perf.ms("list_budget_ms"), floor, name)
            perf.set_profile("low")
            # 单轮粒度不改变总预算语义: 预算 / 粒度的乘积仍需 >= 原 150s
            self.assertGreaterEqual(perf.ms("list_budget_ms"), 75_000)
        finally:
            perf.set_profile(old)


class TestFindFastPath(unittest.TestCase):
    """2026-11-01 性能改动: 命中时不空等 —— 一次 query_selector 即可返回。"""

    def test_immediate_hit_skips_waiting(self):
        page = MagicMock()
        page.query_selector.return_value = MagicMock()
        locator_mock = MagicMock()
        page.locator.return_value.first = locator_mock

        result = find(page, ["[data-test='a']"])

        self.assertEqual(result, locator_mock)
        page.wait_for_selector.assert_not_called()

    def test_probe_exception_falls_back_to_waiting_loop(self):
        page = MagicMock()
        page.query_selector.side_effect = RuntimeError("boom")
        page.wait_for_selector.return_value = None
        with patch("src.finder.dump_page"):
            result = find(page, ["[data-test='a']"])
        self.assertIsNone(result)
        page.wait_for_selector.assert_called_once()


if __name__ == "__main__":
    unittest.main()
