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
    """最小页面替身: evaluate 分「脱敏(单参)」与「取证(带参)」两种返回。

    脱敏分支现在回报结构化对象(masked/textNodes/uncovered/unverified/
    unreadable/keptFlame), 不再是一个裸计数 —— 覆盖自检既要「遮了几处」,
    也要「还有几处没被盖住」「有几处没验证完」。
    """

    def __init__(self, rows=None, screenshot_ok=True, redact=None):
        self.rows = rows if rows is not None else []
        self.screenshot_ok = screenshot_ok
        self.redact = redact if redact is not None else {
            "masked": 3,
            "textNodes": 3,
            "uncovered": 0,
            "unverified": 0,
            "unreadable": 0,
            "keptFlame": 1,
        }

    def evaluate(self, js, arg=None):
        if arg is None:
            return self.redact
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
        """火花图标必须留着: 行定位与「有没有火花」的判定都靠它。

        但放行条件必须收紧 —— 旧版只看「URL 里含 flame|fire|streak|spark」, 普通
        头像或贴纸的 URL 只要撞上关键词就整块放行, 那等于没打码。现在要求三条同时
        成立(命中关键词 + 图标尺寸 + 不在用户主页链接内), 缺一条一律盖住。
        """
        from src.vision import _JS_REDACT

        self.assertIn("flame", _JS_REDACT)
        self.assertIn("keepFlame", _JS_REDACT)
        self.assertIn("iconSized", _JS_REDACT)
        self.assertIn("inUserLink", _JS_REDACT)
        # 失败的默认方向必须是「盖住」, 所以放行只能出现在 continue 之前那一个分支
        self.assertNotIn("if (flame.test(s)) continue;", _JS_REDACT)

    def test_scans_shadow_roots_for_every_kind(self):
        """shadow root 的遍历必须覆盖**所有**扫描, 不只是文本。

        旧版只有文本遍历走 roots, img / 表单与媒体元素 / CSS 背景三条仍是
        document.querySelectorAll —— shadow root 里的头像、canvas、背景图一个都
        没盖, 而计数照样漂亮。
        """
        from src.vision import _JS_REDACT

        qs = [l for l in _JS_REDACT.split("\n") if "querySelectorAll(" in l]
        for line in qs:
            self.assertNotIn(
                "document.querySelectorAll(",
                line,
                f"这条扫描没走 roots, shadow DOM 里的会漏: {line.strip()}",
            )

    def test_covers_svg_images(self):
        """SVG 的 <image>/<use> 一样能加载位图, 只扫 img 会整块漏掉。"""
        from src.vision import _JS_REDACT

        self.assertIn("svg image", _JS_REDACT)
        self.assertIn("svg use", _JS_REDACT)
        self.assertIn("xlink:href", _JS_REDACT)

    def test_covers_other_css_image_channels(self):
        """mask-image / border-image 同样是位图通道, 以前只扫 background-image。"""
        from src.vision import _JS_REDACT

        for prop in ("backgroundImage", "maskImage", "borderImageSource"):
            self.assertIn(prop, _JS_REDACT, f"脱敏脚本漏了 {prop}")

    def test_self_checks_coverage(self):
        """脚本要自证「纸上加了遮罩」和「屏幕上真盖住」是同一件事。

        position:fixed 的遮罩落在被 transform/filter 的祖先里时会相对那个祖先
        定位 —— 数组是对的、屏幕上是错的。所以脚本必须拿渲染后的遮罩矩形回查文本
        矩形, 并把 uncovered 报回来。
        """
        from src.vision import _JS_REDACT

        self.assertIn("uncovered", _JS_REDACT)
        self.assertIn("getBoundingClientRect()", _JS_REDACT)
        self.assertIn("maskRects", _JS_REDACT)

    def test_coverage_check_is_not_sampled(self):
        """旧版只复核前 400 个文本矩形, 超出的那批完全不验证。

        会话列表动辄几百个文本节点, 抽样等于给最该保守的一步开了后门。现在按
        2px 栅格逐点判定, 并用计算预算兜底 —— 预算耗尽报 unverified, 不是通过。
        """
        from src.vision import _JS_REDACT

        self.assertNotIn("checkN", _JS_REDACT)
        self.assertNotIn("< 400", _JS_REDACT)
        self.assertIn("unverified", _JS_REDACT)

    def test_masks_components_it_cannot_read(self):
        """闭合 shadow root 里的内容永远拿不到, 只能整块盖住宿主。

        `el.shadowRoot` 为 null 无法区分「没有 shadow DOM」与「闭合 shadow root」。
        对能确认内容不来自 light DOM 的自定义元素宿主, 打码是唯一安全答案。
        """
        from src.vision import _JS_REDACT

        self.assertIn("unreadable", _JS_REDACT)
        self.assertIn("indexOf('-')", _JS_REDACT)

    def test_walks_open_shadow_roots(self):
        """shadow DOM 里的文本 createTreeWalker(document.body) 走不进去。

        抖音的会话项里就有 shadow host —— 只从 body 起遍历, 那部分文本一个遮罩
        都不会生成, 而脚本返回的计数照样漂亮, 反而是种掩护。
        """
        from src.vision import _JS_REDACT

        self.assertIn("shadowRoot", _JS_REDACT)

    def test_covers_iframe(self):
        """跨源 iframe 的 DOM 读不到, 只能整块盖 —— 读不到就别猜它是空的。"""
        from src.vision import _JS_REDACT

        self.assertIn("iframe", _JS_REDACT)

    def test_covers_pseudo_element_content(self):
        """::before / ::after 的 content 是可见文字, 但不在文本节点里。"""
        from src.vision import _JS_REDACT

        self.assertIn("::before", _JS_REDACT)
        self.assertIn("::after", _JS_REDACT)


