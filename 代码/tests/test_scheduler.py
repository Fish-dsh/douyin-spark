"""scheduler.py 单测：两阶段调度、抖动、窗口校验、退出码语义。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import ANY, MagicMock, call, patch

from src import scheduler as _scheduler_mod
from src.config import in_window
from src.discovery import RenderHangDetected
from src.scheduler import (
    _HEALABLE_ERRORS,
    _acquire_pid_lock,
    _heal_after_send_failure,
    _merge_rc,
    _send_phase,
    process_account,
    process_friend,
    run,
)
from src.session import LoginRequiredError


class TestInWindow(unittest.TestCase):
    """复用 config.py 的测试，这里只测关键边界"""

    def test_cross_midnight_window(self):
        # 23:30 - 00:30 跨午夜
        now = datetime(2026, 8, 14, 0, 15)
        self.assertTrue(in_window(now, "23:30", "00:30"))

        now = datetime(2026, 8, 13, 22, 00)
        self.assertFalse(in_window(now, "23:30", "00:30"))

        now = datetime(2026, 8, 14, 1, 00)
        self.assertFalse(in_window(now, "23:30", "00:30"))

    def test_same_day_window(self):
        now = datetime(2026, 8, 13, 12, 00)
        self.assertTrue(in_window(now, "10:00", "14:00"))

        now = datetime(2026, 8, 13, 9, 00)
        self.assertFalse(in_window(now, "10:00", "14:00"))

        now = datetime(2026, 8, 13, 15, 00)
        self.assertFalse(in_window(now, "10:00", "14:00"))


class TestProcessFriend(unittest.TestCase):
    def setUp(self):
        self.page = MagicMock()
        self.cfg = {"block_resources": True}

    @patch("src.scheduler.wait_chat_ui")
    @patch("src.scheduler.wait_chat_list")
    @patch("src.scheduler.click_friend")
    @patch("src.scheduler.send_sticker")
    @patch("src.scheduler.confirm_send")
    def test_process_friend_success_sticker(
        self, mock_confirm, mock_sticker, mock_click, mock_wait_list, mock_wait_ui
    ):
        mock_wait_ui.return_value = True
        mock_click.return_value = MagicMock()
        mock_sticker.return_value = (True, "表情已发送")
        mock_confirm.return_value = (True, "确认成功")

        with patch("src.login.is_logged_in", return_value=True):
            status, sent = process_friend(self.page, self.cfg, "测试好友", False)

        self.assertEqual(status, "ok")
        self.assertTrue(sent)

    @patch("src.scheduler.wait_chat_ui")
    @patch("src.scheduler.wait_chat_list")
    @patch("src.scheduler.click_friend")
    @patch("src.scheduler.send_sticker")
    @patch("src.scheduler.confirm_send")
    def test_process_friend_waits_for_input_box_before_send(
        self, mock_confirm, mock_sticker, mock_click, mock_wait_list, mock_wait_ui
    ):
        """2026-10-06 好友A事故回归: 点击后必须先等消息输入框挂载再发送。"""
        mock_wait_ui.return_value = True
        mock_click.return_value = MagicMock()
        mock_sticker.return_value = (True, "表情已发送")
        mock_confirm.return_value = (True, "确认成功")

        with patch("src.login.is_logged_in", return_value=True):
            process_friend(self.page, self.cfg, "测试好友", False)

        self.page.wait_for_selector.assert_called_once()
        args, kwargs = self.page.wait_for_selector.call_args
        self.assertEqual(args[0], '[contenteditable="true"]')
        self.assertEqual(kwargs.get("state"), "visible")

    @patch("src.scheduler.wait_chat_ui")
    @patch("src.scheduler.wait_chat_list")
    @patch("src.scheduler.click_friend")
    @patch("src.scheduler.send_sticker")
    @patch("src.scheduler.confirm_send")
    @patch("src.scheduler.synthetic_click_row")
    def test_process_friend_panel_never_mounts_fails_fast(
        self,
        mock_synth,
        mock_confirm,
        mock_sticker,
        mock_click,
        mock_wait_list,
        mock_wait_ui,
    ):
        """取证 v1-v6: 重燃会话可信点击打不开面板。首等超时 → 合成点击抢救 →
        再超时 → 快速失败, 绝不盲发(盲发必败且留假发送风险)。"""
        mock_wait_ui.return_value = True
        mock_click.return_value = MagicMock()
        mock_synth.return_value = {"x": 185, "y": 562}
        self.page.wait_for_selector.side_effect = TimeoutError("boom")

        with patch("src.login.is_logged_in", return_value=True):
            status, sent = process_friend(self.page, self.cfg, "测试好友", False)

        self.assertEqual(status, "fail")
        self.assertFalse(sent)
        mock_synth.assert_called_once()
        mock_sticker.assert_not_called()
        mock_confirm.assert_not_called()
        # 首等 + 合成点击后二等
        self.assertEqual(self.page.wait_for_selector.call_count, 2)

    @patch("src.scheduler.wait_chat_ui")
    @patch("src.scheduler.wait_chat_list")
    @patch("src.scheduler.click_friend")
    @patch("src.scheduler.send_sticker")
    @patch("src.scheduler.verify_chat_opened_ready")
    @patch("src.scheduler.synthetic_click_row")
    def test_process_friend_synthetic_rescue_recovers_panel(
        self,
        mock_synth,
        mock_verify,
        mock_sticker,
        mock_click,
        mock_wait_list,
        mock_wait_ui,
    ):
        """合成点击抢救成功(二等通过) → 正常走发送链路。"""
        mock_wait_ui.return_value = True
        mock_click.return_value = MagicMock()
        mock_synth.return_value = {"x": 1, "y": 2}
        mock_verify.return_value = (True, "ok")
        self.page.wait_for_selector.side_effect = [TimeoutError("boom"), None]
        mock_sticker.return_value = (True, "表情已发送")

        with patch("src.login.is_logged_in", return_value=True):
            status, sent = process_friend(self.page, self.cfg, "测试好友", False)

        self.assertEqual(status, "ok")
        self.assertTrue(sent)
        mock_sticker.assert_called_once()

    @patch("src.scheduler.wait_chat_ui")
    @patch("src.scheduler.wait_chat_list")
    @patch("src.scheduler.click_friend")
    @patch("src.scheduler.verify_chat_opened_ready")
    @patch("src.scheduler.send_sticker")
    def test_process_friend_identity_mismatch_blocks_send(
        self,
        mock_sticker,
        mock_verify,
        mock_click,
        mock_wait_list,
        mock_wait_ui,
    ):
        """身份核对冲突时必须拦截, 不进发送链路(假发送闸门)。"""
        mock_wait_ui.return_value = True
        mock_click.return_value = MagicMock()
        mock_verify.return_value = (False, "身份核对冲突: 右侧面板未出现目标")

        with patch("src.login.is_logged_in", return_value=True):
            status, sent = process_friend(self.page, self.cfg, "测试好友", False)

        self.assertEqual(status, "fail")
        self.assertFalse(sent)
        mock_sticker.assert_not_called()

    @patch("src.scheduler.wait_chat_ui")
    @patch("src.scheduler.wait_chat_list")
    @patch("src.scheduler.click_friend")
    @patch("src.scheduler.send_sticker")
    @patch("src.scheduler.send_message")
    @patch("src.scheduler.confirm_send")
    def test_process_friend_fallback_to_text(
        self,
        mock_confirm,
        mock_msg,
        mock_sticker,
        mock_click,
        mock_wait_list,
        mock_wait_ui,
    ):
        mock_wait_ui.return_value = True
        mock_click.return_value = MagicMock()
        mock_sticker.return_value = (False, "表情失败")
        mock_msg.return_value = (True, "策略A 生效")
        mock_confirm.return_value = (True, "确认成功")

        with patch("src.login.is_logged_in", return_value=True):
            status, sent = process_friend(self.page, self.cfg, "测试好友", False)

        self.assertEqual(status, "ok")
        self.assertTrue(sent)
        mock_msg.assert_called_once()

    @patch("src.scheduler.wait_chat_ui")
    @patch("src.scheduler.wait_chat_list")
    @patch("src.scheduler.click_friend")
    def test_process_friend_click_fails(self, mock_click, mock_wait_list, mock_wait_ui):
        mock_wait_ui.return_value = True
        mock_click.return_value = None

        with patch("src.login.is_logged_in", return_value=True):
            status, sent = process_friend(self.page, self.cfg, "测试好友", False)

        self.assertEqual(status, "fail")
        self.assertFalse(sent)

    @patch("src.scheduler.wait_chat_ui")
    @patch("src.scheduler.wait_chat_list")
    def test_process_friend_logged_out(self, mock_wait_list, mock_wait_ui):
        mock_wait_ui.return_value = True

        with patch("src.login.is_logged_in", return_value=False):
            status, sent = process_friend(self.page, self.cfg, "测试好友", False)

        self.assertEqual(status, "logged_out")
        self.assertFalse(sent)

    def test_process_friend_dry_run(self):
        with (
            patch("src.scheduler.wait_chat_ui", return_value=True),
            patch("src.scheduler.wait_chat_list"),
            patch("src.scheduler.click_friend", return_value=MagicMock()),
            patch("src.login.is_logged_in", return_value=True),
        ):
            status, sent = process_friend(self.page, self.cfg, "测试好友", True)

        self.assertEqual(status, "ok")
        self.assertFalse(sent)

    @patch("src.scheduler.wait_chat_ui", return_value=True)
    @patch("src.scheduler.wait_chat_list", return_value=False)
    def test_process_friend_raises_on_render_hang(self, mock_wait_list, mock_wait_ui):
        """列表超时且 body 全空 → 抛 RenderHangDetected, 绝不在残缺页面上继续点击。"""
        with (
            patch("src.login.is_logged_in", return_value=True),
            patch("src.scheduler.page_render_state", return_value="blank"),
            patch("src.scheduler.click_friend") as mock_click,
            self.assertRaises(RenderHangDetected),
        ):
            process_friend(self.page, self.cfg, "测试好友", False)

        mock_click.assert_not_called()


class TestHealableErrors(unittest.TestCase):
    def test_healable_errors_tuple(self):
        self.assertIn(TimeoutError, _HEALABLE_ERRORS)
        self.assertIn(OSError, _HEALABLE_ERRORS)
        self.assertIn(ConnectionError, _HEALABLE_ERRORS)
        self.assertIn(BrokenPipeError, _HEALABLE_ERRORS)


def _guard_real_heal_side_effects(test: unittest.TestCase) -> None:
    """打桩 process_account 收尾三条真实链路: streak_gap 触发、P5 覆盖门禁、历史写盘。

    未打桩时测试会读写真实 runtime/, 覆盖缺失分支还会走真实 self_heal
    并在本仓库打出 heal 基线脏提交(2026-09-30 / 2026-10-03 两次实测)。
    """
    for target, kwargs in (
        (
            "src.scheduler.streak_history.trigger_if_gap_detected",
            {"return_value": False},
        ),
        ("src.scheduler.streak_history.check_recent_coverage", {"return_value": []}),
        ("src.scheduler.streak_history.save_daily_record", {}),
    ):
        patcher = patch(target, **kwargs)
        patcher.start()
        test.addCleanup(patcher.stop)


class TestRunArgumentParsing(unittest.TestCase):
    def setUp(self):
        # 防真实自愈: 用例会走到 process_account 的 streak_gap/覆盖门禁/历史写盘,
        # 未打桩时会读写真实 runtime 并打出脏提交(2026-09-30 与 2026-10-03 两次实测)
        _guard_real_heal_side_effects(self)

    @patch("src.scheduler.sync_playwright")
    def test_force_flag(self, mock_playwright):
        p = MagicMock()
        mock_playwright.return_value.__enter__.return_value = p

        cfg = {
            "send_window": {"start": "23:30", "end": "00:30"},
            "accounts": [{"name": "main", "account": "a", "password": "b"}],
        }

        with (
            patch("sys.argv", ["spark.py", "--force"]),
            patch("src.scheduler.load_config", return_value=cfg),
            patch(
                "src.scheduler.load_state",
                return_value={"date": "2026-08-13", "sent": []},
            ),
            patch("src.scheduler.write_state"),
            patch("src.scheduler.open_session") as mock_open,
            patch("src.scheduler.wait_chat_list", return_value=True),
            patch("src.scheduler.discover_pending_friends", return_value=[]),
            patch("src.scheduler.close_session"),
            patch("src.scheduler.memory_report"),
        ):
            mock_open.return_value = (MagicMock(), MagicMock(), MagicMock())

            try:
                run()
            except SystemExit as e:
                self.assertEqual(e.code, 0)


class TestRunBrowserReuse(unittest.TestCase):
    """browser_reuse=True 时共享单个浏览器, 仅关 context, 结束后关 browser。"""

    def setUp(self):
        self.p = MagicMock()
        self.patcher_playwright = patch("src.scheduler.sync_playwright")
        self.mock_playwright = self.patcher_playwright.start()
        self.mock_playwright.return_value.__enter__.return_value = self.p
        self.cfg = {
            "send_window": {"start": "23:30", "end": "00:30"},
            "browser_reuse": True,
            "accounts": [{"name": "main", "account": "a", "password": "b"}],
        }
        self.addCleanup(self.patcher_playwright.stop)
        patcher_wait_list = patch("src.scheduler.wait_chat_list", return_value=True)
        patcher_wait_list.start()
        self.addCleanup(patcher_wait_list.stop)
        # 防真实自愈: 用例会走到 process_account 的 streak_gap/覆盖门禁/历史写盘,
        # 未打桩时会读写真实 runtime 并打出脏提交(2026-09-30 与 2026-10-03 两次实测)
        _guard_real_heal_side_effects(self)

    @patch("src.scheduler.memory_report")
    @patch("src.scheduler.save_session_state")
    @patch("src.scheduler.process_friend", return_value=("ok", True))
    def test_reuses_shared_browser(self, mock_process, mock_save, mock_mem):
        with patch("src.scheduler.close_session") as mock_close:
            shared_browser_obj = MagicMock()

            def fake_open(p, cfg, account, browser=None):
                if browser is None:
                    return shared_browser_obj, MagicMock(), MagicMock()
                return browser, MagicMock(), MagicMock()

            with (
                patch("sys.argv", ["spark.py", "--force"]),
                patch("src.scheduler.load_config", return_value=self.cfg),
                patch("src.scheduler.available_mb", return_value=2048),
                patch(
                    "src.scheduler.load_state",
                    return_value={"date": "2026-08-13", "sent": []},
                ),
                patch("src.scheduler.write_state"),
                patch("src.scheduler.open_session", side_effect=fake_open) as mock_open,
                patch(
                    "src.scheduler.discover_pending_friends", return_value=["A", "B"]
                ),
            ):
                run()

            # 发现阶段1次 + 每好友1次 = 3 次
            self.assertEqual(mock_open.call_count, 3)
            # 好友A: browser=None(首次启动, 之后成为共享) ; 好友B: 复用共享 browser
            self.assertIsNone(mock_open.call_args_list[1].kwargs.get("browser"))
            self.assertEqual(
                mock_open.call_args_list[2].kwargs.get("browser"), shared_browser_obj
            )

            # 共4次 close: 发现阶段1次 + 每好友1次(仅关context) + 最后关共享browser
            self.assertEqual(mock_close.call_count, 4)
            # 每好友(调用1,2)仅关 context(None, fctx), 不关共享 browser
            for call in mock_close.call_args_list[1:3]:
                self.assertIsNone(call.args[0])
                self.assertIsNotNone(call.args[1])
            # 最后(调用3)关闭共享 browser
            final_call = mock_close.call_args_list[-1]
            self.assertEqual(final_call.args[0], shared_browser_obj)
            self.assertIsNone(final_call.args[1])

    @patch("src.scheduler.memory_report")
    @patch("src.scheduler.save_session_state")
    @patch("src.scheduler.process_friend", return_value=("ok", True))
    def test_recycles_shared_browser_when_memory_low(
        self, mock_process, mock_save, mock_mem
    ):
        """剩余内存低于阈值: 回收共享实例, 下个好友冷启动; 内存够则一直复用。"""
        created = []

        def fake_open(p, cfg, account, browser=None):
            if browser is None:
                nb = MagicMock()
                created.append(nb)
                return nb, MagicMock(), MagicMock()
            return browser, MagicMock(), MagicMock()

        with (
            patch("src.scheduler.close_session") as mock_close,
            patch("sys.argv", ["spark.py", "--force"]),
            patch("src.scheduler.load_config", return_value=self.cfg),
            patch("src.scheduler.available_mb", return_value=120),
            patch(
                "src.scheduler.load_state",
                return_value={"date": "2026-08-13", "sent": []},
            ),
            patch("src.scheduler.write_state"),
            patch("src.scheduler.open_session", side_effect=fake_open) as mock_open,
            patch("src.scheduler.discover_pending_friends", return_value=["A", "B"]),
        ):
            run()

        # 发现1 + 好友A冷启动1 + 回收后好友B冷启动1 = 3 次全新启动
        self.assertEqual(len(created), 3)
        # 好友B 不复用已回收的实例(否则就是"内存不够仍硬撑")
        self.assertIsNone(mock_open.call_args_list[2].kwargs.get("browser"))
        # 好友A 用的实例确实被关掉(browser, None), 不是仅关 context
        self.assertIn(call(created[1], None), mock_close.call_args_list)

    @patch("src.scheduler.memory_report")
    @patch("src.scheduler.save_session_state")
    def test_render_hang_retries_without_ai_heal(self, mock_save, mock_mem):
        """发送阶段渲染挂死: 重开会话重试一次, 且不触发 AI 自愈(与发现阶段同口径)。"""
        created = []

        def fake_open(p, cfg, account, browser=None):
            if browser is None:
                nb = MagicMock()
                created.append(nb)
                return nb, MagicMock(), MagicMock()
            return browser, MagicMock(), MagicMock()

        with (
            patch("src.scheduler.close_session"),
            patch("src.scheduler.self_heal") as mock_heal,
            patch("src.scheduler.free_page_memory"),
            patch(
                "src.scheduler.process_friend",
                side_effect=[RenderHangDetected("body 全空"), ("ok", True)],
            ),
            patch("sys.argv", ["spark.py", "--force"]),
            patch("src.scheduler.load_config", return_value=self.cfg),
            patch("src.scheduler.available_mb", return_value=2048),
            patch(
                "src.scheduler.load_state",
                return_value={"date": "2026-08-13", "sent": []},
            ),
            patch("src.scheduler.write_state"),
            patch("src.scheduler.open_session", side_effect=fake_open) as mock_open,
            patch("src.scheduler.discover_pending_friends", return_value=["A"]),
        ):
            run()

        mock_heal.assert_not_called()
        # 发现1 + 好友A首启1 + 挂死重试重建1 = 3 次全新启动
        self.assertEqual(len(created), 3)
        # 重试必须重开会话, 不能复用挂死的共享实例
        self.assertIsNone(mock_open.call_args_list[2].kwargs.get("browser"))

    @patch("src.scheduler.memory_report")
    @patch("src.scheduler.save_session_state")
    def test_critical_fail_discards_shared_browser(self, mock_save, mock_mem):
        """W1: 关键故障且 AI 自愈失败后, 故障共享浏览器必须被丢弃, 下个好友全新重建。"""
        created = []

        def fake_open(p, cfg, account, browser=None):
            if browser is None:
                nb = MagicMock()
                created.append(nb)
                return nb, MagicMock(), MagicMock()
            return browser, MagicMock(), MagicMock()

        def process_raise(page, account, name, dry_run):
            raise OSError("Target closed")

        with (
            patch("src.scheduler.close_session"),
            patch("src.scheduler.self_heal", return_value=False),
            patch("src.scheduler.dump_page", return_value=""),
            patch("src.scheduler.free_page_memory"),
            patch("src.scheduler.process_friend", side_effect=process_raise),
            patch("sys.argv", ["spark.py", "--force"]),
            patch("src.scheduler.load_config", return_value=self.cfg),
            patch(
                "src.scheduler.load_state",
                return_value={"date": "2026-08-13", "sent": []},
            ),
            patch("src.scheduler.write_state"),
            patch("src.scheduler.open_session", side_effect=fake_open) as mock_open,
            patch("src.scheduler.discover_pending_friends", return_value=["A", "B"]),
        ):
            run()

        # 7 次启动: 发现1 + A首启/重建/自愈重建(3) + B首启/重建(2) + 补扫A/B(2)
        # 核心 W1 不变式: A/B 关键失败后均走全新浏览器, 不复用故障浏览器
        self.assertEqual(len(created), 7)
        # A 与 B 首启均全新(browser=None → 新建); 好友B(发送循环最后一次)不复用故障浏览器
        self.assertIsNone(mock_open.call_args_list[4].kwargs.get("browser"))


class TestRunVerifySend(unittest.TestCase):
    """--verify-send 门禁: 对全部待续好友逐一真实发送并写入 state(防重发)。"""

    def setUp(self):
        self.p = MagicMock()
        self.patcher_playwright = patch("src.scheduler.sync_playwright")
        self.mock_playwright = self.patcher_playwright.start()
        self.mock_playwright.return_value.__enter__.return_value = self.p
        self.cfg = {
            "send_window": {"start": "23:30", "end": "00:30"},
            "accounts": [{"name": "main", "account": "a", "password": "b"}],
        }
        self.addCleanup(self.patcher_playwright.stop)
        patcher_wait_list = patch("src.scheduler.wait_chat_list", return_value=True)
        patcher_wait_list.start()
        self.addCleanup(patcher_wait_list.stop)
        # 防真实自愈: 用例会走到 process_account 的 streak_gap/覆盖门禁/历史写盘,
        # 未打桩时会读写真实 runtime 并打出脏提交(2026-09-30 与 2026-10-03 两次实测)
        _guard_real_heal_side_effects(self)

    @patch("src.scheduler.in_window", return_value=True)
    @patch("src.scheduler.memory_report")
    @patch("src.scheduler.save_session_state")
    @patch("src.scheduler.process_friend", return_value=("ok", True))
    def test_verify_send_sends_all_pending(
        self, mock_process, mock_save, mock_mem, mock_win
    ):
        written = {}

        def fake_write(name, state):
            written.clear()
            written.update(state)

        with (
            patch("src.scheduler.close_session"),
            patch("sys.argv", ["spark.py", "--verify-send", "--no-jitter"]),
            patch("src.scheduler.load_config", return_value=self.cfg),
            patch(
                "src.scheduler.load_state",
                return_value={"date": "2026-08-13", "sent": []},
            ),
            patch("src.scheduler.write_state", side_effect=fake_write),
            patch(
                "src.scheduler.open_session",
                return_value=(MagicMock(), MagicMock(), MagicMock()),
            ),
            patch("src.scheduler.discover_pending_friends", return_value=["A", "B"]),
            self.assertRaises(SystemExit) as cm,
        ):
            run()
        self.assertEqual(cm.exception.code, 0)
        # 全部待续好友逐一真实发送并全部写入 state(含置顶强制续好友)
        self.assertEqual(mock_process.call_count, 2)
        mock_process.assert_has_calls(
            [call(ANY, ANY, "A", dry_run=False), call(ANY, ANY, "B", dry_run=False)]
        )
        self.assertEqual(written.get("sent"), ["A", "B"])

    @patch("src.scheduler.in_window", return_value=True)
    @patch("src.scheduler.memory_report")
    @patch("src.scheduler.save_session_state")
    @patch("src.scheduler.process_friend", return_value=("fail", False))
    def test_verify_send_fail_exits_1(
        self, mock_process, mock_save, mock_mem, mock_win
    ):
        with (
            patch("src.scheduler.close_session"),
            patch("sys.argv", ["spark.py", "--verify-send", "--no-jitter"]),
            patch("src.scheduler.load_config", return_value=self.cfg),
            patch(
                "src.scheduler.load_state",
                return_value={"date": "2026-08-13", "sent": []},
            ),
            patch("src.scheduler.write_state"),
            patch(
                "src.scheduler.open_session",
                return_value=(MagicMock(), MagicMock(), MagicMock()),
            ),
            patch("src.scheduler.discover_pending_friends", return_value=["A"]),
            self.assertRaises(SystemExit) as cm,
        ):
            run()
        self.assertEqual(cm.exception.code, 1)

    @patch("src.scheduler.in_window", return_value=True)
    @patch("src.scheduler.memory_report")
    @patch("src.scheduler.save_session_state")
    @patch("src.scheduler.process_friend", side_effect=[("ok", True), ("fail", False)])
    def test_verify_send_second_fail_exits_1(
        self, mock_process, mock_save, mock_mem, mock_win
    ):
        """任一待续好友发送失败, 门禁整体失败; 已成功的写入 state 防重发。"""
        written = {}

        def fake_write(name, state):
            written.clear()
            written.update(state)

        with (
            patch("src.scheduler.close_session"),
            patch("sys.argv", ["spark.py", "--verify-send", "--no-jitter"]),
            patch("src.scheduler.load_config", return_value=self.cfg),
            patch(
                "src.scheduler.load_state",
                return_value={"date": "2026-08-13", "sent": []},
            ),
            patch("src.scheduler.write_state", side_effect=fake_write),
            patch(
                "src.scheduler.open_session",
                return_value=(MagicMock(), MagicMock(), MagicMock()),
            ),
            patch("src.scheduler.discover_pending_friends", return_value=["A", "B"]),
            self.assertRaises(SystemExit) as cm,
        ):
            run()
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual(written.get("sent"), ["A"])

    @patch("src.scheduler.in_window", return_value=True)
    @patch("src.scheduler.memory_report")
    @patch("src.scheduler.save_session_state")
    def test_heal_gate_sent_skips_resend(self, mock_save, mock_mem, mock_win):
        """自愈后重试: process_friend 首次抛异常, self_heal 修复后重试成功。"""
        calls = []

        def process_raise(page, account, name, dry_run):
            calls.append(name)
            if len(calls) == 1:
                raise OSError("Target closed")
            return ("ok", True)

        today = datetime.now().strftime("%Y-%m-%d")

        with (
            patch("src.scheduler.close_session"),
            patch("src.scheduler.self_heal", return_value=True),
            patch("src.scheduler.dump_page", return_value=""),
            patch("src.scheduler.free_page_memory"),
            patch("src.scheduler.process_friend", side_effect=process_raise),
            patch("sys.argv", ["spark.py", "--force"]),
            patch("src.scheduler.load_config", return_value=self.cfg),
            patch(
                "src.scheduler.load_state",
                return_value={"date": today, "sent": []},
            ),
            patch("src.scheduler.write_state"),
            patch(
                "src.scheduler.open_session",
                return_value=(MagicMock(), MagicMock(), MagicMock()),
            ),
            patch("src.scheduler.discover_pending_friends", return_value=["A"]),
        ):
            run()

        # 首次失败 + self_heal 修复后重试成功 = 2 次调用
        self.assertEqual(calls, ["A", "A"])

    @patch("src.scheduler.in_window", return_value=True)
    @patch("src.scheduler.memory_report")
    @patch("src.scheduler.save_session_state")
    @patch("src.scheduler.process_friend", return_value=("ok", True))
    def test_loop_top_skips_already_sent_in_state(
        self, mock_process, mock_save, mock_mem, mock_win
    ):
        """顺序漂移: 门禁已发送好友A(state 含 A)但 pending 仍含 A → 循环顶部跳过, 只处理 B。"""
        calls = []

        def process(page, account, name, dry_run):
            calls.append(name)
            return "ok", True

        today = datetime.now().strftime("%Y-%m-%d")

        with (
            patch("src.scheduler.close_session"),
            patch("src.scheduler.self_heal", return_value=False),
            patch("src.scheduler.free_page_memory"),
            patch("src.scheduler.process_friend", side_effect=process),
            patch("sys.argv", ["spark.py", "--force"]),
            patch("src.scheduler.load_config", return_value=self.cfg),
            patch(
                "src.scheduler.load_state",
                return_value={"date": today, "sent": ["A"]},
            ),
            patch("src.scheduler.write_state"),
            patch(
                "src.scheduler.open_session",
                return_value=(MagicMock(), MagicMock(), MagicMock()),
            ),
            patch("src.scheduler.discover_pending_friends", return_value=["A", "B"]),
        ):
            run()

        self.assertEqual(calls, ["B"], "A 已在 state 中应跳过, 不重复发送")


class TestRunDefaultBrowser(unittest.TestCase):
    """reuse=False 路径: 每好友独立浏览器, 每次都全新启动。"""

    def setUp(self):
        self.p = MagicMock()
        self.patcher_playwright = patch("src.scheduler.sync_playwright")
        self.mock_playwright = self.patcher_playwright.start()
        self.mock_playwright.return_value.__enter__.return_value = self.p
        self.cfg = {
            "send_window": {"start": "23:30", "end": "00:30"},
            "browser_reuse": False,
            "accounts": [{"name": "main", "account": "a", "password": "b"}],
        }
        self.addCleanup(self.patcher_playwright.stop)
        patcher_wait_list = patch("src.scheduler.wait_chat_list", return_value=True)
        patcher_wait_list.start()
        self.addCleanup(patcher_wait_list.stop)
        # 防真实自愈: 用例会走到 process_account 的 streak_gap/覆盖门禁/历史写盘,
        # 未打桩时会读写真实 runtime 并打出脏提交(2026-09-30 与 2026-10-03 两次实测)
        _guard_real_heal_side_effects(self)

    @patch("src.scheduler.memory_report")
    @patch("src.scheduler.save_session_state")
    @patch("src.scheduler.process_friend", return_value=("ok", True))
    def test_default_mode_launches_fresh_browser_per_friend(
        self, mock_process, mock_save, mock_mem
    ):
        with patch("src.scheduler.close_session"):

            def fake_open(p, cfg, account, browser=None):
                self.assertIsNone(browser, "默认模式不得复用浏览器")
                return MagicMock(), MagicMock(), MagicMock()

            with (
                patch("sys.argv", ["spark.py", "--force"]),
                patch("src.scheduler.load_config", return_value=self.cfg),
                patch(
                    "src.scheduler.load_state",
                    return_value={"date": "2026-08-13", "sent": []},
                ),
                patch("src.scheduler.write_state"),
                patch("src.scheduler.open_session", side_effect=fake_open) as mock_open,
                patch(
                    "src.scheduler.discover_pending_friends", return_value=["A", "B"]
                ),
            ):
                run()

            # 发现阶段 + 每好友各一次 = 3 次
            self.assertEqual(mock_open.call_count, 3)
            for call in mock_open.call_args_list:
                self.assertIsNone(call.kwargs.get("browser"))


class TestHasLoginState(unittest.TestCase):
    """调度判据: 有有效登录态(storage 有未过期 douyin cookie)即视为可处理账号。"""

    @staticmethod
    def _storage(expires_after_sec: float, domain: str = ".douyin.com"):
        import json
        import tempfile
        import time

        d = tempfile.mkdtemp()
        p = Path(d) / "storage.json"
        p.write_text(
            json.dumps(
                {
                    "cookies": [
                        {
                            "name": "sessionid",
                            "domain": domain,
                            "expires": time.time() + expires_after_sec,
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        return str(p)

    def _check(self, storage_path: str):
        with patch(
            "src.scheduler.get_account_storage_file", return_value=Path(storage_path)
        ):
            from src.scheduler import has_login_state

            return has_login_state({"name": "main"})

    def test_valid_cookie_returns_true(self):
        self.assertTrue(
            self._check(self._storage(3600)), "未过期 douyin cookie 应判定有登录态"
        )

    def test_expired_cookie_returns_false(self):
        self.assertFalse(
            self._check(self._storage(-3600)), "过期 cookie 应判定无登录态"
        )

    def test_missing_file_returns_false(self):
        self.assertFalse(
            self._check("/nonexistent/storage.json"), "无文件应判定无登录态"
        )

    def test_non_douyin_domain_returns_false(self):
        self.assertFalse(
            self._check(self._storage(3600, domain=".example.com")),
            "非 douyin 域 cookie 不应判定为登录态",
        )


class TestHealAfterSendFailure(unittest.TestCase):
    """发送静默失败(不抛异常)时的自愈兜底 —— except 分支覆盖不到的盲区。"""

    def test_triggers_heal_when_not_yet_healed(self):
        with patch("src.scheduler.self_heal", return_value=True) as heal:
            self.assertTrue(_heal_after_send_failure("好友A", False))
            heal.assert_called_once()
            self.assertEqual(heal.call_args[0][0], "process_friend")

    def test_skips_heal_when_already_healed(self):
        with patch("src.scheduler.self_heal") as heal:
            self.assertTrue(_heal_after_send_failure("好友A", True))
            heal.assert_not_called()

    def test_returns_false_when_heal_fails(self):
        with patch("src.scheduler.self_heal", return_value=False):
            self.assertFalse(_heal_after_send_failure("好友A", False))


class TestHealAfterSendFailurePassesScreenshot(unittest.TestCase):
    """自愈必须在关页前拿到截图 —— 否则页面改版场景失去视觉诊断线索。"""

    def test_screenshot_passed_when_page_alive(self):
        page = MagicMock()
        with (
            patch("src.scheduler.dump_page", return_value="/tmp/shot.png") as dump,
            patch("src.scheduler.self_heal", return_value=True) as heal,
        ):
            self.assertTrue(_heal_after_send_failure("好友A", False, page))
            dump.assert_called_once_with(page)
            self.assertEqual(
                heal.call_args.kwargs.get("screenshot_path"), "/tmp/shot.png"
            )

    def test_no_screenshot_when_page_missing(self):
        with patch("src.scheduler.self_heal", return_value=True) as heal:
            self.assertTrue(_heal_after_send_failure("好友A", False, None))
            self.assertEqual(heal.call_args.kwargs.get("screenshot_path"), "")


class TestSilentFailureHealRetry(unittest.TestCase):
    """静默失败自愈的重试上限。

    回归 2026-09-27 a2ffdfb: 发送持续返回 fail 时, `_heal_after_send_failure` 在
    ai_healed=True 下恒返回 True, 缺 `not ai_healed` 门会让 while True 无限重开会话。
    """

    def test_heal_success_retries_send_only_once(self):
        state = {"date": "2026-10-01", "sent": []}
        process = MagicMock(side_effect=[("fail", False), ("fail", False)])
        with (
            patch("src.scheduler.open_session") as mock_open,
            patch("src.scheduler.process_friend", process),
            patch("src.scheduler.self_heal", return_value=True) as mock_heal,
            patch("src.scheduler.dump_page", return_value=""),
            patch("src.scheduler.save_session_state"),
            patch("src.scheduler.close_session"),
            patch("src.scheduler.free_page_memory"),
            patch("src.scheduler.memory_report"),
            patch("src.scheduler.available_mb", return_value=2048),
            patch("src.scheduler.write_state"),
        ):
            mock_open.return_value = (MagicMock(), MagicMock(), MagicMock())
            ok, fail, failed, skipped, halted = _send_phase(
                None, {"browser_reuse": False}, {"name": "main"}, state, ["A"], False
            )

        self.assertEqual(
            process.call_count, 2, "自愈一次后仍失败必须收手, 不能无限重开会话"
        )
        self.assertEqual(mock_heal.call_count, 1, "同一好友同轮不重复烧 API")
        self.assertEqual(mock_open.call_count, 2)
        # 静默失败(无异常)按本地策略归入 transient → 值得补扫, 不进 skip
        self.assertEqual((ok, fail, failed, skipped, halted), (0, 1, ["A"], [], False))


class TestSendPacing(unittest.TestCase):
    """好友之间必须有随机停顿(拟人化节奏), 避免"零间隔连发"的机器人特征。"""

    def test_random_gap_between_friends(self):
        state = {"date": "2026-10-05", "sent": []}
        process = MagicMock(return_value=("ok", True))
        with (
            patch("src.scheduler.open_session") as mock_open,
            patch("src.scheduler.process_friend", process),
            patch("src.scheduler.save_session_state"),
            patch("src.scheduler.close_session"),
            patch("src.scheduler.free_page_memory"),
            patch("src.scheduler.memory_report"),
            patch("src.scheduler.available_mb", return_value=2048),
            patch("src.scheduler.write_state"),
            patch("src.scheduler.random.uniform", return_value=7.0) as mock_uniform,
            patch("src.scheduler.time.sleep") as mock_sleep,
        ):
            mock_open.return_value = (MagicMock(), MagicMock(), MagicMock())
            _send_phase(
                None,
                {"browser_reuse": False},
                {"name": "main"},
                state,
                ["A", "B"],
                False,
            )

        self.assertEqual(mock_uniform.call_count, 1, "仅第 2 个好友前需要停顿")
        self.assertIn(7.0, [c.args[0] for c in mock_sleep.call_args_list])

    def test_no_gap_in_dry_run(self):
        state = {"date": "2026-10-05", "sent": []}
        process = MagicMock(return_value=("ok", True))
        with (
            patch("src.scheduler.open_session") as mock_open,
            patch("src.scheduler.process_friend", process),
            patch("src.scheduler.save_session_state"),
            patch("src.scheduler.close_session"),
            patch("src.scheduler.free_page_memory"),
            patch("src.scheduler.memory_report"),
            patch("src.scheduler.available_mb", return_value=2048),
            patch("src.scheduler.write_state"),
            patch("src.scheduler.random.uniform", return_value=7.0) as mock_uniform,
        ):
            mock_open.return_value = (MagicMock(), MagicMock(), MagicMock())
            _send_phase(
                None,
                {"browser_reuse": False},
                {"name": "main"},
                state,
                ["A", "B"],
                True,
            )

        self.assertEqual(mock_uniform.call_count, 0, "dry_run 不应引入停顿")


class TestDiscoverWithHangRetry(unittest.TestCase):
    """P3 三态判别: 渲染挂死(body 空)重开重试, 不触发 AI 自愈(2026-09-30 事故)。"""

    def _call(self):
        from src.scheduler import _discover_with_hang_retry

        return _discover_with_hang_retry(
            None, {}, {"name": "main"}, {"date": "d", "sent": []}, "d", False, False
        )

    def test_success_first_try(self):
        with (
            patch("src.scheduler.open_session", return_value=(1, 2, 3)) as mos,
            patch("src.scheduler.wait_chat_list", return_value=True),
            patch("src.scheduler.discover_pending_friends", return_value=["甲"]),
            patch("src.scheduler.save_session_state") as msave,
            patch("src.scheduler.close_session") as mclose,
            patch("src.scheduler.memory_report"),
        ):
            pending, rc = self._call()
        self.assertEqual((pending, rc), (["甲"], 0))
        mos.assert_called_once()
        msave.assert_called_once()
        mclose.assert_called_once()

    def test_render_hang_then_success(self):
        with (
            patch("src.scheduler.open_session", return_value=(1, 2, 3)) as mos,
            patch("src.scheduler.wait_chat_list", side_effect=[False, True]),
            patch("src.scheduler.page_render_state", side_effect=["blank", "content"]),
            patch("src.scheduler.discover_pending_friends", return_value=["甲"]),
            patch("src.scheduler.save_session_state"),
            patch("src.scheduler.close_session"),
            patch("src.scheduler.memory_report"),
        ):
            pending, rc = self._call()
        self.assertEqual((pending, rc), (["甲"], 0))
        self.assertEqual(mos.call_count, 2, "挂死一次后应重开会话")

    def test_render_hang_exhausted_returns_1(self):
        with (
            patch("src.scheduler.open_session", return_value=(1, 2, 3)) as mos,
            patch("src.scheduler.wait_chat_list", return_value=False),
            patch("src.scheduler.page_render_state", return_value="blank"),
            patch("src.scheduler.discover_pending_friends") as mdis,
            patch("src.scheduler.close_session"),
            patch("src.scheduler.memory_report"),
        ):
            pending, rc = self._call()
        self.assertEqual((pending, rc), ([], 1))
        self.assertEqual(mos.call_count, 3, "首次+2次重开")
        mdis.assert_not_called()
        self.assertEqual(mdis.call_count, 0)

    def test_general_exception_returns_1(self):
        with (
            patch("src.scheduler.open_session", side_effect=RuntimeError("boom")),
            patch("src.scheduler.close_session") as mclose,
            patch("src.scheduler.memory_report"),
        ):
            pending, rc = self._call()
        self.assertEqual((pending, rc), ([], 1))
        mclose.assert_called_once()

    def test_login_required_returns_2(self):
        """未登录 = 账号级 rc=2, 抛异常而非退出进程(2026-10-07 审查)。

        旧实现是 sys.exit(2): SystemExit 属 BaseException, 绕过 run() 里所有
        except Exception, 一个账号掉线就让其余账号当晚根本没跑。
        """
        with (
            patch(
                "src.scheduler.open_session",
                side_effect=LoginRequiredError("账号 main 未登录且无法自动登录"),
            ),
            patch("src.scheduler.close_session") as mclose,
            patch("src.scheduler.memory_report"),
            patch("src.scheduler.dump_page", return_value=""),
        ):
            pending, rc = self._call()
        self.assertEqual((pending, rc), ([], 2))
        mclose.assert_called_once()


class TestLoginFailureIsolation(unittest.TestCase):
    """#98: 一个账号的登录失效既不带走其他账号, 也不让漏发静默。"""

    def test_send_phase_logged_out_accounts_remaining_as_failed(self):
        """登录态中断后**当前这位连同其后**的好友必须计入失败并留名。

        旧行为是静默 break: 收尾行印「发送 3 / 失败 0」, health_check 见该账号
        已收尾且零失败 → 判健康, 于是整晚漏发没有任何可观测信号。
        """
        state = {"date": "2026-10-07", "sent": []}
        seen = []

        def fake_process(page, account, name, dry_run):
            seen.append(name)
            if name == "B":
                return "logged_out", False
            return "ok", True

        with (
            patch(
                "src.scheduler.open_session",
                return_value=(MagicMock(), MagicMock(), MagicMock()),
            ),
            patch("src.scheduler.process_friend", side_effect=fake_process),
            patch("src.scheduler.save_session_state"),
            patch("src.scheduler.close_session"),
            patch("src.scheduler.free_page_memory"),
            patch("src.scheduler.memory_report"),
            patch("src.scheduler.available_mb", return_value=2048),
            patch("src.scheduler.write_state"),
            patch("src.scheduler.random.uniform", return_value=1.0),
            patch("src.scheduler.time.sleep"),
        ):
            ok, fail, failed, skipped, halted = _send_phase(
                None,
                {"browser_reuse": False},
                {"name": "main"},
                state,
                ["A", "B", "C", "D"],
                False,
            )
        self.assertTrue(halted, "登录态失效必须让本账号知道自己没跑完")
        self.assertEqual(seen, ["A", "B"])
        self.assertEqual(ok, 1)
        self.assertEqual(fail, 3, "B 自己 + C/D 一个都没发, 三个都算失败")
        self.assertEqual(skipped, ["B", "C", "D"], "登录类不值得补扫, 但要留名")
        self.assertEqual(failed, [])

    def test_send_phase_login_required_error_halts_same_way(self):
        """open_session 抛 LoginRequiredError 与 process_friend 报 logged_out 同权。"""
        state = {"date": "2026-10-07", "sent": []}
        with (
            patch(
                "src.scheduler.open_session",
                side_effect=[
                    (MagicMock(), MagicMock(), MagicMock()),
                    LoginRequiredError("账号 main 未登录且无法自动登录"),
                ],
            ),
            patch(
                "src.scheduler.process_friend", return_value=("ok", True)
            ),  # 只在 A 那轮被调用
            patch("src.scheduler.save_session_state"),
            patch("src.scheduler.close_session"),
            patch("src.scheduler.free_page_memory"),
            patch("src.scheduler.memory_report"),
            patch("src.scheduler.available_mb", return_value=2048),
            patch("src.scheduler.write_state"),
            patch("src.scheduler.random.uniform", return_value=1.0),
            patch("src.scheduler.time.sleep"),
        ):
            ok, fail, failed, skipped, halted = _send_phase(
                None,
                {"browser_reuse": False},
                {"name": "main"},
                state,
                ["A", "B", "C"],
                False,
            )
        self.assertTrue(halted)
        self.assertEqual((ok, fail), (1, 2))
        self.assertEqual(skipped, ["B", "C"])
        self.assertEqual(failed, [])

    def test_merge_rc_keeps_retryable_over_login(self):
        """退出码合并按「谁更该让 cron 行动」, 不按先后。"""
        self.assertEqual(_merge_rc(0, 2), 2)
        self.assertEqual(_merge_rc(2, 0), 2)
        self.assertEqual(_merge_rc(2, 3), 3, "掉线不该盖掉可冷启动重跑的失败")
        self.assertEqual(_merge_rc(3, 2), 3)
        self.assertEqual(_merge_rc(2, 1), 1)
        self.assertEqual(_merge_rc(1, 2), 1)
        self.assertEqual(_merge_rc(2, 5), 5, "未知退出码一律优先, 不降级成已知")

    def test_run_logged_out_account_does_not_skip_later_accounts(self):
        cfg = {
            "send_window": {"start": "23:30", "end": "00:30"},
            "accounts": [
                {"name": "first", "account": "a", "password": "b"},
                {"name": "second", "account": "c", "password": "d"},
            ],
        }
        seen = []

        def fake_open(p2, cfg_, account, browser=None):
            seen.append(account["name"])
            if account["name"] == "first":
                raise LoginRequiredError("账号 first 未登录且无法自动登录")
            return MagicMock(), MagicMock(), MagicMock()

        with (
            patch("src.scheduler.sync_playwright") as mp,
            patch("sys.argv", ["spark.py", "--force", "--no-jitter"]),
            patch("src.scheduler.load_config", return_value=cfg),
            patch(
                "src.scheduler.load_state",
                return_value={"date": "2026-08-13", "sent": []},
            ),
            patch("src.scheduler.write_state"),
            patch("src.scheduler.open_session", side_effect=fake_open),
            patch("src.scheduler.wait_chat_list", return_value=True),
            patch("src.scheduler.discover_pending_friends", return_value=[]),
            patch("src.scheduler._coverage_gate", return_value=0),
            patch("src.scheduler.streak_history.save_daily_record"),
            patch(
                "src.scheduler.streak_history.trigger_if_gap_detected",
                return_value=False,
            ),
            patch("src.scheduler.dump_page", return_value=""),
            patch("src.scheduler.close_session"),
            patch("src.scheduler.memory_report"),
            patch("src.scheduler._acquire_pid_lock", return_value=True),
            patch("src.scheduler._release_pid_lock"),
            patch("src.scheduler._heartbeat"),
        ):
            mp.return_value.__enter__.return_value = MagicMock()
            with self.assertRaises(SystemExit) as cm:
                run()
        self.assertEqual(cm.exception.code, 2, "未登录仍需可见地非 0 退出(cron 不重试)")
        self.assertIn("second", seen, "第一个账号掉线后第二个必须照跑")


