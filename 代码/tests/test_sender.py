"""sender.py 单测：表情/文字发送、确认、好友定位点击。"""

from __future__ import annotations

import itertools
import unittest
from typing import ClassVar
from unittest.mock import MagicMock, patch

from src import vision_cv
from src.sender import (
    SEND_BUTTON_SELECTORS,
    click_friend,
    confirm_send,
    cv_send_fallback,
    last_bubble_is_self,
    press_enter,
    send_message,
    send_sticker,
    synthetic_click_row,
    try_send_button_or_enter,
    verify_chat_opened,
    verify_chat_opened_ready,
    xpath_text_literal,
)


class TestXpathTextLiteral(unittest.TestCase):
    def test_single_quote_in_text(self):
        result = xpath_text_literal("it's")
        self.assertEqual(result, '"it\'s"')

    def test_double_quote_in_text(self):
        result = xpath_text_literal('he said "hi"')
        self.assertEqual(result, "'he said \"hi\"'")

    def test_no_quotes(self):
        result = xpath_text_literal("hello")
        self.assertEqual(result, "'hello'")


class TestPressEnter(unittest.TestCase):
    def test_press_enter_success(self):
        page = MagicMock()
        press_enter(page)
        page.keyboard.press.assert_called_once_with("Enter")

    def test_press_enter_exception(self):
        page = MagicMock()
        page.keyboard.press.side_effect = Exception("keyboard error")
        press_enter(page)  # Should not raise
        page.keyboard.press.assert_called_once_with("Enter")


class TestTrySendButtonOrEnter(unittest.TestCase):
    def test_send_button_found(self):
        page = MagicMock()
        btn_mock = MagicMock()
        btn_mock.wait_for.return_value = None
        btn_mock.click.return_value = None
        page.locator.return_value.first = btn_mock

        result = try_send_button_or_enter(page)
        self.assertTrue(result)
        btn_mock.wait_for.assert_called()
        btn_mock.click.assert_called()

    def test_no_send_button_fallback_enter(self):
        page = MagicMock()
        btn_mock = MagicMock()
        btn_mock.wait_for.side_effect = Exception("not found")
        page.locator.return_value.first = btn_mock

        result = try_send_button_or_enter(page)
        self.assertTrue(result)
        page.keyboard.press.assert_called_with("Enter")


class TestSendMessage(unittest.TestCase):
    def setUp(self):
        self.page = MagicMock()
        self.box_mock = MagicMock()
        self.box_mock.click.return_value = None

    @patch("src.sender.find")
    def test_strategy_a_success(self, mock_find):
        mock_find.return_value = self.box_mock
        self.page.keyboard.type.return_value = None

        with patch("src.sender.try_send_button_or_enter") as mock_try:
            mock_try.return_value = True
            sent, method = send_message(self.page, "test text")

        self.assertTrue(sent)
        self.assertIn("策略A", method)
        self.page.evaluate.assert_called()

    @patch("src.sender.find")
    def test_strategy_a_fail_strategy_b_success(self, mock_find):
        mock_find.return_value = self.box_mock
        self.page.evaluate.return_value = {"ok": False, "reason": "inject failed"}

        sent, method = send_message(self.page, "test text")

        self.assertTrue(sent)
        self.assertIn("策略B", method)
        self.page.keyboard.type.assert_called_with("test text", delay=30)

    @patch("src.sender.find")
    def test_both_strategies_fail(self, mock_find):
        mock_find.return_value = self.box_mock
        self.box_mock.click.side_effect = Exception("click failed")
        self.page.evaluate.side_effect = Exception("eval failed")

        sent, method = send_message(self.page, "test text")

        self.assertFalse(sent)
        self.assertEqual(method, "策略A/B均失败")

    @patch("src.sender.find")
    def test_no_input_box(self, mock_find):
        mock_find.return_value = None

        sent, method = send_message(self.page, "test text")

        self.assertFalse(sent)
        self.assertEqual(method, "未定位到输入框")


def _tail(n: int, last: str = "", prev: str = "") -> dict:
    """构造 _JS_MSG_TAIL 的返回体。"""
    return {"n": n, "last": last, "prev": prev}


