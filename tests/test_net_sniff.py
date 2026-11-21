"""net_sniff.py tests: passive capture, caps, rotation, main-path safety."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.net_sniff import (
    _MAX_PAYLOADS,
    NetSniffer,
    counts,
    note_send_window,
    sniff_report,
)


class FakeResponse:
    def __init__(self, url, body=b"{}", resource_type="xhr", boom=False):
        self.url = url
        self._body = body
        self._boom = boom
        self.request = type("R", (), {"resource_type": resource_type})()

    def body(self):
        if self._boom:
            raise RuntimeError("target page closed")
        return self._body


class FakeWS:
    def __init__(self, url="wss://im.douyin.com/ws"):
        self.url = url
        self._handlers = {}

    def on(self, event, handler):
        self._handlers[event] = handler

    def emit_frame(self, text):
        frame = type("F", (), {"text": text})()
        self._handlers["framereceived"](frame)

    def emit_sent(self, payload=b""):
        frame = type("F", (), {"payload": payload})()
        self._handlers["framesent"](frame)


class FakePage:
    def __init__(self):
        self._handlers = {}

    def on(self, event, handler):
        self._handlers[event] = handler


def _conv_payload():
    return json.dumps(
        {"data": {"conversation_list": [{"nickname": "小明", "spark_count": 3}]}}
    ).encode()


class TestAttach(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.page = FakePage()
        self.sniffer = NetSniffer("main", samples_dir=self.dir)
        self.sniffer.attach(self.page)

    def test_registers_listeners_and_exposes_self(self):
        self.assertIn("response", self.page._handlers)
        self.assertIn("websocket", self.page._handlers)
        self.assertIs(self.page._spark_sniffer, self.sniffer)

    def test_matching_xhr_captured(self):
        resp = FakeResponse(
            "https://www.douyin.com/im/conversation/list", _conv_payload()
        )
        self.page._handlers["response"](resp)
        self.assertEqual(self.sniffer.recorded, 1)
        self.assertEqual(
            self.sniffer.parsed_payloads()[0]["obj"]["data"]["conversation_list"][0][
                "nickname"
            ],
            "小明",
        )

    def test_unrelated_url_ignored(self):
        self.page._handlers["response"](
            FakeResponse("https://www.douyin.com/feed/x", _conv_payload())
        )
        self.assertEqual(self.sniffer.recorded, 0)

    def test_url_hit_body_hint_miss_ignored(self):
        resp = FakeResponse("https://www.douyin.com/im/streak/poll", b'{"status": 1}')
        self.page._handlers["response"](resp)
        self.assertEqual(self.sniffer.recorded, 0)

    def test_non_xhr_type_ignored(self):
        resp = FakeResponse(
            "https://www.douyin.com/im/conversation",
            _conv_payload(),
            resource_type="script",
        )
        self.page._handlers["response"](resp)
        self.assertEqual(self.sniffer.recorded, 0)

    def test_oversize_body_ignored(self):
        resp = FakeResponse(
            "https://www.douyin.com/im/conversation",
            b"x" * 300 * 1024 + _conv_payload(),
        )
        self.page._handlers["response"](resp)
        self.assertEqual(self.sniffer.recorded, 0)

    def test_body_exception_swallowed(self):
        resp = FakeResponse("https://www.douyin.com/im/conversation", None, boom=True)
        self.page._handlers["response"](resp)  # 不抛异常即通过
        self.assertEqual(self.sniffer.recorded, 0)

    def test_ws_text_frame_captured(self):
        ws = FakeWS()
        self.page._handlers["websocket"](ws)
        ws.emit_frame('{"conversation": {"nickname": "阿花", "streak": 5}}')
        self.assertEqual(self.sniffer.recorded, 1)
        self.assertEqual(self.sniffer.parsed_payloads()[0]["kind"], "ws")

    def test_ws_binary_frame_swallowed(self):
        ws = FakeWS()
        self.page._handlers["websocket"](ws)
        frame = type(
            "F",
            (),
            {
                "text": property(
                    lambda self: (_ for _ in ()).throw(ValueError("binary"))
                )
            },
        )()
        ws._handlers["framereceived"](frame)
        self.assertEqual(self.sniffer.recorded, 0)

    def test_payload_cap(self):
        for i in range(_MAX_PAYLOADS + 10):
            self.page._handlers["response"](
                FakeResponse(
                    f"https://www.douyin.com/im/conversation/{i}", _conv_payload()
                )
            )
        self.assertEqual(self.sniffer.recorded, _MAX_PAYLOADS)

    def test_samples_written_as_jsonl(self):
        self.page._handlers["response"](
            FakeResponse("https://www.douyin.com/im/conversation/list", _conv_payload())
        )
        files = list(self.dir.glob("net_*.jsonl"))
        self.assertEqual(len(files), 1)
        line = json.loads(files[0].read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(line["kind"], "resp")
        self.assertIn("conversation", line["url"])


class TestMemoryBound(unittest.TestCase):
    def test_parsed_ring_buffer(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        page = FakePage()
        sniffer = NetSniffer("acc", samples_dir=Path(tmp.name))
        sniffer.attach(page)
        for i in range(10):
            payload = json.dumps({"conversation": {"i": i}}).encode()
            page._handlers["response"](
                FakeResponse("https://x/im/conversation", payload)
            )
        parsed = sniffer.parsed_payloads()
        self.assertLessEqual(len(parsed), 6)
        self.assertEqual(parsed[-1]["obj"]["conversation"]["i"], 9)


class TestSentFrames(unittest.TestCase):
    """上行帧只计数不留样本: 供发送窗口判断「客户端有没有往外推数据」。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.page = FakePage()
        self.sniffer = NetSniffer("main", samples_dir=Path(self.tmp.name))
        self.sniffer.attach(self.page)
        self.ws = FakeWS()
        self.page._handlers["websocket"](self.ws)

    def test_sent_frames_counted_without_storing(self):
        for _ in range(3):
            self.ws.emit_sent(b"\x08\x01")
        self.assertEqual(self.sniffer.counts(), (0, 3))
        self.assertEqual(self.sniffer.recorded, 0)  # 二进制帧一条样本都不落

    def test_sent_count_survives_payload_cap(self):
        # 记录额度用完不该连带把旁证计数一起冻住(旧 _frame 的 return 有这毛病)
        self.sniffer.recorded = _MAX_PAYLOADS
        self.ws.emit_sent()
        self.ws.emit_sent()
        self.assertEqual(self.sniffer.counts()[1], 2)

    def test_counts_without_sniffer_is_zero(self):
        self.assertEqual(counts(FakePage()), (0, 0))

    def test_counts_none_page_is_zero(self):
        self.assertEqual(counts(None), (0, 0))


