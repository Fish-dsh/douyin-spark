"""spark_status.py tests: three-strategy merge, status classification, scroll dedup."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from src.spark_status import (
    SparkDiscoveryError,
    _classify_icon,
    _run_discover,
    discover_friends_with_spark,
)

HTML_FIXTURE_NORMAL = """
<div class="conversationConversationItemwrapper-abc123">
  <div class="conversationConversationItemtitleWrapper-xyz">
    <span class="conversationConversationItemtitle-def">test_friend</span>
  </div>
  <div class="commonStreakstreakContainer-ghi">
    <img class="commonStreakicon-jkl"
         src="https://p9.douyinpic.com/streak/flame_gray_32.png" />
    <span class="commonStreaknormalText-mno">5</span>
  </div>
</div>
"""


def _item(
    name, has_streak=True, icon="flame_gray.png", num=5, method="class", pinned=False
):
    d = {
        "name": name,
        "hasStreak": has_streak,
        "iconSrc": icon,
        "streakNum": num,
        "method": method,
    }
    if method == "class":
        d["isPinned"] = pinned
    return d


def _mock_eval(page, class_r=None, text_r=None, icon_r=None, generic_r=None):
    """Route page.evaluate calls by detecting method marker in JS string."""
    from src.spark_status import (
        _JS_DISCOVER_BY_CLASS,
        _JS_DISCOVER_BY_ICON,
        _JS_DISCOVER_BY_TEXT,
        _JS_DISCOVER_GENERIC,
    )

    def side(js):
        if js.strip() == _JS_DISCOVER_BY_CLASS.strip():
            return class_r or []
        if js.strip() == _JS_DISCOVER_BY_TEXT.strip():
            return text_r or []
        if js.strip() == _JS_DISCOVER_BY_ICON.strip():
            return icon_r or []
        if js.strip() == _JS_DISCOVER_GENERIC.strip():
            return generic_r or []
        if "commonStreakstreakContainer" in js and "length" in js:
            return 1
        return None

    page.evaluate.side_effect = side


class TestClassifyIcon(unittest.TestCase):
    def test_empty_is_unknown(self):
        self.assertEqual(_classify_icon(""), "unknown")

    def test_gray_is_need(self):
        self.assertEqual(_classify_icon("flame_gray_32.png"), "need")

    def test_grey_is_need(self):
        self.assertEqual(_classify_icon("icon_grey.png"), "need")

    def test_colored_is_active(self):
        self.assertEqual(_classify_icon("flame_orange_32.png"), "active")

    def test_case_insensitive(self):
        self.assertEqual(_classify_icon("Flame_GRAY.png"), "need")
        self.assertEqual(_classify_icon("FLAME_ORANGE.png"), "active")


class TestRunDiscover(unittest.TestCase):
    def setUp(self):
        self.page = MagicMock()

    def test_class_finds_streak_friend(self):
        _mock_eval(self.page, class_r=[_item("A")])
        items = _run_discover(self.page)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "A")

    def test_filters_no_streak_no_pinned(self):
        _mock_eval(
            self.page,
            class_r=[_item("A", has_streak=False, icon="", num=0, pinned=False)],
        )
        self.assertEqual(_run_discover(self.page), [])

    def test_pinned_without_streak_kept(self):
        _mock_eval(
            self.page,
            class_r=[_item("P", has_streak=False, icon="", num=0, pinned=True)],
        )
        items = _run_discover(self.page)
        self.assertEqual(len(items), 1)

    def test_all_empty_raises_error(self):
        _mock_eval(self.page, class_r=[], text_r=[], icon_r=[])
        with self.assertRaises(SparkDiscoveryError):
            _run_discover(self.page)

    def test_class_exception_falls_to_text(self):
        from src.spark_status import (
            _JS_DISCOVER_BY_CLASS,
            _JS_DISCOVER_BY_ICON,
            _JS_DISCOVER_BY_TEXT,
        )

        n = [0]

        def side(js):
            if js.strip() == _JS_DISCOVER_BY_CLASS.strip():
                n[0] += 1
                raise RuntimeError("class DOM changed")
            if js.strip() == _JS_DISCOVER_BY_TEXT.strip():
                return [_item("T", method="text")]
            if js.strip() == _JS_DISCOVER_BY_ICON.strip():
                return []
            if "commonStreakstreakContainer" in js and "length" in js:
                return 1
            return None

        self.page.evaluate.side_effect = side
        items = _run_discover(self.page)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["method"], "text")

    def test_dedup_class_wins(self):
        _mock_eval(
            self.page,
            class_r=[_item("X", method="class")],
            text_r=[_item("X", method="text")],
        )
        items = _run_discover(self.page)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["method"], "class")

    def test_text_fills_class_gap(self):
        _mock_eval(self.page, class_r=[_item("A")], text_r=[_item("B", method="text")])
        names = {i["name"] for i in _run_discover(self.page)}
        self.assertEqual(names, {"A", "B"})

    def test_icon_fills_remaining_gap(self):
        _mock_eval(self.page, class_r=[], text_r=[], icon_r=[_item("C", method="icon")])
        items = _run_discover(self.page)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["method"], "icon")

    def test_generic_fills_when_class_names_dead(self):
        # 类名整体失效时, 结构兜底策略必须仍能发现好友
        _mock_eval(
            self.page,
            class_r=[],
            text_r=[],
            icon_r=[],
            generic_r=[_item("G", method="generic")],
        )
        items = _run_discover(self.page)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["method"], "generic")

    def test_class_wins_over_generic(self):
        _mock_eval(
            self.page,
            class_r=[_item("X", method="class")],
            generic_r=[_item("X", method="generic")],
        )
        items = _run_discover(self.page)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["method"], "class")


class TestDiscoverFriendsWithSpark(unittest.TestCase):
    def setUp(self):
        self.page = MagicMock()

    @patch("src.spark_status._wait_for_streaks", return_value=True)
    @patch("src.spark_status._run_discover")
    def test_status_classification(self, mock_run, _w):
        mock_run.return_value = [
            _item("need1", icon="flame_gray.png", num=5),
            _item("active1", icon="flame_orange.png", num=12),
            _item("pinned1", has_streak=False, icon="", num=0, pinned=True),
        ]
        self.page.evaluate.return_value = False
        result = discover_friends_with_spark(self.page)
        by_name = {r["name"]: r for r in result}
        self.assertEqual(by_name["need1"]["status"], "need")
        self.assertEqual(by_name["need1"]["streak_days"], 5)
        self.assertEqual(by_name["active1"]["status"], "active")
        self.assertEqual(by_name["pinned1"]["status"], "pinned")

    @patch("src.spark_status._wait_for_streaks", return_value=True)
    @patch("src.spark_status._run_discover")
    def test_scroll_dedup(self, mock_run, _w):
        first = [_item("A", num=3)]
        second = [_item("A", num=3), _item("B", icon="flame_grey.png", num=7)]
        mock_run.side_effect = [first, second]
        scrolls = [True, False]
        self.page.evaluate.side_effect = lambda js: (
            scrolls.pop(0) if "scroll" in js.lower() else None
        )
        result = discover_friends_with_spark(self.page)
        names = {r["name"] for r in result}
        self.assertEqual(names, {"A", "B"})

    @patch("src.spark_status._wait_for_streaks", return_value=True)
    @patch("src.spark_status._run_discover")
    def test_no_scroll_when_not_scrollable(self, mock_run, _w):
        mock_run.return_value = [_item("A")]
        self.page.evaluate.return_value = False
        result = discover_friends_with_spark(self.page)
        self.assertEqual(len(result), 1)
        self.assertEqual(mock_run.call_count, 1)

    @patch("src.spark_status._wait_for_streaks", return_value=True)
    @patch("src.spark_status._run_discover")
    def test_discovery_error_propagates(self, mock_run, _w):
        mock_run.side_effect = SparkDiscoveryError("all failed")
        with self.assertRaises(SparkDiscoveryError):
            discover_friends_with_spark(self.page)

    @patch("src.spark_status._wait_for_streaks", return_value=True)
    @patch("src.spark_status._run_discover")
    def test_generic_exception_returns_empty(self, mock_run, _w):
        mock_run.side_effect = RuntimeError("unexpected")
        result = discover_friends_with_spark(self.page)
        self.assertEqual(result, [])

    @patch("src.spark_status._wait_for_streaks", return_value=True)
    @patch("src.spark_status._run_discover")
    def test_empty_midsection_does_not_stop_scan(self, mock_run, _w):
        # 2026-10-07 事故回归: 列表中段连续无火花好友时循环绝不能按「无新增」提前停,
        # 只有滚动函数报告「滚不动了(False)」才结束, 否则中段之下的好友永久漏检。
        mock_run.side_effect = [
            [_item("A", num=3)],
            [],
            [],
            [_item("好友E", icon="flame_gray.png", num=4)],
        ]
        scrolls = [True, True, True, False]
        self.page.evaluate.side_effect = lambda js: (
            scrolls.pop(0) if "scroll" in js.lower() else None
        )
        result = discover_friends_with_spark(self.page)
        names = {r["name"] for r in result}
        self.assertEqual(names, {"A", "好友E"})


class TestScrollContract(unittest.TestCase):
    def test_scroll_steps_by_viewport_not_jump_to_bottom(self):
        from src.spark_status import _JS_SCROLL_LIST

        self.assertIn("el.clientHeight * 0.9", _JS_SCROLL_LIST)
        self.assertNotIn(
            "el.scrollTop = el.scrollHeight;",
            _JS_SCROLL_LIST,
            "跳到底会漏掉虚拟列表中段(2026-10-07 好友E漏检), 禁止回退",
        )


class TestJsSelectorConsistency(unittest.TestCase):
    def test_text_and_icon_share_keywords(self):
        from src.spark_status import _JS_DISCOVER_BY_ICON, _JS_DISCOVER_BY_TEXT

        kws = ["flame", "fire", "streak", "spark", "hot", "light", "normal", "blue"]
        for kw in kws:
            self.assertIn("'" + kw + "'", _JS_DISCOVER_BY_TEXT)
            self.assertIn("'" + kw + "'", _JS_DISCOVER_BY_ICON)

    def test_class_checks_isstickontop(self):
        from src.spark_status import _JS_DISCOVER_BY_CLASS

        self.assertIn("isStickOnTop", _JS_DISCOVER_BY_CLASS)

    def test_icon_uses_closest(self):
        from src.spark_status import _JS_DISCOVER_BY_ICON

        self.assertIn("closest", _JS_DISCOVER_BY_ICON)
        self.assertIn("conversationConversationItemwrapper", _JS_DISCOVER_BY_ICON)

    def test_generic_js_free_of_obfuscated_class_names(self):
        # 结构兜底策略的价值就在于不锚定混淆类名; 若引入即失去抗改版意义
        from src.spark_status import _JS_DISCOVER_GENERIC

        for banned in (
            "conversationConversationItem",
            "commonStreak",
            "isStickOnTop",
        ):
            self.assertNotIn(banned, _JS_DISCOVER_GENERIC)

    def test_generic_has_streak_keywords(self):
        from src.spark_status import _JS_DISCOVER_GENERIC

        for kw in ("flame", "fire", "streak", "spark"):
            self.assertIn("'" + kw + "'", _JS_DISCOVER_GENERIC)


if __name__ == "__main__":
    unittest.main()