def _js_fake(
    tails,
    *,
    box_empty=True,
    emoji_btn=None,
    panel_visible=True,
    sticker=None,
    panel_closed=True,
):
    """按 JS 语义分发的 page.evaluate 替身。

    位置式 side_effect 列表太脆: 发送链每多一次读盘就会把返回值错位喂给下一个 JS,
    于是「测试绿但断言的是别的东西」。这里按脚本里的独有标记认人, tails 是依次返回的
    消息尾部证据(第 1 个 = 发送前基线, 之后每轮各 1 个)。
    """
    rest = list(tails)
    emoji_btn = emoji_btn or {"x": 100, "y": 200}
    sticker = sticker or {"x": 476, "y": 335}

    def fake(js, *args):
        if "cleaned" in js:  # _JS_BOX_EMPTY
            return box_empty
        if "messageMessageList" in js:  # _JS_MSG_TAIL
            return rest.pop(0) if rest else _tail(0)
        if ".test(m)" in js:  # JS_FIND_EMOJI_BTN
            return emoji_btn
        if "sticker_not_found" in js:  # JS_FIND_STICKER
            return sticker
        if ">= 8) return true" in js:  # JS_PANEL_VISIBLE
            return panel_visible
        if ">= 8) return false" in js:  # JS_PANEL_CLOSED
            return panel_closed
        raise AssertionError(f"未预期 JS: {js[:60]}")

    return fake


class TestConfirmSend(unittest.TestCase):
    def setUp(self):
        self.page = MagicMock()

    def test_confirm_success_empty_and_match(self):
        self.page.evaluate.side_effect = [True, "续火花 7天"]
        ok, detail = confirm_send(self.page, "续火花")
        self.assertTrue(ok)
        self.assertIn("确认成功", detail)

    def test_confirm_success_empty_only(self):
        self.page.evaluate.side_effect = [True, ""]
        ok, detail = confirm_send(self.page, "续火花")
        self.assertTrue(ok)
        self.assertIn("输入框已清空", detail)

    def test_confirm_success_match_only(self):
        self.page.evaluate.side_effect = [False, "续火花 7天"]
        ok, detail = confirm_send(self.page, "续火花")
        self.assertTrue(ok)
        self.assertIn("最新消息包含文案", detail)

    def test_confirm_fail(self):
        self.page.evaluate.side_effect = itertools.cycle([False, "其他内容"])
        ok, detail = confirm_send(self.page, "续火花")
        self.assertFalse(ok)
        self.assertIn("未能确认发送", detail)

    def test_confirm_exception(self):
        self.page.evaluate.side_effect = Exception("eval error")
        ok, detail = confirm_send(self.page, "续火花")
        self.assertFalse(ok)
        self.assertIn("确认发送时出错", detail)

    def test_baseline_new_bubble_confirms(self):
        """带基线且确有新增 → 硬确认(文案相符)。"""
        page = MagicMock()
        page.evaluate.side_effect = _js_fake(
            [_tail(4, "续火花", "昨天的话")], box_empty=False
        )
        ok, detail = confirm_send(page, "续火花", baseline=(3, "昨天的话", "更早"))
        self.assertTrue(ok, detail)
        self.assertIn("新增气泡 3->4", detail)

    def test_empty_box_without_new_bubble_is_failure(self):
        """旧捷径①(输入框清空即成功)已废除: 字没打进去与已发出是两回事。"""
        page = MagicMock()
        base = (3, "昨天的话", "更早")
        page.evaluate.side_effect = _js_fake([_tail(*base)] * 4, box_empty=True)
        ok, detail = confirm_send(page, "续火花", retries=2, baseline=base)
        self.assertFalse(ok)
        self.assertIn("未能确认发送", detail)
        self.assertIn("无任何新增", detail)

    def test_stale_matching_text_without_new_bubble_is_failure(self):
        """旧捷径②(尾部含固定文案即成功)已废除: 昨晚那条「续火花」永远还挂在尾部。"""
        page = MagicMock()
        base = (9, "续火花 7天", "昨天的话")
        page.evaluate.side_effect = _js_fake([_tail(*base)] * 4)
        ok, detail = confirm_send(page, "续火花", retries=2, baseline=base)
        self.assertFalse(ok)
        self.assertIn("无任何新增", detail)

    def test_shrinking_count_degrades(self):
        """数量收缩 = 列表重渲染, 基线不可比 → 降级而非判失败。"""
        page = MagicMock()
        page.evaluate.side_effect = _js_fake(
            [_tail(2, "新尾巴", "")] * 2, box_empty=True
        )
        ok, detail = confirm_send(page, "续火花", baseline=(7, "旧尾巴", "更早"))
        self.assertTrue(ok)
        self.assertIn("降级", detail)
        self.assertIn("收缩 7->2", detail)

    def test_unreadable_list_degrades_instead_of_failing(self):
        """数量恒 0 = 列表容器没定位到 → 只能降级, 绝不能当「没发出去」(会引发重复发送)。"""
        page = MagicMock()
        page.evaluate.side_effect = _js_fake([_tail(0)] * 2, box_empty=True)
        ok, detail = confirm_send(page, "续火花", baseline=(0, "", ""))
        self.assertTrue(ok)
        self.assertIn("降级", detail)
        self.assertIn("数量恒 0", detail)