class TestRedactionGate(unittest.TestCase):
    """覆盖率闸门: "脚本没抛异常" 不等于 "遮住了东西", 更不等于 "盖全了"。

    这是脱敏链上最凶的一种失败 —— 页面还在骨架屏, 或者选择器改版后一个节点都
    没匹配上, 脚本老老实实返回 0, 旧代码照样 return True, 于是拍下一张完全没
    打码的整屏发给外部模型。没人会为此报错, 因为整条链看起来是成功的。

    第二道是自检口径的变化: 以前只问「遮了几处 > 0 吗」, 现在还要问「有没有文本块
    没被盖住」。一个文本节点就能满足前者, 满足不了后者。
    """

    @staticmethod
    def _ret(masked=3, text_nodes=3, uncovered=0, kept=1, unverified=0, unreadable=0):
        return {
            "masked": masked,
            "textNodes": text_nodes,
            "uncovered": uncovered,
            "unverified": unverified,
            "unreadable": unreadable,
            "keptFlame": kept,
        }

    def _page(self, ret):
        page = MagicMock()
        page.evaluate.return_value = ret
        return page

    def test_zero_hits_is_failure(self):
        from src import vision

        self.assertFalse(vision._inject_redaction(self._page(self._ret(masked=0))))

    def test_negative_hits_is_failure(self):
        from src import vision

        self.assertFalse(vision._inject_redaction(self._page(self._ret(masked=-1))))

    def test_uncovered_text_is_failure(self):
        """遮罩建了、但屏幕上没盖住某个文本块 —— 这就是外发明文, 必须放弃。"""
        from src import vision

        self.assertFalse(vision._inject_redaction(self._page(self._ret(uncovered=1))))

    def test_non_structured_result_is_failure(self):
        """老脚本返回裸数字; 新脚本返回对象。裸数字无法自证覆盖, 一律当失败。"""
        from src import vision

        for bad in (0, -1, 7, "12", None, "很多", [1, 2], True):
            self.assertFalse(
                vision._inject_redaction(self._page(bad)),
                f"{bad!r} 不是结构化结果, 不该被当成脱敏成功",
            )

    def test_positive_hits_pass(self):
        from src import vision

        self.assertTrue(vision._inject_redaction(self._page(self._ret(masked=7))))

    def test_numeric_string_counts_pass(self):
        """JS 侧偶尔会把数字带成字符串回来, 这不是失败, 别误杀。"""
        from src import vision

        ret = {"masked": "12", "textNodes": "12", "uncovered": "0", "keptFlame": "3"}
        self.assertTrue(vision._inject_redaction(self._page(ret)))

    def test_garbage_counts_are_failure(self):
        """结构对、数字烂 —— 同样不能当成功。"""
        from src import vision

        self.assertFalse(
            vision._inject_redaction(self._page({"masked": "很多", "uncovered": 0}))
        )

    def test_evaluate_exception_is_failure(self):
        from src import vision

        page = MagicMock()
        page.evaluate.side_effect = RuntimeError("page closed")
        self.assertFalse(vision._inject_redaction(page))

    def test_zero_hits_aborts_screenshot(self):
        """闸门要真的挡住截图: 命中 0 时不许留下任何 png。"""
        from src import vision

        page = MagicMock()
        page.evaluate.return_value = self._ret(masked=0)
        with (
            tempfile.TemporaryDirectory() as td,
            patch("src.vision._SHOT_DIR", Path(td)),
        ):
            out = vision._redacted_screenshot(page)
            self.assertEqual(out, "")
            self.assertEqual(list(Path(td).glob("*.png")), [])
        page.screenshot.assert_not_called()

    def test_uncovered_aborts_screenshot(self):
        """覆盖自检不过时同样不许落盘 —— 这一条守的是「看着加了其实没盖上」。"""
        from src import vision

        page = MagicMock()
        page.evaluate.return_value = self._ret(uncovered=2)
        with (
            tempfile.TemporaryDirectory() as td,
            patch("src.vision._SHOT_DIR", Path(td)),
        ):
            out = vision._redacted_screenshot(page)
            self.assertEqual(out, "")
            self.assertEqual(list(Path(td).glob("*.png")), [])
        page.screenshot.assert_not_called()

    def test_unverified_text_is_failure(self):
        """预算耗尽 = 没验证完, 不是通过。旧版把超出抽样范围的部分当盖住了。"""
        from src import vision

        self.assertFalse(
            vision._inject_redaction(self._page(self._ret(unverified=17)))
        )

    def test_unverified_aborts_screenshot(self):
        from src import vision

        page = MagicMock()
        page.evaluate.return_value = self._ret(unverified=5)
        with (
            tempfile.TemporaryDirectory() as td,
            patch("src.vision._SHOT_DIR", Path(td)),
        ):
            self.assertEqual(vision._redacted_screenshot(page), "")
            self.assertEqual(list(Path(td).glob("*.png")), [])
        page.screenshot.assert_not_called()

    def test_unreadable_components_do_not_block(self):
        """读不到的组件被整块盖住了, 那是处理过而不是没处理 —— 不该算失败。"""
        from src import vision

        self.assertTrue(
            vision._inject_redaction(self._page(self._ret(unreadable=4)))
        )


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
