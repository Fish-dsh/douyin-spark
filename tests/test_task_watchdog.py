"""task_watchdog 的判定表测试。

只测 classify() 的纯函数判定 —— 文件读取/落盘属于薄壳, 由 --json 手工验过一次即可。
"""

import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import task_watchdog as tw


def _hb(
    stage="account_done", age_sec=60, stage_age_sec=None, task_id="t1", seq=3, now=None
):
    now = now or datetime.now()
    rec = {
        "ts": now.timestamp() - age_sec,
        "iso": "2026-11-01 00:10:00",
        "task_id": task_id,
        "seq": seq,
        "stage": stage,
    }
    if stage_age_sec is not None:
        rec["stage_start_ts"] = now.timestamp() - stage_age_sec
    return rec


class TestClassify(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now().replace(hour=0, minute=30, second=0, microsecond=0)

    def test_healthy(self):
        status, detail = tw.classify(_hb(age_sec=60, now=self.now), self.now)
        self.assertEqual(status, "HEALTHY", detail)

    def test_out_of_window(self):
        now = self.now.replace(hour=12)
        status, _ = tw.classify(_hb(now=now), now)
        self.assertEqual(status, "OUT_OF_WINDOW")

    def test_missing_when_no_heartbeat(self):
        status, _ = tw.classify(None, self.now)
        self.assertEqual(status, "MISSING")

    def test_missing_when_no_task_id(self):
        # 旧版本写的心跳没有 task_id: 无法判断属于哪一轮, 宁可报 MISSING 也不当 HEALTHY。
        status, detail = tw.classify(_hb(task_id="", now=self.now), self.now)
        self.assertEqual(status, "MISSING", detail)

    def test_stale_when_heartbeat_stops_updating(self):
        status, detail = tw.classify(
            _hb(stage="friend_start:x", age_sec=3600, now=self.now), self.now
        )
        self.assertEqual(status, "STALE", detail)

    def test_page_stalled_when_heartbeat_fresh_but_stage_stuck(self):
        hb = _hb(stage="friend_start:x", age_sec=30, stage_age_sec=3600, now=self.now)
        status, detail = tw.classify(hb, self.now)
        self.assertEqual(status, "PAGE_STALLED", detail)

    def test_jitter_is_not_stall(self):
        # 抖动最多 30 分钟, 这期间 run_start 静默是正常的。
        status, detail = tw.classify(
            _hb(stage="run_start", age_sec=900, now=self.now), self.now
        )
        self.assertEqual(status, "HEALTHY", detail)

    def test_allowance_lookup(self):
        self.assertEqual(tw._allowance("friend_start:张三"), 10 * 60)
        self.assertEqual(tw._allowance("account_start:main"), 40 * 60)
        self.assertEqual(tw._allowance("unknown_stage"), 20 * 60)


if __name__ == "__main__":
    unittest.main()