class TestClickFriend(unittest.TestCase):
    def setUp(self):
        self.page = MagicMock()

    def test_strategy_get_by_text_exact(self):
        loc_mock = MagicMock()
        loc_mock.wait_for.return_value = None
        loc_mock.scroll_into_view_if_needed.return_value = None
        loc_mock.click.return_value = None
        self.page.get_by_text.return_value.first = loc_mock

        result = click_friend(self.page, "测试好友")

        self.assertIsNotNone(result)
        self.page.get_by_text.assert_called_with("测试好友", exact=True)

    def test_strategy_xpath_fallback(self):
        loc1 = MagicMock()
        loc1.wait_for.side_effect = Exception("not found")
        self.page.get_by_text.return_value.first = loc1

        loc2 = MagicMock()
        loc2.wait_for.return_value = None
        loc2.scroll_into_view_if_needed.return_value = None
        loc2.click.return_value = None
        self.page.locator.return_value.first = loc2

        result = click_friend(self.page, "测试好友")

        self.assertIsNotNone(result)
        self.page.locator.assert_called()

    def test_all_strategies_fail(self):
        for _ in range(3):
            loc = MagicMock()
            loc.wait_for.side_effect = Exception("not found")
            self.page.get_by_text.return_value.first = loc
            self.page.locator.return_value.first = loc

        with patch("src.sender.dump_page") as mock_dump:
            mock_dump.return_value = ""
            result = click_friend(self.page, "测试好友")

        self.assertIsNone(result)


