"""把源码里的 JS 字符串常量交给 node --check 验语法。

动机(2026-11-02 板端真实事故): 生产日志一晚出现 6 次
    [发送] 策略A JS注入异常: Page.evaluate: SyntaxError: Invalid or unexpected token
真相是 _JS_INJECT_AND_SEND 的注释里写了字面量 "\\n续火花" —— Python 解析三引号
时把它变成真换行, JS 收到「注释里断行」直接语法错误。因为下面还有三层回落,
结果只是「慢一点」, 主链路坏了整整一晚没人发现。

JS 语法错误必须构建期就炸, 不能靠生产日志碰运气。没装 node 时跳过 —— 但跳过
不是通过, 所以这条用例的名字里带 skip 字样, 免得有人以为这里验过了。
"""

from __future__ import annotations

import unittest

from tools import check_js


class TestJsSyntax(unittest.TestCase):
    def test_at_least_some_js_is_found(self):
        """先确认扫描器真的抓到了东西 —— 抓到 0 段时「0 段有错」毫无意义。"""
        found = list(check_js.iter_js())
        self.assertGreater(len(found), 10, "没扫到 JS 常量, 扫描器本身坏了")
        names = {name for _, name, _ in found}
        self.assertIn("_JS_INJECT_AND_SEND", names)
        self.assertIn("_JS_FIND_STICKER", names)

    @unittest.skipUnless(check_js.node_available(), "没装 node, JS 语法检查跳过")
    def test_all_js_parses(self):
        failures = check_js.check_all()
        self.assertEqual(failures, [], "\n".join(failures))


if __name__ == "__main__":
    unittest.main()
