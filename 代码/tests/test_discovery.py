"""discovery.py 单测：好友发现编排、自愈触发、pending 过滤。"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from src import spark_status
from src.discovery import HealRetryFailed, discover_pending_friends
from src.spark_status import SparkDiscoveryError


class TestDiscoverPendingFriends(unittest.TestCase):
    def setUp(self):
        self.page = MagicMock()
        self.cfg = {"auto_login": True, "block_resources": True}
        self.state = {"date": "2026-08-13", "sent": ["已发好友"]}
        self.today = "2026-08-13"

    @patch("src.discovery.discover_friends_with_spark")
    @patch("src.discovery.dump_page")
    def test_discover_need_friends(self, mock_dump, mock_discover):
        mock_discover.return_value = [
            {
                "name": "需续好友1",
                "streak_days": 5,
                "status": "need",
                "isPinned": False,
            },
            {
                "name": "已续好友",
                "streak_days": 10,
                "status": "active",
                "isPinned": False,
            },
            {
                "name": "置顶无火花",
                "streak_days": 0,
                "status": "pinned",
                "isPinned": True,
            },
            {
                "name": "未知状态",
                "streak_days": 0,
                "status": "unknown",
                "isPinned": False,
            },
        ]
        mock_dump.return_value = "/tmp/shot.png"

        pending = discover_pending_friends(
            self.page, self.cfg, self.state, self.today, False, False
        )

        self.assertEqual(pending, ["需续好友1", "置顶无火花"])

    @patch("src.discovery.discover_friends_with_spark")
    @patch("src.discovery.dump_page")
    def test_force_ignores_sent(self, mock_dump, mock_discover):
        mock_discover.return_value = [
            {"name": "已发好友", "streak_days": 5, "status": "need", "isPinned": False},
        ]
        mock_dump.return_value = "/tmp/shot.png"

        # force=True should ignore already sent
        pending = discover_pending_friends(
            self.page, self.cfg, self.state, self.today, True, False
        )

        self.assertEqual(pending, ["已发好友"])

    @patch("src.discovery.discover_friends_with_spark")
    @patch("src.discovery.dump_page")
    def test_skip_active_and_pinned(self, mock_dump, mock_discover):
        mock_discover.return_value = [
            {
                "name": "活跃火花",
                "streak_days": 10,
                "status": "active",
                "isPinned": False,
            },
            {"name": "置顶", "streak_days": 0, "status": "pinned", "isPinned": True},
            {"name": "需续", "streak_days": 3, "status": "need", "isPinned": False},
        ]
        mock_dump.return_value = "/tmp/shot.png"

        pending = discover_pending_friends(
            self.page, self.cfg, self.state, self.today, False, False
        )

        self.assertEqual(pending, ["置顶", "需续"])

    @patch("src.discovery.discover_friends_with_spark")
    @patch("src.discovery.dump_page")
    def test_pinned_active_still_forced(self, mock_dump, mock_discover):
        mock_discover.return_value = [
            {
                "name": "置顶已续",
                "streak_days": 8,
                "status": "active",
                "isPinned": True,
            },
            {
                "name": "普通已续",
                "streak_days": 8,
                "status": "active",
                "isPinned": False,
            },
            {"name": "需续", "streak_days": 3, "status": "need", "isPinned": False},
        ]
        mock_dump.return_value = "/tmp/shot.png"

        pending = discover_pending_friends(
            self.page, self.cfg, self.state, self.today, False, False
        )

        self.assertEqual(pending, ["置顶已续", "需续"])

    @patch("src.discovery.discover_friends_with_spark")
    @patch("src.discovery.dump_page")
    def test_pinned_sent_today_skipped(self, mock_dump, mock_discover):
        mock_discover.return_value = [
            {"name": "置顶", "streak_days": 0, "status": "pinned", "isPinned": True},
        ]
        mock_dump.return_value = "/tmp/shot.png"

        state = {"date": "2026-08-13", "sent": ["置顶"]}
        pending = discover_pending_friends(
            self.page, self.cfg, state, self.today, False, False
        )

        self.assertEqual(pending, [])

    @patch("src.discovery.page_render_state", return_value="content")
    @patch("src.discovery.discover_friends_with_spark")
    @patch("src.discovery.self_heal")
    @patch("src.discovery.dump_page")
    def test_heal_triggered_on_discovery_error(
        self, mock_dump, mock_heal, mock_discover, mock_render_state
    ):
        mock_discover.side_effect = SparkDiscoveryError("三策略全未命中")
        mock_dump.return_value = "/tmp/shot.png"
        mock_heal.return_value = True

        # After heal, retry should succeed
        mock_discover.side_effect = [
            SparkDiscoveryError("三策略全未命中"),
            [
                {
                    "name": "修复后好友",
                    "streak_days": 2,
                    "status": "need",
                    "isPinned": False,
                },
            ],
        ]

        with patch("src.discovery.wait_for_streaks") as mock_wait:
            mock_wait.return_value = True
            pending = discover_pending_friends(
                self.page, self.cfg, self.state, self.today, False, False
            )

        self.assertEqual(pending, ["修复后好友"])
        mock_heal.assert_called_once()

    @patch("src.discovery.page_render_state", return_value="content")
    @patch("src.discovery.discover_friends_with_spark")
    @patch("src.discovery.self_heal")
    @patch("src.discovery.dump_page")
    def test_heal_fails_raises_original(
        self, mock_dump, mock_heal, mock_discover, mock_render_state
    ):
        mock_discover.side_effect = SparkDiscoveryError("三策略全未命中")
        mock_dump.return_value = "/tmp/shot.png"
        mock_heal.return_value = False

        with self.assertRaises(SparkDiscoveryError):
            discover_pending_friends(
                self.page, self.cfg, self.state, self.today, False, False
            )

    @patch("src.discovery.page_render_state", return_value="content")
    @patch("src.discovery.discover_friends_with_spark")
    @patch("src.discovery.self_heal")
    @patch("src.discovery.dump_page")
    def test_heal_retry_failed_raises_healretryfailed(
        self, mock_dump, mock_heal, mock_discover, mock_render_state
    ):
        mock_discover.side_effect = SparkDiscoveryError("三策略全未命中")
        mock_dump.return_value = "/tmp/shot.png"
        mock_heal.return_value = True

        # Both retry attempts fail
        mock_discover.side_effect = [
            SparkDiscoveryError("三策略全未命中"),
            SparkDiscoveryError("重试仍失败"),
            SparkDiscoveryError("重试仍失败"),
        ]

        with patch("src.discovery.wait_for_streaks") as mock_wait:
            mock_wait.return_value = True
            with self.assertRaises(HealRetryFailed):
                discover_pending_friends(
                    self.page, self.cfg, self.state, self.today, False, False
                )

    @patch("src.discovery.discover_friends_with_spark")
    @patch("src.discovery.dump_page")
    def test_dry_run_mode(self, mock_dump, mock_discover):
        mock_discover.return_value = [
            {"name": "需续好友", "streak_days": 5, "status": "need", "isPinned": False},
        ]
        mock_dump.return_value = "/tmp/shot.png"

        pending = discover_pending_friends(
            self.page, self.cfg, self.state, self.today, False, True
        )

        self.assertEqual(pending, ["需续好友"])


class TestDiscoveryWarnOnce(unittest.TestCase):
    """会话19 遗留①: 「无火花标记」告警只在整轮发现结束后判定一次。

    _run_discover 每个滚动 pass 都会调用; 中间 pass 火花图标尚未渲染时不应告警。
    """

    def _run(self, page, discover_items):
        with (
            patch("src.spark_status._wait_for_streaks"),
            patch(
                "src.spark_status._run_discover", return_value=discover_items
            ) as mock_run,
            patch("src.spark_status.log") as mock_log,
        ):
            result = spark_status.discover_friends_with_spark(page)
        warns = [
            call for call in mock_log.call_args_list if "无任何火花标记" in str(call)
        ]
        return result, warns, mock_run

    def test_warn_emitted_once_for_whole_discovery(self):
        page = MagicMock()
        page.evaluate.return_value = 1  # 列表始终可滚动 → 跑多个 pass

        result, warns, mock_run = self._run(page, [])

        self.assertEqual(result, [])
        self.assertGreater(mock_run.call_count, 1, "应经过多个扫描 pass")
        self.assertEqual(len(warns), 1)

    def test_no_warn_when_friends_found(self):
        page = MagicMock()
        page.evaluate.return_value = 0
        item = {
            "name": "甲",
            "hasStreak": True,
            "iconSrc": "gray_x.png",
            "method": "class",
        }

        result, warns, _ = self._run(page, [item])

        self.assertEqual(len(result), 1)
        self.assertEqual(warns, [])


class TestHealRetryFailed(unittest.TestCase):
    def test_exception_message(self):
        exc = HealRetryFailed("自愈成功但本进程重试发现仍失败")
        self.assertEqual(str(exc), "自愈成功但本进程重试发现仍失败")


class TestRenderHangDetection(unittest.TestCase):
    """P3: body 全空时发现失败应抛 RenderHangDetected(不触发 AI 自愈)。"""

    def setUp(self):
        self.page = MagicMock()
        self.cfg = {}
        self.state = {"date": "d", "sent": []}

    @patch("src.discovery.page_render_state", return_value="blank")
    @patch("src.discovery.discover_friends_with_spark")
    def test_blank_raises_render_hang(self, mock_discover, _mstate):
        from src.discovery import RenderHangDetected

        mock_discover.side_effect = SparkDiscoveryError("全未命中")
        with self.assertRaises(RenderHangDetected):
            discover_pending_friends(self.page, self.cfg, self.state, "d", False, False)

    @patch("src.discovery.page_render_state", return_value="content")
    @patch("src.discovery.self_heal", return_value=False)
    @patch("src.discovery.dump_page", return_value="")
    @patch("src.discovery.discover_friends_with_spark")
    def test_content_still_triggers_heal(self, mock_discover, _mdump, mheal, _mstate):
        mock_discover.side_effect = SparkDiscoveryError("全未命中")
        with self.assertRaises(SparkDiscoveryError):
            discover_pending_friends(self.page, self.cfg, self.state, "d", False, False)
        mheal.assert_called_once()


if __name__ == "__main__":
    unittest.main()