class TestSendSticker(unittest.TestCase):
    def setUp(self):
        self.page = MagicMock()

    def test_emoji_button_not_found(self):
        self.page.evaluate.return_value = None

        sent, method = send_sticker(self.page, "续火花")

        self.assertFalse(sent)
        self.assertEqual(method, "未找到表情按钮")

    def test_panel_not_opened(self):
        # First call: find emoji button, next 12: panel visible check (range(12))
        self.page.evaluate.side_effect = [
            {"x": 100, "y": 200},  # emoji button
        ] + [False] * 12  # panel visible check x12 (放宽后的等待窗口)

        sent, method = send_sticker(self.page, "续火花")

        self.assertFalse(sent)
        self.assertEqual(method, "表情面板未打开")

    def test_panel_content_not_loaded(self):
        self.page.evaluate.side_effect = [
            {"x": 100, "y": 200},  # emoji button
            True,
            True,
            True,
            True,
            True,
            True,  # panel visible x6
        ]
        self.page.wait_for_selector.side_effect = Exception("timeout")

        sent, method = send_sticker(self.page, "续火花")

        self.assertFalse(sent)
        self.assertEqual(method, "表情面板内容未加载")

    def test_sticker_not_found_in_panel(self):
        # Actual call sequence:
        # 1. evaluate(JS_FIND_EMOJI_BTN) -> {"x": 100, "y": 200}
        # 2. evaluate(JS_PANEL_VISIBLE) -> True (breaks loop after 1)
        # 3. wait_for_selector (not evaluate)
        # 4. evaluate(JS_FIND_STICKER, name) -> {"error": "sticker_not_found"}
        # Then loop 6 times: read_tail(_JS_MSG_TAIL) + evaluate(JS_PANEL_CLOSED)
        # (本例在 find sticker 处就返回, 后续值不会被消费, 留着只为暴露序列)
        self.page.evaluate.side_effect = (
            [
                {"x": 100, "y": 200},  # emoji button
                True,  # panel visible (breaks after 1)
                {"error": "sticker_not_found"},  # find sticker
            ]
            + [""] * 6
            + [False] * 6
        )  # last_msg x6, panel_closed x6

        sent, method = send_sticker(self.page, "续火花")

        self.assertFalse(sent)
        self.assertIn("未找到", method)

    def test_exception_handling(self):
        self.page.evaluate.side_effect = Exception("JS error")

        sent, method = send_sticker(self.page, "续火花")

        self.assertFalse(sent)
        self.assertIn("表情发送异常", method)

    def test_success_requires_new_bubble(self):
        """硬确认: 点击表情后气泡数增加 → 成功, 且说明里带上新增证据。"""
        self.page.evaluate.side_effect = _js_fake(
            [_tail(3, "昨天的话", "更早"), _tail(4, "续火花", "昨天的话")]
        )
        sent, method = send_sticker(self.page, "续火花")
        self.assertTrue(sent, method)
        self.assertIn("新增气泡 3->4", method)

    def test_panel_closed_without_new_bubble_is_failure(self):
        """2026-10-07 审计的核心漏洞: 面板关闭曾被当成「抖音已自动发送」的充分证据。

        表情点了个空(定位过期/面板切换)同样会关掉面板, 而列表一条没多 → 必须判失败,
        否则 scheduler 直接 return ok 并写 state, 火花断了且当晚无人知道。
        """
        base = _tail(3, "昨天的话", "更早")
        self.page.evaluate.side_effect = _js_fake([base] * 8, panel_closed=True)
        sent, method = send_sticker(self.page, "续火花")
        self.assertFalse(sent)
        self.assertIn("判未发送", method)
        self.assertIn("无任何新增", method)

    def test_stale_sticker_text_is_not_success(self):
        """昨晚那条「续火花」表情一直挂在尾部: 文本命中但数量没变 → 不算本次发送。"""
        base = _tail(5, "续火花 7天", "昨天的话")
        self.page.evaluate.side_effect = _js_fake([base] * 8, panel_closed=True)
        sent, method = send_sticker(self.page, "续火花")
        self.assertFalse(sent)
        self.assertIn("判未发送", method)

    def test_unreadable_list_keeps_legacy_panel_closed_success(self):
        """读不到列表(数量恒 0)时回到旧口径, 但必须标注「降级」让 strat_stats 数得出来。"""
        self.page.evaluate.side_effect = _js_fake([_tail(0)] * 8, panel_closed=True)
        sent, method = send_sticker(self.page, "续火花")
        self.assertTrue(sent, method)
        self.assertIn("降级", method)
        self.assertIn("面板已关闭", method)


class TestSyntheticClickRow(unittest.TestCase):
    def test_returns_hit_from_evaluate(self):
        page = MagicMock()
        page.evaluate.return_value = {"x": 185, "y": 562}
        self.assertEqual(synthetic_click_row(page, "某某"), {"x": 185, "y": 562})
        args = page.evaluate.call_args[0]
        self.assertIn("pointerdown", args[0])
        self.assertEqual(args[1], "某某")

    def test_missing_name_returns_none(self):
        page = MagicMock()
        page.evaluate.return_value = None
        self.assertIsNone(synthetic_click_row(page, "某某"))

    def test_evaluate_exception_returns_none(self):
        page = MagicMock()
        page.evaluate.side_effect = Exception("boom")
        self.assertIsNone(synthetic_click_row(page, "某某"))


class TestVerifyChatOpened(unittest.TestCase):
    """发送前身份核对三态: 确认/冲突拦截/读不懂放行。"""

    def setUp(self):
        self.page = MagicMock()

    def test_name_in_panel_passes(self):
        self.page.evaluate.return_value = {
            "box": True,
            "hits": 2,
            "panel_len": 500,
            "top_texts": ["测试好友"],
        }
        ok, detail = verify_chat_opened(self.page, "测试好友")
        self.assertTrue(ok)
        self.assertIn("确认", detail)

    def test_panel_rendered_without_name_blocks(self):
        self.page.evaluate.return_value = {
            "box": True,
            "hits": 0,
            "panel_len": 500,
            "top_texts": ["另一个好友", "对方正在输入"],
        }
        ok, detail = verify_chat_opened(self.page, "测试好友")
        self.assertFalse(ok)
        self.assertIn("冲突", detail)
        self.assertIn("另一个好友", detail)

    def test_unreadable_panel_passes(self):
        self.page.evaluate.return_value = {
            "box": False,
            "hits": 0,
            "panel_len": 0,
            "top_texts": [],
        }
        ok, detail = verify_chat_opened(self.page, "测试好友")
        self.assertTrue(ok)
        self.assertIn("放行", detail)

    def test_js_exception_passes(self):
        self.page.evaluate.side_effect = Exception("boom")
        ok, detail = verify_chat_opened(self.page, "测试好友")
        self.assertTrue(ok)
        self.assertIn("放行", detail)


