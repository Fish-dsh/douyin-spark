"""发送与补扫的状态转换回归测试。"""

from __future__ import annotations

import unittest
from unittest.mock import ANY, MagicMock, patch

from src import sender as sender_mod
from src.scheduler import _retry_phase, _send_phase, process_friend


def _js_fake(inject=None, read_box=None, clear=None, box_empty=None, tail=None):
    """按 JS 脚本识别调用，避免位置式 side_effect 错位。"""

    def pick(seq, default):
        value = seq.pop(0) if seq else default
        if isinstance(value, BaseException):
            raise value
        return value

    def fake(js, *args):
        if "btnClicked" in js:  # _JS_INJECT_AND_SEND
            return pick(inject, {"ok": True, "btnClicked": False, "got": "续火花"})
        if "sticker_not_found" in js:  # 只用于别的测试，不应进入这里
            raise AssertionError("send_message 不该点表情")
        if "cleared: true" in js:  # _JS_CLEAR_BOX
            return pick(clear, {"ok": True, "cleared": True})
        if (
            "replace(/[\\u00a0\\u1680\\u2000-\\u200a\\u202f\\u205f\\u3000]/g, ' ').trim();"
            in js
        ):
            return pick(read_box, "")
        if "cleaned" in js:  # _JS_BOX_EMPTY
            return pick(box_empty, False)
        if "messageMessageList" in js:  # _JS_MSG_TAIL
            return pick(tail, {"n": 0, "last": "", "prev": ""})
        raise AssertionError(f"未预期 JS: {js[:80]}")

    return fake


class TestSendMessagePartialState(unittest.TestCase):
    def setUp(self):
        self.page = MagicMock()
        self.box = MagicMock()

    def test_inject_ok_click_exception_does_not_fall_back_to_typing(self):
        """策略A 文本已就位但点击异常：报未确认，不回退策略B 再打一条。"""
        self.page.evaluate.side_effect = _js_fake(
            inject=[{"ok": True, "btnClicked": False, "got": "续火花"}]
        )
        with (
            patch("src.sender.find", return_value=self.box),
            patch(
                "src.sender.try_send_button_or_enter",
                side_effect=RuntimeError("click boom"),
            ) as click,
        ):
            sent, method = sender_mod.send_message(self.page, "续火花")

        self.assertFalse(sent)
        self.assertIn("发送点击异常", method)
        click.assert_called_once()
        self.page.keyboard.type.assert_not_called()

    def test_inject_state_exception_with_equivalent_box_retries_send_only(self):
        """注入 JS 抛异常但读回等价：不重注入，只再点一次发送。"""
        self.page.evaluate.side_effect = _js_fake(
            inject=[RuntimeError("inject boom")], read_box=["续火花"]
        )
        with (
            patch("src.sender.find", return_value=self.box),
            patch(
                "src.sender.try_send_button_or_enter", return_value="点击发送"
            ) as click,
        ):
            sent, method = sender_mod.send_message(self.page, "续火花")

        self.assertTrue(sent)
        self.assertIn("JS注入+点击发送", method)
        click.assert_called_once_with(self.page, attempt=ANY, friend="")
        self.page.keyboard.type.assert_not_called()

    def test_inject_state_exception_with_different_box_disables_send(self):
        """注入异常且读回不等价：禁止发送，不把脏草稿当成功。"""
        self.page.evaluate.side_effect = _js_fake(
            inject=[RuntimeError("inject boom")], read_box=["别的内容"]
        )
        with (
            patch("src.sender.find", return_value=self.box),
            patch("src.sender.try_send_button_or_enter") as click,
        ):
            sent, method = sender_mod.send_message(self.page, "续火花")

        self.assertFalse(sent)
        self.assertIn("策略B 输入内容与目标不等价", method)
        click.assert_not_called()
        self.assertEqual(self.page.keyboard.type.call_count, 2)


class _ProcessFriendSetup(unittest.TestCase):
    def setUp(self):
        self.page = MagicMock()

    def invoke(self, *, sent, okc, identity_ok=True, cv_ok=False):
        stickers = (False, "未找到表情按钮")
        confirmed = (okc, "确认结果")
        events: list[tuple[tuple[str, ...], dict]] = []
        with (
            patch("src.scheduler.wait_chat_ui", return_value=True),
            patch("src.scheduler.wait_chat_list", return_value=True),
            patch("src.scheduler.click_friend", return_value=MagicMock()),
            patch("src.scheduler._verify_friend_identity", return_value=identity_ok),
            patch("src.login.is_logged_in", return_value=True),
            patch("src.scheduler.send_sticker", return_value=stickers),
            patch("src.scheduler.read_tail", return_value=(2, "昨天", "前天")),
            patch("src.scheduler.send_message", return_value=(sent, "策略A")) as send,
            patch("src.scheduler.confirm_send", return_value=confirmed) as confirm,
            patch(
                "src.scheduler.cv_send_fallback", return_value=(cv_ok, "兜底结果")
            ) as cv,
            patch(
                "src.scheduler.events.emit",
                side_effect=lambda *a, **k: events.append((a, k)),
            ),
        ):
            result = process_friend(self.page, {"name": "main"}, "好友A", False)
        return result, send, confirm, cv, events


