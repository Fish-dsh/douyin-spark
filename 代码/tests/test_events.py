"""结构化事件层测试: 字段完整、task_id 传播、attempt 递增、写失败不抛。"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestEvents(unittest.TestCase):
    def setUp(self):
        from src import events

        self.events = events
        self._tmp = tempfile.mkdtemp(prefix="spark-events-")
        self._orig = events.EVENTS_FILE
        events.EVENTS_FILE = Path(self._tmp) / "events.jsonl"
        events.start_task()

    def tearDown(self):
        self.events.EVENTS_FILE = self._orig

    def _lines(self):
        return [
            json.loads(line)
            for line in self.events.EVENTS_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_emit_writes_all_minimal_fields(self):
        self.events.emit(
            "sticker",
            "send",
            "failed",
            friend="小淇淇",
            account="main",
            verified=False,
            error_class="panel_not_opened",
        )
        rec = self._lines()[0]
        for key in (
            "event_id",
            "task_id",
            "ts",
            "layer",
            "action",
            "result",
            "friend",
            "account",
            "verified",
            "attempt_id",
            "parent_attempt_id",
            "error_class",
        ):
            self.assertIn(key, rec)
        self.assertEqual(rec["layer"], "sticker")
        self.assertEqual(rec["error_class"], "panel_not_opened")
        self.assertFalse(rec["verified"])
        self.assertTrue(rec["task_id"])

    def test_task_id_shared_and_attempts_increment(self):
        a1 = self.events.attempt_id()
        a2 = self.events.attempt_id()
        self.assertNotEqual(a1, a2)
        self.assertTrue(a2.endswith(":2"))
        self.assertEqual(a1.split(":")[0], self.events.current_task_id())

    def test_parent_chain_is_recorded(self):
        parent = self.events.attempt_id()
        child = self.events.attempt_id()
        self.events.emit(
            "enter", "send", "ok", verified=True, attempt=child, parent=parent
        )
        rec = self._lines()[0]
        self.assertEqual(rec["parent_attempt_id"], parent)
        self.assertEqual(rec["attempt_id"], child)

    def test_emit_never_raises_on_unwritable_path(self):
        self.events.EVENTS_FILE = Path("/proc/definitely/not/writable/events.jsonl")
        self.events.emit("task", "start", "ok")  # 不抛 = 通过

    def test_read_events_skips_bad_lines(self):
        self.events.emit("task", "start", "ok")
        with self.events.EVENTS_FILE.open("a", encoding="utf-8") as f:
            f.write("{坏行\n")
        self.events.emit("task", "end", "ok")
        rows = self.events.read_events()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]["action"], "end")

    def test_read_events_missing_file(self):
        self.events.EVENTS_FILE = Path(self._tmp) / "nope.jsonl"
        self.assertEqual(self.events.read_events(), [])


class TestHeartbeatIdentity(unittest.TestCase):
    """心跳必须带 task_id + 递增 seq, 否则读者无法区分两轮任务。"""

    def test_heartbeat_carries_task_id_and_seq(self):
        from src import scheduler

        tmp = tempfile.mkdtemp(prefix="spark-hb-")
        sched_file = Path(tmp) / "heartbeat.json"
        scheduler._HB_SEQ = 0
        scheduler._HB_STAGE = ""
        scheduler._TASK_ID = "20261101T000000-1"
        with mock.patch.object(scheduler, "_HEARTBEAT_FILE", sched_file):
            scheduler._heartbeat("run_start")
            scheduler._heartbeat("run_start")
            rec = json.loads(sched_file.read_text(encoding="utf-8"))
        self.assertEqual(rec["task_id"], "20261101T000000-1")
        self.assertEqual(rec["seq"], 2)
        self.assertIn("stage_start_ts", rec)


if __name__ == "__main__":
    unittest.main()