class TestVerifyChatOpenedReady(unittest.TestCase):
    """主链路身份核对: 骨架期先复检再裁决(2026-10-07 叶文j/小淇淇 盲发事故)。

    判据两条: 读得到时**一次都不等**(正常好友零额外耗时), 读不到时终态语义与
    verify_chat_opened 一致(只有 conflict 拦截, 其余放行), 不新增中止分支。
    """

    SKELETON: ClassVar[dict] = {
        "box": False,
        "hits": 0,
        "panel_len": 0,
        "top_texts": [],
    }
    CONFIRMED: ClassVar[dict] = {
        "box": True,
        "hits": 1,
        "panel_len": 500,
        "top_texts": ["测试好友"],
    }
    CONFLICT: ClassVar[dict] = {
        "box": True,
        "hits": 0,
        "panel_len": 500,
        "top_texts": ["别人"],
    }

    def setUp(self):
        self.page = MagicMock()

    def test_ready_panel_waits_nothing(self):
        self.page.evaluate.return_value = self.CONFIRMED
        ok, detail = verify_chat_opened_ready(self.page, "测试好友")
        self.assertTrue(ok)
        self.assertIn("确认", detail)
        self.page.wait_for_timeout.assert_not_called()
        self.assertEqual(self.page.evaluate.call_count, 1)

    def test_skeleton_then_confirmed_passes_after_recheck(self):
        self.page.evaluate.side_effect = [self.SKELETON, self.CONFIRMED]
        ok, detail = verify_chat_opened_ready(self.page, "测试好友")
        self.assertTrue(ok)
        self.assertIn("确认", detail)
        self.assertEqual(self.page.evaluate.call_count, 2)
        self.page.wait_for_timeout.assert_called_once_with(4000)

    def test_skeleton_then_conflict_blocks(self):
        """骨架→渲染完成时读到的是别人的会话: 拦截(这条以前会被盲发)。"""
        self.page.evaluate.side_effect = [self.SKELETON, self.CONFLICT, self.CONFLICT]
        ok, detail = verify_chat_opened_ready(self.page, "测试好友")
        self.assertFalse(ok)
        self.assertIn("冲突", detail)

    def test_single_conflict_read_is_rechecked_not_blocking(self):
        """首读判冲突可能是半渲染(消息区有文本、标题栏昵称还没挂上)。
        root 修好后 conflict 会真的拦下发送, 所以必须连续两读成立才拦 —— 否则
        一次时序抖动就把「本来能发」变成漏发。"""
        self.page.evaluate.side_effect = [self.CONFLICT, self.CONFIRMED]
        ok, detail = verify_chat_opened_ready(self.page, "测试好友")
        self.assertTrue(ok)
        self.assertIn("确认", detail)
        self.assertEqual(self.page.evaluate.call_count, 2)

    def test_always_skeleton_still_passes(self):
        """复检用尽仍读不到 → 维持原放行语义(不新增中止分支)。"""
        self.page.evaluate.return_value = self.SKELETON
        ok, detail = verify_chat_opened_ready(self.page, "测试好友", retries=2)
        self.assertTrue(ok)
        self.assertIn("放行", detail)
        self.assertIn("已复检 2 次", detail)
        self.assertEqual(self.page.evaluate.call_count, 3)

    def test_strict_skeleton_blocks(self):
        """复用上一个好友的页面时: 读不到 = 右栏可能还是上一位(半渲染瞬间),
        放行等于把消息发给不确定的人, 所以 strict 下必须拦。"""
        self.page.evaluate.return_value = self.SKELETON
        ok, detail = verify_chat_opened_ready(self.page, "测试好友", strict=True)
        self.assertFalse(ok)
        self.assertIn("不放行", detail)

    def test_strict_confirmed_still_passes(self):
        """strict 只收紧「读不到」, 确认过的会话照常放行(正常好友零额外耗时)。"""
        self.page.evaluate.return_value = self.CONFIRMED
        ok, detail = verify_chat_opened_ready(self.page, "测试好友", strict=True)
        self.assertTrue(ok)
        self.assertIn("确认", detail)
        self.assertEqual(self.page.evaluate.call_count, 1)


