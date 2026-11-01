"""性能档位单测(2026-11-01): 三档表自洽 + low 档锁死历史生产值。

为什么 low 列要逐项卡住: 切到 perf 后板端不能有任何行为变化 —— low 列必须逐项
等于 2026-11-01 之前散落在各模块里的字面量。任何人不小心改了 low 列(哪怕是「顺手
调快点」), 这里立刻红, 而不是等某个晚上在板子上假发送。
"""

import ast
import os
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

from src import perf


class TestProfiles(unittest.TestCase):
    def setUp(self):
        os.environ.pop("SPARK_PERF_PROFILE", None)
        perf.set_profile("low")

    def tearDown(self):
        os.environ.pop("SPARK_PERF_PROFILE", None)
        perf.set_profile("low")

    def test_three_tiers(self):
        self.assertEqual(perf.PROFILES, ("low", "mid", "high"))
        self.assertEqual(perf.DEFAULT_PROFILE, "low")

    def test_normalize(self):
        self.assertEqual(perf.normalize(" High "), "high")
        self.assertEqual(perf.normalize("MID"), "mid")
        self.assertEqual(perf.normalize("turbo"), "low")
        self.assertEqual(perf.normalize(None), "low")
        self.assertEqual(perf.normalize(""), "low")

    def test_set_profile_returns_normalized(self):
        self.assertEqual(perf.set_profile(" HIGH "), "high")
        self.assertEqual(perf.active_profile(), "high")

    def test_env_profile_wins(self):
        perf.set_profile("low")
        os.environ["SPARK_PERF_PROFILE"] = "high"
        try:
            self.assertEqual(perf.active_profile(), "high")
        finally:
            del os.environ["SPARK_PERF_PROFILE"]

    def test_unknown_knob_raises(self):
        with self.assertRaises(KeyError):
            perf.ms("no_such_knob")


class TestTableIntegrity(unittest.TestCase):
    """三档必须覆盖同一批 knob, 且逐档单调(机器越快, 等待越短)。"""

    def test_all_tiers_same_knobs(self):
        names = set(perf._SPECS["low"])
        for name in perf.PROFILES:
            self.assertEqual(set(perf._SPECS[name]), names, name)

    def test_bounds_cover_every_knob(self):
        self.assertEqual(set(perf._BOUNDS), set(perf.knobs()))

    def test_every_value_within_bounds(self):
        for name in perf.PROFILES:
            for knob, (lo, hi) in perf._BOUNDS.items():
                value = perf._SPECS[name][knob]
                self.assertGreaterEqual(value, lo, f"{name}.{knob}")
                self.assertLessEqual(value, hi, f"{name}.{knob}")

    def test_monotonic_low_ge_mid_ge_high(self):
        """等待类 knob: low >= mid >= high(机器越强等得越短)。"""
        # skip: 慢在抖音侧/不可逆代价的项, 三档统一, 不参与「越短越好」的档差判定。
        skip = {
            "goto_ms",
            "goto_commit_ms",
            "page_dom_ms",
            "reload_ms",
            "list_budget_ms",
            "login_state_deadline_ms",
        }
        for knob in perf.knobs():
            lo, mid, hi = (int(perf._SPECS[p][knob]) for p in perf.PROFILES)
            self.assertGreaterEqual(lo, mid, knob)
            self.assertGreaterEqual(mid, hi, knob)
            if knob not in skip:
                self.assertLessEqual(lo, mid * 2, f"{knob} 档差过大")
        for p in perf.PROFILES:
            a, b = perf._SPECS[p]["send_gap_ms"]
            self.assertLessEqual(a, b, p)