class TestEmptyPendingNightRecord(unittest.TestCase):
    """#95: 「今日无待续」这一晚也必须在 streak_history 里留下痕迹。

    不写这行历史的后果: ①昨日漏发要到第二晚才比得到(空晚在历史里根本不存在);
    ②选择器/火花图标改版导致「全员看起来没火花」与「真的都没火花」在历史里
    长得一模一样 —— 无人值守一个月最难发现的就是这种静默故障。
    """

    def _invoke(self, msave, mgap):
        cfg = {"send_window": {"start": "23:30", "end": "00:30"}}
        account = {"name": "main", "account": "a", "password": "b"}
        with (
            patch("src.scheduler.sync_playwright"),
            patch(
                "src.scheduler.load_state",
                return_value={"date": "2026-10-07", "sent": []},
            ),
            patch("src.scheduler.write_state"),
            patch("src.scheduler._discover_with_hang_retry", return_value=([], 0)),
            patch("src.scheduler._coverage_gate", return_value=0),
            patch("src.scheduler.streak_history.save_daily_record", side_effect=msave),
            patch(
                "src.scheduler.streak_history.trigger_if_gap_detected", side_effect=mgap
            ),
        ):
            return process_account(
                None,
                account,
                cfg,
                datetime(2026, 10, 7, 0, 5),
                "23:30",
                "00:30",
                True,
                False,
                False,
            )

    def test_empty_pending_writes_history_row_and_checks_gap(self):
        calls = []
        rc = self._invoke(
            lambda **kw: calls.append(("save", kw)),
            lambda **kw: calls.append(("gap", kw)) or False,
        )
        self.assertEqual(rc, 0)
        kinds = [c[0] for c in calls]
        self.assertEqual(kinds, ["save", "gap"], "空晚必须先落历史再跑漏发比对")
        save_kw = calls[0][1]
        self.assertEqual(save_kw["account"], "main")
        self.assertEqual(save_kw["discovered"], [])
        self.assertEqual(save_kw["failed"], [])
        self.assertEqual(save_kw["sent"], [])
        self.assertEqual(calls[1][1]["account"], "main")

    def test_gap_check_gets_self_heal_hook_not_broken_signature(self):
        """trigger_if_gap_detected 的自愈回调必须能真被调用(签名对齐)。"""
        seen = []
        rc = self._invoke(
            lambda **kw: None,
            lambda account=None, self_heal_fn=None, **kw: (
                seen.append(self_heal_fn) or False
            ),
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(seen), 1)
        self.assertTrue(callable(seen[0]), "漏发自愈回调必须以可调用对象传入")