class TestCvSendFallback(unittest.TestCase):
    """Emergency Visual Fallback: 定位→验证→点击→确认 的闸链与降级。"""

    def setUp(self):
        self.page = MagicMock()
        self.er = {"x": 320, "y": 530, "w": 380, "h": 40, "vw": 800}

    def _shot(self, circle=(755, 561)):
        # 延迟导入: 仅 cv2 可用的环境会走到这里(skipUnless 已守卫)
        import cv2
        import numpy as np

        img = np.zeros((600, 800, 3), dtype=np.uint8)
        img[:, :] = (45, 42, 40)
        cv2.circle(img, circle, 14, (0, 40, 230), -1)
        return cv2.imencode(".png", img)[1].tobytes()

    def _eval(self, identity_ok=True):
        identity = {
            "box": True,
            "hits": 1 if identity_ok else 0,
            "panel_len": 300,
            "top_texts": ["测试好友"],
        }

        def fake(js, *args):
            if "panel_len" in js:  # _JS_CHAT_IDENTITY(也含 innerWidth, 必须先判)
                return identity
            if "innerWidth" in js:
                return self.er
            if "inputType" in js:
                return True
            if "cleaned" in js:
                return True
            if "messageMessageList" in js:
                return "续火花"
            raise AssertionError(f"未预期 JS: {js[:60]}")

        return fake

    @unittest.skipUnless(vision_cv.available(), "需要 opencv")
    def test_happy_path_clicks_candidate(self):
        self.page.screenshot.return_value = self._shot()
        self.page.evaluate.side_effect = self._eval()
        ok, detail = cv_send_fallback(self.page, "续火花", "测试好友")
        self.assertTrue(ok, detail)
        x, y = self.page.mouse.click.call_args[0]
        self.assertAlmostEqual(x, 755, delta=2)
        self.assertAlmostEqual(y, 561, delta=2)

    @unittest.skipUnless(vision_cv.available(), "需要 opencv")
    def test_not_found_no_click_and_clears(self):
        self.page.screenshot.return_value = self._shot(circle=(100, 100))  # ROI 外
        self.page.evaluate.side_effect = self._eval()
        ok, detail = cv_send_fallback(self.page, "续火花", "测试好友")
        self.assertFalse(ok)
        self.assertIn("未找到", detail)
        self.page.mouse.click.assert_not_called()
        # 清空草稿: SET_BOX_TEXT 被以 "" 调过
        clears = [c for c in self.page.evaluate.call_args_list if c[0][-1] == ""]
        self.assertTrue(clears)

    @unittest.skipUnless(vision_cv.available(), "需要 opencv")
    def test_identity_conflict_blocks_click(self):
        self.page.screenshot.return_value = self._shot()
        self.page.evaluate.side_effect = self._eval(identity_ok=False)
        ok, detail = cv_send_fallback(self.page, "续火花", "测试好友")
        self.assertFalse(ok)
        self.assertIn("身份复核", detail)
        self.page.mouse.click.assert_not_called()

    def test_cv2_unavailable_degrades(self):
        with patch.object(vision_cv, "available", return_value=False):
            ok, detail = cv_send_fallback(self.page, "续火花", "测试好友")
        self.assertFalse(ok)
        self.assertIn("cv2", detail)
        self.page.mouse.click.assert_not_called()

    def test_no_editable_aborts(self):
        self.page.evaluate.return_value = None
        ok, detail = cv_send_fallback(self.page, "续火花", "测试好友")
        self.assertFalse(ok)
        self.assertIn("输入框", detail)


if __name__ == "__main__":
    unittest.main()