class TestProcessFriendConfirmEvidence(_ProcessFriendSetup):
    def test_sent_but_confirm_false_is_failure_and_event_marks_unverified(self):
        """发送报已尝试、确认否证 → fail/sent=False，事件 verified=None。"""
        result, _, _, _, events = self.invoke(sent=True, okc=False)

        self.assertEqual(result, ("fail", False))
        text_events = [
            (a, k)
            for a, k in events
            if len(a) >= 2 and a[0] == "text" and a[1] == "send"
        ]
        self.assertEqual(len(text_events), 1)
        a, k = text_events[0]
        self.assertEqual(a[2], "failed")
        self.assertIs(k.get("verified"), None)
        self.assertEqual(k.get("extra"), {"strategy_sent": True, "confirmed": False})
        self.assertTrue(
            all(
                not (len(a) >= 2 and a[0] == "text" and a[1] == "verify")
                for a, _ in events
            ),
            "本测试桩住 confirm_send，不应另有真实确认事件",
        )


class TestProcessFriendGuards(_ProcessFriendSetup):
    def test_identity_conflict_never_attempts_send(self):
        """右栏身份核对失败 → 不发表情/文字，也不落发送事件。"""
        result, send, confirm, cv, events = self.invoke(
            sent=True, okc=True, identity_ok=False
        )

        self.assertEqual(result, ("fail", False))
        send.assert_not_called()
        confirm.assert_not_called()
        cv.assert_not_called()
        self.assertFalse(
            [e for e in events if len(e[0]) >= 2 and e[0][:2] == ("text", "send")]
        )

    def test_sent_and_confirmed_without_cv_success_is_ok(self):
        """文字已发送且确认成功 → OK，不触发视觉兜底。"""
        result, send, confirm, cv, events = self.invoke(sent=True, okc=True)

        self.assertEqual(result, ("ok", True))
        send.assert_called_once()
        confirm.assert_called_once()
        cv.assert_not_called()
        ok_send = [
            k for a, k in events if len(a) >= 3 and a[:3] == ("text", "send", "ok")
        ]
        self.assertEqual(len(ok_send), 1)
        self.assertIs(ok_send[0].get("verified"), True)
        self.assertEqual(
            ok_send[0].get("extra"), {"strategy_sent": True, "confirmed": True}
        )

    def test_cv_recovery_after_unconfirmed_send_marks_sent(self):
        """文字未确认但视觉兜底成功 → process_friend 报已发送（保留灰色地带语义）。"""
        result, _, _, cv, events = self.invoke(sent=True, okc=False, cv_ok=True)

        self.assertEqual(result, ("ok", True))
        cv.assert_called_once()
        send_events = [
            a[2] for a, _ in events if len(a) >= 3 and a[:2] == ("text", "send")
        ]
        self.assertEqual(send_events, ["failed"], "视觉兜底不补发 text/send 成功事件")


class TestSendPhaseSkipsAlreadySent(unittest.TestCase):
    def test_state_sent_entry_skips_send_phase_without_session(self):
        """发送主阶段遇到已发好友，也不再 open_session / process_friend。"""
        state = {"date": "2026-11-20", "sent": ["好友A"]}
        p2 = MagicMock()
        with (
            patch("src.scheduler.open_session") as open_session,
            patch("src.scheduler.process_friend") as process,
        ):
            result = _send_phase(p2, {}, {"name": "main"}, state, ["好友A"], False)

        self.assertEqual(result, (1, 0, [], [], False))
        open_session.assert_not_called()
        process.assert_not_called()


class TestRetryPhaseSkipsAlreadySent(unittest.TestCase):
    def test_state_sent_entry_skips_retry_without_new_session(self):
        """补扫遇到已发好友直接跳过，不重复打开会话/发送。"""
        state = {"date": "2026-11-20", "sent": ["好友A"]}
        with (
            patch("src.scheduler.open_session") as open_session,
            patch("src.scheduler.process_friend") as process,
        ):
            ok, fail = _retry_phase(
                MagicMock(), {"name": "main"}, {"name": "main"}, state, ["好友A"]
            )

        self.assertEqual((ok, fail), (1, 0))
        open_session.assert_not_called()
        process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
