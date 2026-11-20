"""自检工具链的一致性: 检查逻辑只有一份, 入口只是转发。

2026-11-20 之前 tools/verify.sh 是 bash 写的 105 行真逻辑, Windows 上跑不了,
而仓库是"Windows 上开发、Linux 上跑" —— 于是开发机没有可用的自检入口。
改成 tools/verify.py 之后, 这三条断言就是防止再退回两份实现的护栏。
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class TestVerifyEntryPoints(unittest.TestCase):
    def test_shim_delegates_instead_of_reimplementing(self) -> None:
        shim = _read("tools/verify.sh")
        self.assertIn('exec "$PY" tools/verify.py "$@"', shim)
        # 旧实现里的检查步骤不能残留在壳里, 否则两份逻辑迟早漂移
        for leftover in ("compileall", "ruff", "pytest"):
            self.assertNotIn(leftover, shim, f"壳里不该再有 {leftover}")

    def test_python_impl_keeps_the_exit_code_contract(self) -> None:
        src = _read("tools/verify.py")
        self.assertIn("return 1", src)
        self.assertIn("return 2", src)
        # --fast 不计入 SKIP, 否则 pre-push 每次推送都被自己的闸门拦下
        self.assertIn("按 --fast 要求跳过", src)

    def test_makefile_lets_you_override_the_interpreter(self) -> None:
        make = _read("Makefile")
        self.assertIn("PY ?=", make)
        self.assertNotIn(".venv/bin/python -m", make)


class TestDocsPointAtTheSameEntry(unittest.TestCase):
    """三份文档都该指同一个入口 —— 之前它们分别写 unittest / bash / make。"""

    def test_development_doc(self) -> None:
        self.assertIn("tools/verify.py", _read("docs/development.md"))

    def test_agents_doc(self) -> None:
        self.assertIn("tools/verify.py", _read("AGENTS.md"))

    def test_readme_doc(self) -> None:
        self.assertIn("tools/verify.py", _read("README.md"))


class TestNoUnittestWordingLeft(unittest.TestCase):
    """docs 里再出现 unittest discover 会让人以为用例是 unittest 写的。"""

    def test_docs_do_not_tell_people_to_run_unittest(self) -> None:
        for rel in ("docs/development.md", "AGENTS.md", "README.md"):
            self.assertNotIn("unittest discover", _read(rel), rel)


if __name__ == "__main__":
    unittest.main()