class TestGeometrySendButton(unittest.TestCase):
    """2026-11-01 板端取证: 发送键是纯图标, 必须靠几何+命中校验点它。"""

    def _fake(self, cands, emptied):
        state = {"n": 0}

        def fake(js, *args):
            if "cands" in js:  # _JS_FIND_SEND_BTN
                return {"cands": cands}
            if "cleaned" in js:  # _JS_BOX_EMPTY
                i = min(state["n"], len(emptied) - 1)
                state["n"] += 1
                return emptied[i]
            raise AssertionError(f"未预期 JS: {js[:60]}")

        return fake

    def test_clicks_rightmost_candidate(self):
        page = MagicMock()
        page.evaluate.side_effect = self._fake(
            [{"x": 755, "y": 561, "tag": "SPAN", "cls": "send"}], [True]
        )
        desc = try_send_button_or_enter(page)
        self.assertIn("几何", desc)
        page.mouse.click.assert_called_once_with(755, 561)

    def test_skips_candidate_that_did_not_send(self):
        page = MagicMock()
        page.evaluate.side_effect = self._fake(
            [
                {"x": 755, "y": 561, "tag": "SPAN", "cls": "send"},
                {"x": 708, "y": 562, "tag": "SPAN", "cls": "emoji"},
            ],
            [False, True],
        )
        desc = try_send_button_or_enter(page)
        self.assertIn("(708,562", desc)
        self.assertEqual(page.mouse.click.call_count, 2)
        page.keyboard.press.assert_called_with("Escape")

    def test_no_candidate_falls_back_to_enter(self):
        page = MagicMock()
        page.evaluate.side_effect = self._fake([], [True])
        page.locator.return_value.first.wait_for.side_effect = Exception("nope")
        desc = try_send_button_or_enter(page)
        self.assertIn("Enter", desc)
        page.keyboard.press.assert_called_with("Enter")


class TestStickerOcclusion(unittest.TestCase):
    def test_occluded_item_is_not_clicked(self):
        """目标表情被别的层盖住时不许瞎点: 直接失败交给文字层, 别浪费 6s 等面板。"""
        page = MagicMock()
        page.evaluate.side_effect = [
            {"x": 708, "y": 562},  # emoji button
            True,  # panel visible
            {
                "error": "sticker_occluded",
                "at": {"x": 476, "y": 335, "tag": "DIV", "cls": "chatList"},
            },
        ]
        sent, method = send_sticker(page, "续火花")
        self.assertFalse(sent)
        self.assertIn("遮挡", method)
        page.mouse.click.assert_called_once_with(708, 562)


class TestSendButtonSelectors(unittest.TestCase):
    """2026-11-01 板端 DOM 实测: 发送键是 <svg class="messageMsgInputpublishBtn
    e2e-send-msg-btn">, 没有「发送」文字也没有 data-e2e 属性。历史上的五条选择器
    全靠文案/属性, 一条都不命中 → 每晚「未找到发送按钮, 改用 Enter」→ 文字层整月
    假成功。防止有人把类名选择器删掉。
    """

    def test_class_based_selectors_present(self):
        self.assertIn("svg[class*='publishBtn']", SEND_BUTTON_SELECTORS)
        self.assertIn("[class*='e2e-send-msg-btn']", SEND_BUTTON_SELECTORS)

    def test_class_selectors_tried_before_text_selectors(self):
        cls_idx = SEND_BUTTON_SELECTORS.index("svg[class*='publishBtn']")
        text_idx = SEND_BUTTON_SELECTORS.index("button:has-text('发送')")
        self.assertLess(cls_idx, text_idx)


class TestLastBubbleIsSelf(unittest.TestCase):
    """2026-11-01 会话50: 表情层判据「最后一条文本含表情名」分不清是谁发的 ——
    续火花是双方互发同一个表情, 好友回一个同款也会命中。这里只测旁证函数本身,
    判定逻辑不变(今晚不改发送行为)。
    """

    def test_returns_flag_from_page(self):
        page = MagicMock()
        page.evaluate.return_value = True
        self.assertIs(last_bubble_is_self(page), True)
        page.evaluate.return_value = False
        self.assertIs(last_bubble_is_self(page), False)

    def test_unreadable_returns_none(self):
        page = MagicMock()
        page.evaluate.return_value = None
        self.assertIsNone(last_bubble_is_self(page))
        page.evaluate.return_value = "yes"
        self.assertIsNone(last_bubble_is_self(page))

    def test_exception_returns_none(self):
        page = MagicMock()
        page.evaluate.side_effect = RuntimeError("boom")
        self.assertIsNone(last_bubble_is_self(page))
