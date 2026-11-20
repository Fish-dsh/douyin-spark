"""src/stealth.py: 注入的是"特征伪装", 不是"绕过验证码" —— 这条边界要钉住。

模块本身没有分支逻辑, 能测的只有三件事: 脚本真的被注入、注入失败不抛、
以及脚本里那几条关键补丁还在(抖音风控读的就是它们)。
"""

from __future__ import annotations

import unittest

from src import stealth


class _FakeContext:
    def __init__(self) -> None:
        self.scripts: list[str] = []

    def add_init_script(self, script: str) -> None:
        self.scripts.append(script)


class TestApplyStealth(unittest.TestCase):
    def test_injects_the_patch_script(self) -> None:
        ctx = _FakeContext()
        stealth.apply_stealth(ctx)
        self.assertEqual(ctx.scripts, [stealth._STEALTH_JS])

    def test_failure_never_breaks_the_caller(self) -> None:
        # 伪装是"锦上添花": 注入失败也得让主链路继续跑, 否则一条 try 就废掉整晚。
        class Broken(_FakeContext):
            def add_init_script(self, script: str) -> None:
                raise RuntimeError("context 已关闭")

        stealth.apply_stealth(Broken())


class TestPatchContent(unittest.TestCase):
    """逐条钉住抖音会读的特征, 删掉任何一条都是行为回退。"""

    def test_webdriver_is_patched(self) -> None:
        self.assertIn("'webdriver'", stealth._STEALTH_JS)

    def test_plugins_and_languages_are_patched(self) -> None:
        self.assertIn("'plugins'", stealth._STEALTH_JS)
        self.assertIn("'languages'", stealth._STEALTH_JS)

    def test_webgl_vendor_and_renderer_are_patched(self) -> None:
        # 37445/37446 是 UNMASKED_VENDOR_WEBGL / UNMASKED_RENDERER_WEBGL 的常量值,
        # 板子上的 SwiftShader 就是从这里漏出去的。
        self.assertIn("37445", stealth._STEALTH_JS)
        self.assertIn("37446", stealth._STEALTH_JS)

    def test_chrome_object_is_patched(self) -> None:
        self.assertIn("window.chrome", stealth._STEALTH_JS)

    def test_each_patch_is_wrapped_in_try(self) -> None:
        # 单项失败不能连坐: 六段补丁各自 try/catch, 计数对齐就是护栏。
        self.assertEqual(stealth._STEALTH_JS.count("catch (e) {}"), 6)


if __name__ == "__main__":
    unittest.main()
