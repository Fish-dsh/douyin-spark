"""src/debug.py: 失败现场截图必须落在 RUNTIME_DIR 下, 且轮转不能无界增长。

这两条都是 2026-11-20 修过的真事故: 硬编码 BASE_DIR/runtime 会让开发机上的
run_dev.sh 把截图写进生产目录, 而 _SCREENSHOT_KEEP 的轮转于是会删掉生产
失败现场 —— 那正是排查改版问题时唯一能看的东西。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import debug


class _FakePage:
    """只实现 save_screenshot 用到的那三个关键字参数。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def screenshot(self, **kwargs) -> None:
        self.calls.append(kwargs)
        Path(kwargs["path"]).write_bytes(b"png")

    def title(self) -> str:
        return "fake"

    @property
    def url(self) -> str:
        return "about:blank"

    def evaluate(self, _js: str) -> str:
        return "正文"


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        patcher = patch.object(debug, "SCREENSHOT_DIR", self.dir / "shots")
        patcher.start()
        self.addCleanup(patcher.stop)


class TestSaveScreenshot(_Case):
    def test_writes_into_screenshot_dir(self) -> None:
        path = debug.save_screenshot(_FakePage(), "tag")
        self.assertTrue(path)
        self.assertEqual(Path(path).parent, self.dir / "shots")
        self.assertTrue(path.endswith("-tag.png"))

    def test_returns_empty_string_when_page_fails(self) -> None:
        class Boom(_FakePage):
            def screenshot(self, **kwargs) -> None:
                raise RuntimeError("页面已关闭")

        self.assertEqual(debug.save_screenshot(Boom(), "tag"), "")

    def test_rotation_keeps_only_the_newest(self) -> None:
        shots = self.dir / "shots"
        shots.mkdir()
        for i in range(debug._SCREENSHOT_KEEP + 5):
            (shots / f"20260101-0000{i:02d}-old.png").write_bytes(b"x")
        debug.save_screenshot(_FakePage(), "new")
        kept = sorted(p.name for p in shots.glob("*.png"))
        self.assertEqual(len(kept), debug._SCREENSHOT_KEEP)
        # 轮转按文件名排序(时间戳前缀), 该留下的是最新那批
        self.assertNotIn("20260101-000000-old.png", kept)

    def test_rotation_survives_unlink_error(self) -> None:
        shots = self.dir / "shots"
        shots.mkdir()
        for i in range(debug._SCREENSHOT_KEEP + 2):
            (shots / f"20260101-0000{i:02d}-old.png").write_bytes(b"x")
        with patch.object(Path, "unlink", side_effect=OSError("被占用")):
            self.assertTrue(debug.save_screenshot(_FakePage(), "new"))


class TestDumpPage(_Case):
    def test_returns_screenshot_path(self) -> None:
        self.assertTrue(debug.dump_page(_FakePage()).endswith(".png"))

    def test_survives_page_that_cannot_be_read(self) -> None:
        class Dead(_FakePage):
            def title(self) -> str:
                raise RuntimeError("no title")

            def evaluate(self, _js: str) -> str:
                raise RuntimeError("no body")

        self.assertTrue(debug.dump_page(Dead()).endswith(".png"))


if __name__ == "__main__":
    unittest.main()
