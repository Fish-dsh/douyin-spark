"""src/recovery.py 的判定表: 顺序即优先级, 顺序错了会把故障分到错的动作上。

这个模块是纯函数, 没有一行 IO, 所以这里全部是直接调用, 不需要桩。
真正的风险不在"能不能识别", 在**先识别到哪一个**: 一段 OOM 日志里往往同时
出现 "内存" 和 "未找到", 顺序一反, 资源压力就会被当成选择器失效 ——
前者该重启会话, 后者该 reload 重试, 处置完全相反。
"""

from __future__ import annotations

import unittest

from src import recovery as rc

# 恢复动作的全集。单独列一份而不是从模块里 import 集合, 是为了让
# 「模块新增了动作但忘了进策略表」这种改动在测试里显形。
_ACTIONS = frozenset({rc.RETRY, rc.RELOAD_RETRY, rc.RESTART_SESSION, rc.SKIP, rc.ALERT})


class TestClassifyBasic(unittest.TestCase):
    """六类各一例: 认得出来是最低要求。"""

    def test_render_hang(self) -> None:
        self.assertEqual(rc.classify("Error", "body 全空, 页面白屏"), rc.RENDER_HANG)

    def test_login(self) -> None:
        self.assertEqual(rc.classify("Error", "未登录, 需要重新扫码"), rc.LOGIN)

    def test_resource(self) -> None:
        self.assertEqual(
            rc.classify("MemoryError", "Cannot allocate memory"), rc.RESOURCE
        )

    def test_transient(self) -> None:
        self.assertEqual(
            rc.classify("TimeoutError", "page.goto: net::ERR_CONNECTION_RESET"),
            rc.TRANSIENT,
        )

    def test_selector(self) -> None:
        self.assertEqual(rc.classify("Error", "选择器失效, 疑似改版"), rc.SELECTOR)

    def test_unknown_falls_through(self) -> None:
        self.assertEqual(
            rc.classify("ValueError", "something nobody expected"), rc.UNKNOWN
        )

    def test_case_insensitive(self) -> None:
        self.assertEqual(rc.classify("", "OUT OF MEMORY"), rc.RESOURCE)


class TestRealLogShapes(unittest.TestCase):
    """真实日志里出现过的原文 —— 这些串是从 worklog 的事故条目里抄的,
    不是编的。改 _RULES 的标记词时先让它们继续绿。"""

    def test_render_hang_with_timeout_in_the_same_line(self) -> None:
        # 该串同时含 "超时"(transient) 与 "渲染挂死", 必须判为渲染挂死
        self.assertEqual(
            rc.classify("process_friend", "会话列表超时且页面 body 全空(渲染挂死)"),
            rc.RENDER_HANG,
        )

    def test_out_of_memory(self) -> None:
        self.assertEqual(rc.classify("process_friend", "Out of memory"), rc.RESOURCE)

    def test_discovery_all_strategies_missed(self) -> None:
        self.assertEqual(
            rc.classify(
                "discover", "火花检测四策略全部未命中, 疑似抖音页面改版导致选择器失效"
            ),
            rc.SELECTOR,
        )

    def test_coverage_gap(self) -> None:
        self.assertEqual(rc.classify("discover", "发现覆盖缺失: 甲, 乙"), rc.SELECTOR)

    def test_goto_timeout(self) -> None:
        self.assertEqual(
            rc.classify("process_friend", "Page.goto: Timeout 30000ms exceeded"),
            rc.TRANSIENT,
        )

    def test_target_closed(self) -> None:
        self.assertEqual(rc.classify("process_friend", "Target closed"), rc.TRANSIENT)

    def test_unconfirmed_send(self) -> None:
        # 静默失败(confirm_send 返回 fail 而非抛异常)必须值得重试
        self.assertEqual(
            rc.classify("process_friend", "发送未确认: 好友A"), rc.TRANSIENT
        )

    def test_streak_gap_is_unknown(self) -> None:
        # 漏发无法在本地恢复 → 未知类 → 告警, 不臆测重试
        self.assertEqual(
            rc.classify("streak_gap", "昨日 3 个待续好友未发送(账号 main): 甲"),
            rc.UNKNOWN,
        )