class TestSendWindow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.page = FakePage()
        self.sniffer = NetSniffer("main", samples_dir=Path(self.tmp.name))
        self.sniffer.attach(self.page)
        self.ws = FakeWS()
        self.page._handlers["websocket"](self.ws)

    def test_returns_delta(self):
        before = counts(self.page)
        self.ws.emit_sent()
        self.ws.emit_sent()
        self.ws.emit_frame('{"conversation": {}}')
        self.assertEqual(note_send_window(self.page, before, "小明"), (2, 1))

    def test_no_traffic_still_reported(self):
        # 一点动静都没有也是一条结论(旁证=无), 不能静默跳过
        self.assertEqual(note_send_window(self.page, counts(self.page)), (0, 0))

    def test_without_sniffer_returns_none(self):
        self.assertIsNone(note_send_window(FakePage(), (0, 0)))

    def test_counter_reset_clamped_to_zero(self):
        # 窗口里发生页面重载时计数从头开始, 差值不许是负数
        self.ws.emit_sent()
        self.assertEqual(note_send_window(self.page, (0, 99)), (0, 0))


class TestSniffReport(unittest.TestCase):
    def test_no_sniffer_is_noop(self):
        sniff_report(FakePage())  # 不抛异常

    def test_none_page_is_noop(self):
        sniff_report(None)


class TestRotation(unittest.TestCase):
    def test_old_files_pruned(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        for i in range(12):
            (d / f"net_2026010{i}_000000_main.jsonl").write_text(
                "{}\n", encoding="utf-8"
            )
        page = FakePage()
        sniffer = NetSniffer("main", samples_dir=d)
        sniffer.attach(page)
        page._handlers["response"](
            FakeResponse("https://x/im/conversation", _conv_payload())
        )
        self.assertEqual(len(list(d.glob("net_*.jsonl"))), 8)


class TestAccountNameSanitized(unittest.TestCase):
    def test_special_chars_replaced(self):
        s = NetSniffer("汽修二班。梁云茜/../etc")
        self.assertNotIn("/", s.account)
        self.assertTrue(s.account)


if __name__ == "__main__":
    unittest.main()