class TestUnifiedAcrossProfiles(unittest.TestCase):
    """「慢在抖音侧」与「不可逆代价」的项必须三档统一, 不许跟档位缩。

    这两项收窄不省时间, 只换来漏发/锁死当晚:
      list_budget_ms        —— 列表没填充完就开扫 → 扫半张 → 静默漏发
      login_state_deadline_ms —— 误判未登录 → 写 last_fail → 3600s 退避
    """

    UNIFIED: ClassVar[dict] = {
        "list_budget_ms": 150000,
        "login_state_deadline_ms": 45000,
    }

    def test_values_identical_in_all_profiles(self):
        for knob, expected in self.UNIFIED.items():
            for name in perf.PROFILES:
                self.assertEqual(perf._SPECS[name][knob], expected, f"{name}.{knob}")

    def test_list_budget_floor_is_enforced_by_bounds(self):
        """_BOUNDS 下限不许低于板端实测最慢 75s 的安全垫。"""
        lo, _ = perf._BOUNDS["list_budget_ms"]
        self.assertGreaterEqual(lo, 120000)

    def test_login_state_deadline_floor_is_enforced_by_bounds(self):
        lo, _ = perf._BOUNDS["login_state_deadline_ms"]
        self.assertGreaterEqual(lo, 30000)


class TestSrcActuallyConsumesPerf(unittest.TestCase):
    """锁「src 有没有在用 perf」, 而不只是「表里的数字对不对」。

    2026-11-01 审计教训: 把 perf.ms("x") 换回硬编码字面量时, 原有用例全绿 ——
    表本身没变, 变的是 src 不再读表。这里用 AST 扫源码兜住。
    """

    SRC = Path(__file__).resolve().parent.parent / "src"

    @staticmethod
    def _call_names(tree):
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Attribute):
                    yield f.attr, node
            elif isinstance(node, ast.Attribute):
                yield node.attr, node

    def _perf_knob_reads(self):
        """src/ 里所有 perf.<fn>("knob") 的 knob 名集合(fn ∈ ms/sec/get)。"""
        reads = set()
        for path in self.SRC.glob("*.py"):
            if path.name == "perf.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                if not (
                    isinstance(f, ast.Attribute)
                    and f.attr in ("ms", "sec", "get")
                    and isinstance(f.value, ast.Name)
                    and f.value.id == "perf"
                ):
                    continue
                if node.args and isinstance(node.args[0], ast.Constant):
                    reads.add(node.args[0].value)
        return reads

    def test_every_knob_is_read_by_src(self):
        """没有任何 knob 可以只躺在表里骗人(死配置 = 以为改得动其实改不动)。"""
        reads = self._perf_knob_reads()
        # send_gap_ms 走 perf.send_gap_range_sec(), 不是整型 knob
        unused = set(perf.knobs()) - reads
        self.assertEqual(unused, set(), f"这些 knob 在 src/ 无人读取: {sorted(unused)}")

    def test_no_bare_wait_for_timeout_literals(self):
        """src/ 不许出现裸 wait_for_timeout(<数字>), 必须走 perf。"""
        found = {}
        for path in self.SRC.glob("*.py"):
            if path.name == "perf.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                if not (isinstance(f, ast.Attribute) and f.attr == "wait_for_timeout"):
                    continue
                arg = node.args[0] if node.args else None
                if isinstance(arg, ast.Constant) and isinstance(
                    arg.value, (int, float)
                ):
                    found.setdefault(path.name, []).append((node.lineno, arg.value))
        self.assertEqual(found, {}, f"裸 wait_for_timeout 字面量: {found}")

    def test_no_bare_numeric_timeout_kwarg(self):
        """src/ 的 timeout=<数字> 只允许出现在 health_check 的子进程调用里。"""
        allowed = {"health_check.py"}
        found = {}
        for path in self.SRC.glob("*.py"):
            if path.name == "perf.py" or path.name in allowed:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for kw in node.keywords:
                    if (
                        kw.arg == "timeout"
                        and isinstance(kw.value, ast.Constant)
                        and isinstance(kw.value.value, (int, float))
                    ):
                        found.setdefault(path.name, []).append(
                            (node.lineno, kw.value.value)
                        )
        self.assertEqual(found, {}, f"裸 timeout= 字面量: {found}")