class TestRulePriority(unittest.TestCase):
    """_RULES 的顺序就是优先级 —— 这几条是顺序的护栏, 不是重复覆盖。"""

    def test_resource_beats_selector(self) -> None:
        # 真实日志形态: 内存打爆后 Playwright 报"元素未找到"。判成 SELECTOR
        # 会去 reload 重试, 而内存没释放, 重试只会再挂一次。
        self.assertEqual(rc.classify("Error", "内存不足: 元素未找到"), rc.RESOURCE)

    def test_render_hang_beats_selector(self) -> None:
        self.assertEqual(
            rc.classify("Error", "body 全空, 表情按钮未找到"), rc.RENDER_HANG
        )

    def test_login_beats_transient(self) -> None:
        # 登录过期时页面也会 timeout, 但重试没有意义, 只能喊人。
        self.assertEqual(rc.classify("TimeoutError", "登录态失效 timeout"), rc.LOGIN)

    def test_transient_beats_selector(self) -> None:
        # 等元素超时是最常见的一类, 它本质是瞬时问题, 不是改版。
        self.assertEqual(rc.classify("TimeoutError", "等待表情按钮超时"), rc.TRANSIENT)

    def test_type_and_message_are_joined(self) -> None:
        # 判据同时看类型与摘要, 只给一边也要能认。
        self.assertEqual(rc.classify("TimeoutError", ""), rc.TRANSIENT)
        self.assertEqual(rc.classify("", "发送未确认"), rc.TRANSIENT)


class TestPolicyTable(unittest.TestCase):
    def test_every_category_has_an_action(self) -> None:
        for category in (
            rc.TRANSIENT,
            rc.SELECTOR,
            rc.RENDER_HANG,
            rc.RESOURCE,
            rc.LOGIN,
            rc.UNKNOWN,
        ):
            self.assertIn(rc.policy(category), _ACTIONS, category)

    def test_login_and_unknown_alert_instead_of_retry(self) -> None:
        # 这两类重试只会浪费时间: 一个要人扫码, 一个连是什么都不知道。
        self.assertEqual(rc.policy(rc.LOGIN), rc.ALERT)
        self.assertEqual(rc.policy(rc.UNKNOWN), rc.ALERT)

    def test_unrecognized_category_alerts(self) -> None:
        self.assertEqual(rc.policy("no_such_category"), rc.ALERT)


class TestShouldRetry(unittest.TestCase):
    def test_retry_actions(self) -> None:
        for action in (rc.RETRY, rc.RELOAD_RETRY, rc.RESTART_SESSION):
            self.assertTrue(rc.should_retry(action), action)

    def test_terminal_actions(self) -> None:
        for action in (rc.SKIP, rc.ALERT, "no_such_action"):
            self.assertFalse(rc.should_retry(action), action)


class TestRuleTableShape(unittest.TestCase):
    """改表时的静态约束: 标记必须小写, 类别必须在已知集合里。"""

    def test_markers_are_lowercase(self) -> None:
        # classify() 把文本转小写后做子串匹配, 表里留大写就永远匹配不上。
        for category, markers in rc._RULES:
            for marker in markers:
                self.assertEqual(marker, marker.lower(), f"{category}: {marker}")

    def test_categories_are_known(self) -> None:
        known = {rc.TRANSIENT, rc.SELECTOR, rc.RENDER_HANG, rc.RESOURCE, rc.LOGIN}
        for category, _markers in rc._RULES:
            self.assertIn(category, known)

    def test_no_duplicate_categories(self) -> None:
        # 同一类别写两条规则, 后一条永远命中不到 —— 那是笔误不是设计。
        cats = [category for category, _m in rc._RULES]
        self.assertEqual(len(cats), len(set(cats)))


if __name__ == "__main__":
    unittest.main()