class TestCoverageCheckHook(unittest.TestCase):
    """P5 发现覆盖对照: 缺失→自愈成功→rc=3 冷启动重跑; 冷却中仅告警。

    熔断只允许发生在「无好友可发」时(见 test_missing_with_pending_warns_not_fuses)。
    """

    def test_no_missing_no_heal(self):
        with (
            patch(
                "src.scheduler.streak_history.check_recent_coverage", return_value=[]
            ),
            patch("src.scheduler.self_heal") as mheal,
        ):
            rc = self._invoke(["甲"])
        self.assertEqual(rc, 0)
        mheal.assert_not_called()

    def test_missing_heal_success_returns_3(self):
        with (
            patch(
                "src.scheduler.streak_history.check_recent_coverage",
                return_value=["乙"],
            ),
            patch(
                "src.scheduler.streak_history.coverage_heal_on_cooldown",
                return_value=False,
            ),
            patch("src.scheduler.streak_history.record_coverage_heal") as mrec,
            patch("src.scheduler.self_heal", return_value=True) as mheal,
        ):
            rc = self._invoke([])
        self.assertEqual(rc, 3)
        mrec.assert_called_once_with("main")
        mheal.assert_called_once()

    def test_missing_with_pending_warns_not_fuses(self):
        """2026-10-07 事故回归: 有确认待续好友时, 「疑似还漏了别人」不得熔断整轮发送。"""
        with (
            patch(
                "src.scheduler.streak_history.check_recent_coverage",
                return_value=["乙"],
            ),
            patch(
                "src.scheduler.streak_history.coverage_heal_on_cooldown",
                return_value=False,
            ),
            patch("src.scheduler.streak_history.record_coverage_heal") as mrec,
            patch("src.scheduler.self_heal") as mheal,
        ):
            rc = self._invoke(["甲"])
        self.assertEqual(rc, 0)
        mrec.assert_not_called()
        mheal.assert_not_called()

    def test_missing_on_cooldown_warns_only(self):
        with (
            patch(
                "src.scheduler.streak_history.check_recent_coverage",
                return_value=["乙"],
            ),
            patch(
                "src.scheduler.streak_history.coverage_heal_on_cooldown",
                return_value=True,
            ),
            patch("src.scheduler.streak_history.record_coverage_heal") as mrec,
            patch("src.scheduler.self_heal") as mheal,
        ):
            rc = self._invoke([])
        self.assertEqual(rc, 0)
        mrec.assert_not_called()
        mheal.assert_not_called()

    def test_heal_fail_continues(self):
        with (
            patch(
                "src.scheduler.streak_history.check_recent_coverage",
                return_value=["乙"],
            ),
            patch(
                "src.scheduler.streak_history.coverage_heal_on_cooldown",
                return_value=False,
            ),
            patch("src.scheduler.streak_history.record_coverage_heal"),
            patch("src.scheduler.self_heal", return_value=False),
        ):
            rc = self._invoke([])
        self.assertEqual(rc, 0)

    def test_heal_raises_continues(self):
        """自愈抛异常不得中断发送: 门禁捕获后按本次发现结果继续(rc=0)。"""
        with (
            patch(
                "src.scheduler.streak_history.check_recent_coverage",
                return_value=["乙"],
            ),
            patch(
                "src.scheduler.streak_history.coverage_heal_on_cooldown",
                return_value=False,
            ),
            patch("src.scheduler.streak_history.record_coverage_heal"),
            patch("src.scheduler.self_heal", side_effect=RuntimeError("boom")),
        ):
            rc = self._invoke([])
        self.assertEqual(rc, 0)

    def _invoke(self, pending):
        """直接调用 _coverage_gate(P5 已抽成纯函数, 返回 0 或 3)。"""
        from src.scheduler import _coverage_gate

        return _coverage_gate("main", pending, {"date": "d", "sent": []})