class TestLowLocksProductionLiterals(unittest.TestCase):
    """low 列 = 2026-11-01 之前的生产字面量。改动任一值都意味着板端行为变了。"""

    LEGACY: ClassVar[dict] = {
        "panel_open_ms": 6000,  # sender 原 12 × 500ms
        "panel_poll_ms": 500,
        "panel_items_ms": 5000,
        "panel_wait_ms": 2500,  # scheduler SPARK_PANEL_WAIT_MS 默认
        "contenteditable_ms": 15000,
        "contenteditable_retry_ms": 8000,
        "click_small_ms": 2000,
        "click_friend_rounds": 4,
        "click_friend_round_ms": 2500,
        "friend_attach_ms": 3000,
        "friend_click_ms": 4000,
        "input_click_ms": 3000,
        "btn_visible_ms": 1500,
        "goto_ms": 60000,
        "goto_commit_ms": 120000,
        "page_dom_ms": 30000,
        "reload_ms": 20000,
        "url_stable_ms": 2500,
        "login_panel_ms": 8000,
        "login_tab_deadline_ms": 60000,
        "settle_ms": 800,
        "ui_quiet_ms": 700,
        "escape_settle_ms": 300,
        "type_settle_ms": 200,
        "btn_ready_ms": 600,
        "short_settle_ms": 500,
        "scroll_settle_ms": 600,  # 原 spark_status 裸 600ms
        "post_reload_settle_ms": 1000,
        "heal_settle_ms": 2000,
        "login_step_wait_ms": 2000,
        "confirm_first_ms": 300,
        "confirm_poll_ms": 1000,
        "tail_first_ms": 250,
        "tail_poll_ms": 500,
        "identity_retry_ms": 4000,
        "login_content_ms": 12000,  # 原 24 × 500ms
        "login_poll_ms": 500,
        "pwd_field_poll_ms": 1000,  # 原 15 × 1000ms
        "pwd_field_budget_ms": 15000,  # 原 login 里 15000 // _pwd_poll 的裸分母
        "login_state_poll_ms": 1500,
        "login_state_deadline_ms": 45000,  # 三档统一, 不随档位缩
        "manual_poll_ms": 3000,
        "selector_wait_ms": 1500,  # finder.find 原默认
        "list_budget_ms": 150000,  # 原 30 × 5000ms; 三档统一, 不随档位缩
        "list_step_ms": 250,
        "streak_wait_ms": 20000,
        "streak_poll_ms": 1000,  # 原 spark_status 裸 1000ms
        "streak_scan_ms": 60000,
        "vision_budget_ms": 45000,  # vision 原 _BUDGET_SEC
        "vision_http_ms": 20000,  # vision 原 _HTTP_TIMEOUT_SEC
        "screenshot_ms": 15000,
    }

    def test_low_equals_legacy(self):
        low = perf._SPECS["low"]
        for knob, value in self.LEGACY.items():
            self.assertEqual(low[knob], value, knob)
        self.assertEqual(low["send_gap_ms"], (500, 1200))  # 原 _SEND_GAP_RANGE 秒值
        self.assertEqual(set(low), set(self.LEGACY) | {"send_gap_ms"})

    def test_low_matches_runtime_get(self):
        old = perf.active_profile()
        try:
            perf.set_profile("low")
            for knob, value in self.LEGACY.items():
                self.assertEqual(perf.ms(knob), value, knob)
        finally:
            perf.set_profile(old)


