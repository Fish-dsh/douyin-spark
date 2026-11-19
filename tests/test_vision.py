"""vision.py tests: JSON 解析容错、开关门禁、脱敏截图→取证端到端(不真调网)。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.vision import (
    _parse_points,
    find_row,
    locate_rows,
    vision_enabled,
)

_ROWS = [
    {
        "name": "张三",
        "hasStreak": True,
        "iconSrc": "flame_gray.png",
        "streakNum": 5,
        "isPinned": False,
        "method": "vision",
        "x": 96,
        "y": 320,
    }
]


class FakePage:
    """最小页面替身: evaluate 分「脱敏(单参)」与「取证(带参)」两种返回。"""

    def __init__(self, rows=None, screenshot_ok=True):
        self.rows = rows if rows is not None else []
        self.screenshot_ok = screenshot_ok

    def evaluate(self, js, arg=None):
        if arg is None:
            return 3  # 脱敏遮罩数量
        return self.rows

    def screenshot(self, path=None, **_kw):
        if self.screenshot_ok and path:
            Path(path).write_bytes(b"\x89PNG fake")


class TestParsePoints(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(_parse_points('[{"x":1,"y":2}]'), [{"x": 1, "y": 2}])

    def test_fenced(self):
        text = '```json\n[{"x":3,"y":4}]\n```'
        self.assertEqual(_parse_points(text), [{"x": 3, "y": 4}])

    def test_garbage(self):
        self.assertEqual(_parse_points("没有 JSON"), [])

    def test_partial_entries_skipped(self):
        text = '[{"x":"a","y":2},{"x":1,"y":2}]'
        self.assertEqual(_parse_points(text), [{"x": 1, "y": 2}])

    def test_empty(self):
        self.assertEqual(_parse_points(""), [])


class TestVisionEnabled(unittest.TestCase):
    @patch("src.vision._config", return_value={})
    def test_default_on(self, _c):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SPARK_VISION", None)
            self.assertTrue(vision_enabled())

    @patch("src.vision._config", return_value={"vision_fallback": False})
    def test_config_off(self, _c):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SPARK_VISION", None)
            self.assertFalse(vision_enabled())

    @patch("src.vision._config", return_value={"vision_fallback": True})
    def test_env_wins(self, _c):
        with patch.dict(os.environ, {"SPARK_VISION": "0"}):
            self.assertFalse(vision_enabled())
        with patch.dict(os.environ, {"SPARK_VISION": "1"}):
            self.assertTrue(vision_enabled())


class TestRedactionScript(unittest.TestCase):
    """脱敏脚本的覆盖面(结构性断言: 无浏览器环境, 只能查脚本内容)。

    真正的像素效果要靠真机截图核验; 这里守的是「后来的人别把某类可泄露元素
    的遮罩删掉」—— 2026-11-19 两轮都是漏了某一类(先是右侧会话, 再是输入框/
    canvas/video/背景图), 全是靠人工看图才发现的。
    """

    def test_covers_whole_viewport(self):
        """遮罩边界必须是整个视口。曾经是 vw * 0.45, 右侧已打开的会话明文外发。"""
        from src.vision import _JS_REDACT

        self.assertIn("const limitX = vw;", _JS_REDACT)
        self.assertNotIn("vw * 0.45", _JS_REDACT)

    def test_covers_elements_without_text_nodes(self):
        """输入框的值不在文本节点里, canvas/video/背景图也不在 —— 必须单独盖。"""
        from src.vision import _JS_REDACT

        for sel in ("input", "textarea", "select", "canvas", "video"):
            self.assertIn(sel, _JS_REDACT, f"脱敏脚本漏了 {sel}")
        self.assertIn("backgroundImage", _JS_REDACT)

    def test_keeps_flame_icon(self):
        """火花图标必须留着: 行定位与「有没有火花」的判定都靠它。"""
        from src.vision import _JS_REDACT

        self.assertIn("flame", _JS_REDACT)
        self.assertIn("if (flame.test(s)) continue;", _JS_REDACT)


class TestLocateRows(unittest.TestCase):
    def test_happy_path(self):
        page = FakePage(rows=_ROWS)
        with (
            tempfile.TemporaryDirectory() as td,
            patch("src.vision._SHOT_DIR", Path(td)),
            patch("src.vision.vision_enabled", return_value=True),
            patch("src.vision._provider_chain", return_value=["agnes"]),
            patch("src.vision._call", return_value=[{"x": 96, "y": 320}]) as call,
        ):
            out = locate_rows(page)
        self.assertEqual(out, _ROWS)
        call.assert_called_once()

    def test_no_key_returns_empty(self):
        with (
            patch("src.vision.vision_enabled", return_value=True),
            patch("src.vision._provider_chain", return_value=[]),
        ):
            self.assertEqual(locate_rows(FakePage(rows=_ROWS)), [])

    def test_screenshot_failure_returns_empty(self):
        page = FakePage(rows=_ROWS, screenshot_ok=False)
        with (
            tempfile.TemporaryDirectory() as td,
            patch("src.vision._SHOT_DIR", Path(td)),
            patch("src.vision.vision_enabled", return_value=True),
            patch("src.vision._provider_chain", return_value=["agnes"]),
            patch("src.vision._call", return_value=[{"x": 1, "y": 1}]),
        ):
            self.assertEqual(locate_rows(page), [])

    def test_model_no_points_returns_empty(self):
        page = FakePage(rows=_ROWS)
        with (
            tempfile.TemporaryDirectory() as td,
            patch("src.vision._SHOT_DIR", Path(td)),
            patch("src.vision.vision_enabled", return_value=True),
            patch("src.vision._provider_chain", return_value=["agnes"]),
            patch("src.vision._call", return_value=[]),
        ):
            self.assertEqual(locate_rows(page), [])

    def test_exception_swallowed(self):
        page = MagicMock()
        page.evaluate.side_effect = RuntimeError("boom")
        with patch("src.vision._provider_chain", return_value=["agnes"]):
            self.assertEqual(locate_rows(page), [])


class TestFindRow(unittest.TestCase):
    def test_matches_exact_name(self):
        rows = [{"name": "A", "x": 1, "y": 2}, {"name": "B", "x": 3, "y": 4}]
        with patch("src.vision.locate_rows", return_value=rows):
            self.assertEqual(find_row(MagicMock(), "B")["y"], 4)

    def test_no_match_returns_none(self):
        with patch("src.vision.locate_rows", return_value=[{"name": "A"}]):
            self.assertIsNone(find_row(MagicMock(), "Z"))

    def test_never_matches_substring(self):
        """昵称必须完全相等 —— 子串命中会点错好友。"""
        with patch("src.vision.locate_rows", return_value=[{"name": "张三丰"}]):
            self.assertIsNone(find_row(MagicMock(), "张三"))


class TestSparkStatusIntegration(unittest.TestCase):
    """四条 DOM 策略全失效时, 视觉兜底的结果应被 spark_status 采用。"""

    def setUp(self):
        self.page = MagicMock()

    def test_vision_rows_used_when_selectors_dead(self):
        from src import spark_status

        vis_rows = [
            {
                "name": "好友X",
                "hasStreak": True,
                "iconSrc": "flame_gray.png",
                "streakNum": 9,
                "method": "vision",
            }
        ]
        with (
            patch.object(
                spark_status,
                "_run_discover",
                side_effect=spark_status.SparkDiscoveryError("dead"),
            ),
            patch.object(spark_status, "_wait_for_streaks", return_value=True),
            patch.object(spark_status.vision, "vision_enabled", return_value=True),
            patch.object(spark_status.vision, "locate_rows", return_value=vis_rows),
        ):
            result = spark_status.discover_friends_with_spark(self.page)
        self.assertEqual([r["name"] for r in result], ["好友X"])
        self.assertEqual(result[0]["streak_days"], 9)

    def test_vision_unavailable_still_raises(self):
        from src import spark_status

        with (
            patch.object(
                spark_status,
                "_run_discover",
                side_effect=spark_status.SparkDiscoveryError("dead"),
            ),
            patch.object(spark_status, "_wait_for_streaks", return_value=True),
            patch.object(spark_status.vision, "vision_enabled", return_value=True),
            patch.object(spark_status.vision, "locate_rows", return_value=[]),
            self.assertRaises(spark_status.SparkDiscoveryError),
        ):
            spark_status.discover_friends_with_spark(self.page)