class TestPidLockExemption(unittest.TestCase):
    """PID 互斥的语义(2026-10-01 P0-1): 豁免位 / 活锁拒绝 / 陈旧锁回收 / 跨进程真实争锁。

    原缺陷: 自愈在主进程**内部**派生 `spark.py --dry-run/--verify-send` 验证子进程, 子进程
    照常争锁会看到父 PID 存活 → 走"另一实例运行中"分支 → 裸 return → rc=0 且日志无正向证据
    → 每次 AI 修复都被判 `noevidence` 并回滚(自愈从未真正生效)。获锁失败裸 return 也让
    "锁被挂死实例长期持有"在监控面完全静默。

    隔离手法: `_acquire_pid_lock()` 按 `os.path.dirname(os.path.dirname(__file__))` 定位
    runtime/, 故把模块 `__file__` 指向临时目录即可完全隔离 —— **绝不碰生产
    runtime/.spark.pid**, 否则测试会在板子上真抢走运行锁。
    """

    @staticmethod
    def _mk_proj(tmpd: str) -> Path:
        proj = Path(tmpd)
        (proj / "src").mkdir(parents=True, exist_ok=True)
        return proj

    def _fake_file(self, proj: Path) -> str:
        return str(proj / "src" / "scheduler.py")

    def _lock_file(self, proj: Path) -> Path:
        return proj / "runtime" / ".spark.pid"

    def test_exemption_env_skips_lock_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmpd:
            proj = self._mk_proj(tmpd)
            with (
                patch.object(_scheduler_mod, "__file__", self._fake_file(proj)),
                patch.object(_scheduler_mod, "_PID_FILE", None),
                patch.dict(os.environ, {"SPARK_SKIP_PID_LOCK": "1"}),
            ):
                self.assertTrue(_acquire_pid_lock())
            self.assertFalse(self._lock_file(proj).exists(), "豁免进程不得写锁文件")
            self.assertFalse(
                (proj / "runtime").exists(), "豁免进程连 runtime/ 都不该创建"
            )

    def test_live_holder_blocks_second_instance_without_overwriting(self):
        with tempfile.TemporaryDirectory() as tmpd:
            proj = self._mk_proj(tmpd)
            lock = self._lock_file(proj)
            lock.parent.mkdir(parents=True, exist_ok=True)
            lock.write_text(str(os.getpid()), encoding="utf-8")  # 本测试进程=活持锁者
            with (
                patch.object(_scheduler_mod, "__file__", self._fake_file(proj)),
                patch.object(_scheduler_mod, "_PID_FILE", None),
                # 新语义(2026-10-04): 锁里的 PID 存活还不够, 必须 cmdline 含 spark.py
                # 才算"另一实例"。这里显式模拟"持锁者确实是 spark.py"。
                patch.object(_scheduler_mod, "_pid_is_spark", return_value=True),
                patch.dict(os.environ),
            ):
                os.environ.pop("SPARK_SKIP_PID_LOCK", None)
                self.assertFalse(_acquire_pid_lock())
            self.assertEqual(
                lock.read_text(encoding="utf-8"),
                str(os.getpid()),
                "获锁失败方不得覆盖别人的锁",
            )

    def test_reused_pid_of_unrelated_process_is_reclaimed(self):
        """锁里是存活但非 spark.py 的 PID(本测试进程) → 视为陈旧锁接管并覆盖。"""
        with tempfile.TemporaryDirectory() as tmpd:
            proj = self._mk_proj(tmpd)
            lock = self._lock_file(proj)
            lock.parent.mkdir(parents=True, exist_ok=True)
            lock.write_text(str(os.getpid()), encoding="utf-8")
            with (
                patch.object(_scheduler_mod, "__file__", self._fake_file(proj)),
                patch.object(_scheduler_mod, "_PID_FILE", None),
                patch.dict(os.environ),
            ):
                os.environ.pop("SPARK_SKIP_PID_LOCK", None)
                self.assertTrue(_acquire_pid_lock())
            self.assertEqual(lock.read_text(encoding="utf-8"), str(os.getpid()))

    def test_stale_by_age_is_reclaimed(self):
        """锁里是存活 PID, 但锁文件年龄超过 _PID_STALE_SEC → 直接接管(兜住 PID 复用)。"""
        with tempfile.TemporaryDirectory() as tmpd:
            proj = self._mk_proj(tmpd)
            lock = self._lock_file(proj)
            lock.parent.mkdir(parents=True, exist_ok=True)
            lock.write_text(str(os.getpid()), encoding="utf-8")
            old = time.time() - (_scheduler_mod._PID_STALE_SEC + 3600)  # 4 小时前
            os.utime(lock, (old, old))
            with (
                patch.object(_scheduler_mod, "__file__", self._fake_file(proj)),
                patch.object(_scheduler_mod, "_PID_FILE", None),
                patch.dict(os.environ),
            ):
                os.environ.pop("SPARK_SKIP_PID_LOCK", None)
                self.assertTrue(_acquire_pid_lock())
            self.assertEqual(lock.read_text(encoding="utf-8"), str(os.getpid()))

    def test_stale_lock_is_reclaimed(self):
        # 用一个"肯定不存在"的 PID 模拟挂死实例留下的陈旧锁。
        # 按 /proc 判定而非 os.kill: 后者对**别人**的进程返回 EPERM, 会被误当"已死"。
        dead_pid = next(
            p for p in range(999_999, 1_000, -1) if not Path(f"/proc/{p}").exists()
        )
        with tempfile.TemporaryDirectory() as tmpd:
            proj = self._mk_proj(tmpd)
            lock = self._lock_file(proj)
            lock.parent.mkdir(parents=True, exist_ok=True)
            lock.write_text(str(dead_pid), encoding="utf-8")
            with (
                patch.object(_scheduler_mod, "__file__", self._fake_file(proj)),
                patch.object(_scheduler_mod, "_PID_FILE", None),
                patch.dict(os.environ),
            ):
                os.environ.pop("SPARK_SKIP_PID_LOCK", None)
                self.assertTrue(_acquire_pid_lock())
            self.assertEqual(lock.read_text(encoding="utf-8"), str(os.getpid()))

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    CHILD_SNIPPET = (
        "import os, sys\n"
        "from src import scheduler as s\n"
        "s.__file__ = sys.argv[1]\n"
        "s._PID_FILE = None\n"
        "print('ACQ=%s' % s._acquire_pid_lock())\n"
    )

    def _child_acquire(self, proj: Path, exempt: bool) -> str:
        """独立进程里争锁, 返回 'ACQ=True/False'。

        只取 ACQ 行: logutil 会把日志同时打到 stdout, 直接比整段会因日志而假失败。
        """
        root = Path(__file__).resolve().parent.parent
        env = {
            **os.environ,
            "PYTHONPATH": str(root),
            "SPARK_SKIP_PID_LOCK": "1" if exempt else "",
            # 子进程的 __main__ 是 `-c` 字符串, logutil 的"测试入口"探测认不出来,
            # 不显式指路就会把日志写进**生产** runtime/spark.log —— 单测伪造的
            # 「另一实例运行中/完成: …」足以顶掉真实告警(2026-10-04 事故)。
            "SPARK_LOG_FILE": str(proj / "runtime" / "child.test.log"),
        }
        proc = subprocess.run(
            [sys.executable, "-c", self.CHILD_SNIPPET, self._fake_file(proj)],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(root),
            env=env,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, f"子进程异常: {proc.stderr[:400]}")
        verdicts = [
            ln.strip()
            for ln in proc.stdout.splitlines()
            if ln.strip().startswith("ACQ=")
        ]
        self.assertEqual(len(verdicts), 1, f"取不到唯一判定行: {proc.stdout[:400]}")
        return verdicts[0]

    @staticmethod
    def _spawn_spark_holder(tmpd: Path):
        """启动一个真实的 spark.py 辅助进程(内容 sleep), 用于模拟"持锁者确实是 spark.py"。

        新语义下锁里的 PID 必须 cmdline 含 spark.py 才算"另一实例", 故不能再拿
        测试进程自身的 PID 充当活持锁者。
        """
        helper = tmpd / "spark.py"
        helper.write_text("import time\ntime.sleep(120)\n", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, str(helper)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(200):  # 等 /proc 条目出现, 确保写锁时该 PID 一定存活
            if Path(f"/proc/{proc.pid}/cmdline").exists():
                break
            time.sleep(0.02)
        return proc

    def test_child_process_is_blocked_by_parent_lock(self):
        """跨进程复现原缺陷前提: 持锁者是**真正的 spark.py** 时, 未豁免的子进程确实争锁失败。

        语义变化(2026-10-04): 原先用测试进程自身 PID 持锁即可阻断; 现在锁里 PID
        还要求 cmdline 含 spark.py, 否则视为 PID 复用并接管。故改用真实 spark.py
        辅助进程持锁。
        """
        with tempfile.TemporaryDirectory() as tmpd:
            proj = self._mk_proj(tmpd)
            lock = self._lock_file(proj)
            lock.parent.mkdir(parents=True, exist_ok=True)
            holder = self._spawn_spark_holder(Path(tmpd))
            try:
                lock.write_text(str(holder.pid), encoding="utf-8")
                self.assertEqual(self._child_acquire(proj, exempt=False), "ACQ=False")
                self.assertEqual(lock.read_text(encoding="utf-8"), str(holder.pid))
            finally:
                holder.terminate()
                holder.wait(timeout=10)

    def test_exempt_child_proceeds_despite_parent_lock(self):
        """P0-1 回归: 豁免子进程照常获锁(才能产出正向证据), 且不破坏父进程的锁。"""
        with tempfile.TemporaryDirectory() as tmpd:
            proj = self._mk_proj(tmpd)
            lock = self._lock_file(proj)
            lock.parent.mkdir(parents=True, exist_ok=True)
            lock.write_text(str(os.getpid()), encoding="utf-8")
            self.assertEqual(self._child_acquire(proj, exempt=True), "ACQ=True")
            self.assertEqual(
                lock.read_text(encoding="utf-8"),
                str(os.getpid()),
                "豁免子进程不得删改父进程锁文件",
            )

    def test_run_exits_4_when_lock_not_acquired(self):
        """获锁失败必须非 0 退出: rc=4 让 cron 与自愈都能区分"跳过"和"成功"。"""
        with (
            patch.object(_scheduler_mod, "_acquire_pid_lock", return_value=False),
            patch.object(
                _scheduler_mod,
                "load_config",
                return_value={"accounts": [{"name": "a"}]},
            ),
            patch.object(_scheduler_mod, "in_window", return_value=True),
            patch.object(sys, "argv", ["spark.py", "--dry-run", "--no-jitter"]),
            self.assertRaises(SystemExit) as ctx,
        ):
            run()
        self.assertEqual(ctx.exception.code, 4)


class TestHeartbeat(unittest.TestCase):
    """业务心跳(2026-10-06, ChatGPT round-2 ④): 每个业务事件敲一次 heartbeat.json,
    让健康检查能区分「进程活着且在推进」与「进程活着但卡死」。"""

    def test_heartbeat_written_and_atomic(self):
        with tempfile.TemporaryDirectory() as tmpd:
            hb = Path(tmpd) / "sub" / "heartbeat.json"
            with patch.object(_scheduler_mod, "_HEARTBEAT_FILE", hb):
                _scheduler_mod._heartbeat("friend_start:某某", "main", "某某")
                data = json.loads(hb.read_text(encoding="utf-8"))
                self.assertEqual(data["stage"], "friend_start:某某")
                self.assertEqual(data["account"], "main")
                self.assertEqual(data["friend"], "某某")
                self.assertIn("ts", data)
                # 二次敲覆盖而非追加, 且不留 tmp
                _scheduler_mod._heartbeat("account_done", "main")
                data2 = json.loads(hb.read_text(encoding="utf-8"))
                self.assertGreaterEqual(data2["ts"], data["ts"])
                self.assertFalse((hb.parent / "heartbeat.tmp").exists())

    def test_heartbeat_failure_never_raises(self):
        """写心跳失败(路径不可写等)必须静默 —— 它绝不能在主流程中途引爆。"""
        with patch.object(
            _scheduler_mod,
            "_HEARTBEAT_FILE",
            Path("/proc/definitely-not-writable/hb.json"),
        ):
            _scheduler_mod._heartbeat("stage", "acc")  # 不抛异常即通过

    def test_heartbeat_name_redirects_offline_entry(self):
        """板端无 pytest → `python -m unittest` 逐模块跑时 conftest 夹具不加载,
        心跳必须靠 logutil._offline_entry 同源判据自行分流, 否则覆盖生产 heartbeat.json。"""
        with patch.object(_scheduler_mod, "_offline_entry", return_value=True):
            self.assertEqual(
                _scheduler_mod._heartbeat_filename(), "heartbeat.test.json"
            )
        with patch.object(_scheduler_mod, "_offline_entry", return_value=False):
            self.assertEqual(_scheduler_mod._heartbeat_filename(), "heartbeat.json")

    def test_process_friend_beats_on_start(self):
        with tempfile.TemporaryDirectory() as tmpd:
            hb = Path(tmpd) / "heartbeat.json"
            page = MagicMock()
            with (
                patch.object(_scheduler_mod, "_HEARTBEAT_FILE", hb),
                patch("src.scheduler.wait_chat_ui", return_value=True),
                patch("src.scheduler.wait_chat_list", return_value=True),
                patch("src.login.is_logged_in", return_value=True),
                patch("src.scheduler.click_friend", return_value="clicked"),
                patch(
                    "src.scheduler.verify_chat_opened_ready", return_value=(True, "ok")
                ),
                patch("src.scheduler.send_sticker", return_value=(True, "策略1")),
            ):
                status, sent = process_friend(page, {"name": "main"}, "某某", False)
            self.assertTrue(sent)
            self.assertEqual(status, "ok")
            data = json.loads(hb.read_text(encoding="utf-8"))
            self.assertEqual(data["stage"], "friend_start:某某")
            self.assertEqual(data["account"], "main")


class TestSendPhasePageReuse(unittest.TestCase):
    """跨好友复用会话页面: 只对「干净成功」复用, 失败一律重建。

    背景: 旧实现每个好友都新建 context + 整页加载 /chat(板端 ~30s/好友)。
    """

    def _call(self, pending, outcomes, max_friends="3"):
        state = {"date": "2026-10-07", "sent": []}
        page = MagicMock(name="page")
        page.is_closed.return_value = False
        with (
            patch.dict(
                "os.environ",
                {"SPARK_REUSE_PAGE": "1", "SPARK_PAGE_REUSE_MAX": max_friends},
            ),
            patch(
                "src.scheduler.open_session",
                return_value=(MagicMock(), MagicMock(), page),
            ) as open_session,
            patch("src.scheduler._page_usable", return_value=True),
            patch("src.scheduler.process_friend", side_effect=outcomes),
            patch("src.scheduler.save_session_state"),
            patch("src.scheduler.close_session"),
            patch("src.scheduler.free_page_memory"),
            patch("src.scheduler.memory_report"),
            patch("src.scheduler.available_mb", return_value=2048),
            patch("src.scheduler.write_state"),
            patch("src.scheduler.random.uniform", return_value=0.0),
            patch("src.scheduler.time.sleep"),
        ):
            _send_phase(
                None, {"browser_reuse": True}, {"name": "main"}, state, pending, False
            )
        return open_session

    def test_clean_success_reuses_one_page(self):
        """三个好友都干净成功, 且上限 3 → 只开一次会话。"""
        open_session = self._call(["A", "B", "C"], [("ok", True)] * 3)
        self.assertEqual(open_session.call_count, 1)

    def test_failure_forces_rebuild(self):
        """中间一个好友没确认成功 → 下一个必须重建页面, 不复用。"""
        open_session = self._call(
            ["A", "B", "C"],
            [("ok", True), ("ok", False), ("ok", True)],
        )
        self.assertEqual(open_session.call_count, 2)

    def test_discarding_kept_page_closes_its_context(self):
        """保留页被丢弃时必须先关闭它的 context(否则每丢弃一次漏一个 context)。"""
        state = {"date": "2026-10-07", "sent": []}
        page = MagicMock(name="page")
        page.is_closed.return_value = False
        ctx = MagicMock(name="ctx")
        with (
            patch.dict(
                "os.environ",
                {"SPARK_REUSE_PAGE": "1", "SPARK_PAGE_REUSE_MAX": "3"},
            ),
            patch(
                "src.scheduler.open_session",
                return_value=(MagicMock(), ctx, page),
            ),
            # 保留页不可用 → 丢弃它(必须先关掉它的 context)
            patch("src.scheduler._page_usable", return_value=False),
            patch(
                "src.scheduler.process_friend", side_effect=[("ok", True), ("ok", True)]
            ),
            patch("src.scheduler.save_session_state"),
            patch("src.scheduler.close_session") as close_session,
            patch("src.scheduler.free_page_memory"),
            patch("src.scheduler.memory_report"),
            patch("src.scheduler.available_mb", return_value=2048),
            patch("src.scheduler.write_state"),
            patch("src.scheduler.random.uniform", return_value=0.0),
            patch("src.scheduler.time.sleep"),
        ):
            _send_phase(
                None,
                {"browser_reuse": True},
                {"name": "main"},
                state,
                ["A", "B"],
                False,
            )
        closed_ctxs = [
            c.args[1] for c in close_session.call_args_list if len(c.args) > 1
        ]
        self.assertIn(ctx, closed_ctxs)

    def test_upper_bound_friends_per_page(self):
        """上限 2 → 四个好友至少开两次会话(不会无限复用同一个页面)。"""
        open_session = self._call(
            ["A", "B", "C", "D"], [("ok", True)] * 4, max_friends="2"
        )
        self.assertGreaterEqual(open_session.call_count, 2)


if __name__ == "__main__":
    unittest.main()