class TestOverrides(unittest.TestCase):
    def setUp(self):
        perf.set_profile("low")

    def tearDown(self):
        for key in list(os.environ):
            if key.startswith("SPARK_PERF_"):
                del os.environ[key]
        perf.set_profile("low")

    def test_env_overrides_knob(self):
        os.environ["SPARK_PERF_PANEL_OPEN_MS"] = "1234"
        self.assertEqual(perf.ms("panel_open_ms"), 1234)

    def test_out_of_range_falls_back(self):
        os.environ["SPARK_PERF_PANEL_OPEN_MS"] = "999999"
        self.assertEqual(perf.ms("panel_open_ms"), 6000)
        os.environ["SPARK_PERF_PANEL_OPEN_MS"] = "abc"
        self.assertEqual(perf.ms("panel_open_ms"), 6000)

    def test_send_gap_override(self):
        self.assertEqual(perf.send_gap_range_sec(), (0.5, 1.2))
        os.environ["SPARK_PERF_SEND_GAP_MS"] = "200,900"
        self.assertEqual(perf.send_gap_range_sec(), (0.2, 0.9))
        os.environ["SPARK_PERF_SEND_GAP_MS"] = "900,200"  # lo > hi 非法
        self.assertEqual(perf.send_gap_range_sec(), (0.5, 1.2))

    def test_sec_converts(self):
        self.assertEqual(perf.sec("vision_budget_ms"), 45.0)
        perf.set_profile("high")
        self.assertEqual(perf.sec("vision_budget_ms"), 20.0)


class TestDescribe(unittest.TestCase):
    def test_describe_mentions_profile_and_gap(self):
        perf.set_profile("high")
        text = perf.describe()
        self.assertIn("perf_profile=high", text)
        self.assertIn("300-800ms", text)

    def test_summary_rows_shape(self):
        rows = perf.summary_rows()
        # knobs() 不含 send_gap_ms, summary_rows() 会额外带上它
        self.assertEqual(len(rows), len(perf.knobs()) + 1)
        self.assertTrue(all(len(r) == 4 for r in rows))


class TestConfigAppliesProfile(unittest.TestCase):
    """load_config() 必须把 perf_profile 真正落到 perf 模块(不只是留在 dict 里)。"""

    def test_load_config_sets_perf_profile(self):
        import json
        import tempfile
        from pathlib import Path

        from src.config import load_config

        old = perf.active_profile()
        try:
            with tempfile.TemporaryDirectory() as td:
                p = Path(td) / "config.json"
                p.write_text(
                    json.dumps({"send_window": {}, "perf_profile": "high"}),
                    encoding="utf-8",
                )
                with mock.patch("src.config.CONFIG_FILE", p):
                    load_config()
            self.assertEqual(perf.active_profile(), "high")
        finally:
            perf.set_profile(old)


class TestFriendSleepBudget(unittest.TestCase):
    """friend_sleep_ms 是「档位到底省了多少」的唯一可判定口径。"""

    def test_low_matches_legacy_literal_sum(self):
        """low 必须等于 2026-11-01 前的原有 sleep 之和(逐项复原, 不许漂移)。"""
        legacy = 200 * 1 + 700 * 2 + 300 * 2 + 600 * 1 + 300 * 1 + 250 * 1
        self.assertEqual(perf.friend_sleep_ms("low"), legacy)

    def test_monotonic_across_profiles(self):
        low, mid, high = (perf.friend_sleep_ms(p) for p in perf.PROFILES)
        self.assertGreater(low, mid)
        self.assertGreater(mid, high)

    def test_high_saves_about_60_percent_of_fixed_sleep(self):
        ratio = perf.friend_sleep_ms("high") / perf.friend_sleep_ms("low")
        self.assertLess(ratio, 0.45)
        self.assertGreater(ratio, 0.30)

    def test_only_counts_unconditional_sleeps(self):
        """超时天花板(如 contenteditable_ms)不参与, 否则会虚报省下的时间。"""
        self.assertNotIn("contenteditable_ms", perf._SLEEP_KEYS)
        self.assertNotIn("panel_open_ms", perf._SLEEP_KEYS)
        self.assertNotIn("list_budget_ms", perf._SLEEP_KEYS)

    def test_sleep_keys_all_exist_in_specs(self):
        for k in perf._SLEEP_KEYS:
            self.assertIn(k, perf.knobs())
            self.assertIn(k, perf._SLEEP_REPEAT)

    def test_describe_reports_single_friend_budget(self):
        perf.set_profile("high")
        text = perf.describe()
        self.assertIn("单好友固定等待", text)
        self.assertIn("perf_profile=high", text)


if __name__ == "__main__":
    unittest.main()
