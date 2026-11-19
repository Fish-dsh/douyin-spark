"""tools/strat_stats.py 单测: 闸门漏斗与视觉兜底归因的解析契约。

这里锁定的是**日志字符串 ↔ 统计类别**的对应关系。strat_stats 是明早核账的唯一入口,
它自己瞎掉比热路径 bug 更隐蔽(2026-10-07 教训: 身份核对恒判「无法判定」连续 17 次,
日志里看得见却没人统计出来)。所以每条判据都要有正例和反例。
"""

import ast
import contextlib
import gzip
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_SPEC = importlib.util.spec_from_file_location(
    "strat_stats",
    Path(__file__).resolve().parent.parent / "tools" / "strat_stats.py",
)
strat_stats = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(strat_stats)

# 生产日志行前缀格式: [YYYY-MM-DD HH:MM:SS] [tag] ...
_TS = "[2026-10-08 00:12:34]"


def _parse(lines: list[str]) -> dict:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".log", encoding="utf-8", delete=False
    ) as fh:
        fh.write("\n".join(lines) + "\n")
        path = Path(fh.name)
    try:
        return strat_stats.parse([path])
    finally:
        path.unlink(missing_ok=True)


class TestGateFunnel(unittest.TestCase):
    def test_confirmed_counts_as_gate_verdict(self):
        """正例: 面板真读到并确认目标 → 计入闸门裁决。"""
        counts = _parse(
            [
                f"{_TS} [核对] 身份核对: 目标『测试好友』在当前会话面板确认",
            ]
        )
        self.assertEqual(counts[("2026-10-08", "核对/确认")], 1)
        self.assertEqual(counts[("2026-10-08", "核对/总核对数")], 1)

    def test_blind_pass_distinguishes_recheck(self):
        """反例(闸门失效形态): 读不到而放行要能区分「首读即盲」和「复检后仍盲」。

        昨晚 17/17 全是这个类别, 而当时没有任何一行统计把它喊出来。
        """
        counts = _parse(
            [
                f"{_TS} [核对] 身份核对: 面板未渲染或读不到文本, 无法判定, 放行",
                (
                    f"{_TS} [核对] 身份核对: 面板未渲染或读不到文本, 无法判定, "
                    "放行(已复检 2 次仍未渲染)"
                ),
            ]
        )
        self.assertEqual(counts[("2026-10-08", "核对/盲放行(首读即盲)")], 1)
        self.assertEqual(counts[("2026-10-08", "核对/盲放行(复检后)")], 1)
        self.assertEqual(counts[("2026-10-08", "核对/确认")], 0)
        self.assertEqual(counts[("2026-10-08", "核对/总核对数")], 2)

    def test_conflict_block_counts_and_names_recheck(self):
        """拦截必须单独成类: 非零就要去手机端逐个复核(误拦=漏发)。"""
        counts = _parse(
            [
                (
                    f"{_TS} [核对] 身份核对冲突: 当前会话面板未出现『测试好友』, "
                    "面板文本=『某人 / 23 / 00:35』, 复检后仍判冲突, 拦截发送"
                ),
            ]
        )
        self.assertEqual(counts[("2026-10-08", "核对/冲突拦截(复检后)")], 1)
        self.assertEqual(counts[("2026-10-08", "核对/冲突拦截(单次)")], 0)

    def test_identity_probe_failure_counts_as_gate_error(self):
        counts = _parse(
            [
                f"{_TS} [核对] 身份核对不可用(TimeoutError), 无法判定, 放行",
                f"{_TS} [核对] 身份核对返回异常, 无法判定, 放行",
            ]
        )
        self.assertEqual(counts[("2026-10-08", "核对/核对自身异常")], 2)

    def test_rescue_lines_are_counted_separately_not_as_verdicts(self):
        """面板挂载抢救行属于同一 [核对] 前缀, 但不能混进闸门裁决分母。"""
        counts = _parse(
            [
                (
                    f"{_TS} [核对] 可信点击未打开面板, 已向会话行合成点击 "
                    "{'x': 185, 'y': 361}, 再等 15s"
                ),
                f"{_TS} [核对] 可信点击未打开面板, 且会话行合成点击定位失败",
                f"{_TS} [核对] 身份核对: 目标『测试好友』在当前会话面板确认",
            ]
        )
        self.assertEqual(counts[("2026-10-08", "核对/需合成点击抢救")], 1)
        self.assertEqual(counts[("2026-10-08", "核对/抢救定位也失败")], 1)
        self.assertEqual(counts[("2026-10-08", "核对/总核对数")], 1)

    def test_unrelated_hexie_line_is_ignored(self):
        counts = _parse([f"{_TS} [核对] 覆盖对照: 待发送 6 人"])
        self.assertFalse(any(k.startswith("核对/") for _, k in counts))


class TestCvAttribution(unittest.TestCase):
    def test_funnel_numbers_and_detail_category(self):
        """used/found/clicked/verified 之外还要能按 detail= 归因(昨晚 clicked=0 的
        真实原因是身份复核主动放弃, 当时只能靠 grep 翻)。"""
        counts = _parse(
            [
                (
                    f"{_TS} [cv_fallback] used=1 found=1 clicked=0 verified=0 "
                    "detail=identity_conflict:身份核对冲突: 当前会话面板未出现『测试好友』"
                ),
                f"{_TS} [cv_fallback] used=1 found=0 clicked=0 verified=0 detail=not_found",
            ]
        )
        self.assertEqual(counts[("2026-10-08", "cv/used")], 2)
        self.assertEqual(counts[("2026-10-08", "cv/found")], 1)
        self.assertEqual(counts[("2026-10-08", "cv归因/identity_conflict")], 1)
        self.assertEqual(counts[("2026-10-08", "cv归因/not_found")], 1)
        # 漏斗行内嵌的 detail 文本不能被闸门分支二次计数(同一条事实只算一次)。
        self.assertEqual(counts[("2026-10-08", "核对/冲突拦截(复检后)")], 0)
        self.assertEqual(counts[("2026-10-08", "核对/总核对数")], 0)

    def test_verified_run_counts_as_won_session(self):
        counts = _parse(
            [
                (
                    f"{_TS} [cv_fallback] used=1 found=1 clicked=1 verified=1 "
                    "detail=cand=(755,541) conf=0.91"
                ),
            ]
        )
        self.assertEqual(counts[("2026-10-08", "发送/视觉兜底✓")], 1)
        self.assertEqual(counts[("2026-10-08", "发送/总会话数")], 1)
        self.assertEqual(counts[("2026-10-08", "cv归因/cand")], 1)


class TestFailureKinds(unittest.TestCase):
    def test_terminal_and_handoff_lines_are_classified(self):
        counts = _parse(
            [
                f"{_TS} [失败] 未能定位到好友『测试好友』, 跳过该好友",
                f"{_TS} [失败] 『测试好友』会话面板两次等待(15s+15s)均未挂载, 快速失败不盲发",
                f"{_TS} [失败] 『测试好友』发送前身份核对未通过, 跳过(不盲发)",
                f"{_TS} [失败] 『测试好友』表情发送失败, 以文字兜底『续火花』",
                f"{_TS} [失败] 『测试好友』发送未确认成功",
                f"{_TS} [失败] 『测试好友』视觉兜底也未成功: 视觉兜底: ROI 内未找到红色↑按钮",
            ]
        )
        for name in (
            "定位失败",
            "面板未挂载",
            "核对拦截跳过",
            "转文字兜底",
            "转视觉兜底",
            "视觉兜底失败",
        ):
            self.assertEqual(counts[("2026-10-08", f"失败/{name}")], 1, name)

    def test_unknown_fail_line_is_not_forced_into_a_bucket(self):
        """反例: 判不出类别就不计数, 宁可漏报也不把未知写进已知桶(假绿)。"""
        counts = _parse([f"{_TS} [失败] 『测试好友』某种全新失败形态"])
        self.assertFalse(any(k.startswith("失败/") for _, k in counts))


class TestExistingBehaviour(unittest.TestCase):
    def test_day_bucketing_and_send_layers_still_work(self):
        """回归: 新解析分支不能抢走既有 [定位]/[发送] 行的归类(它们在链路里排更前)。"""
        counts = _parse(
            [
                "[2026-10-07 00:10:00] [定位] 策略『get_by_text(exact)』命中并点击了『测试好友』(第1轮)",
                "[2026-10-07 00:10:05] [发送] 生效策略: 表情已发送并确认",
                f"{_TS} [核对] 身份核对: 目标『测试好友』在当前会话面板确认",
            ]
        )
        self.assertEqual(counts[("2026-10-07", "定位/get_by_text(exact)✓")], 1)
        self.assertEqual(counts[("2026-10-07", "发送/表情✓")], 1)
        self.assertEqual(counts[("2026-10-07", "发送/总会话数")], 1)
        self.assertEqual(counts[("2026-10-08", "核对/确认")], 1)


class TestRender(unittest.TestCase):
    """渲染层契约: 多天日志里每个类别只能出现一次。

    这个 bug 是拿真实历史日志(4 天)跑出来的: 类别名列表按 (day,key) 全体遍历, 于是
    「定位失败=13」在同一天里被印了 11 遍。单天样例测不出来, 所以这里用跨天夹具。
    """

    def _run(self, lines):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".log", encoding="utf-8", delete=False
        ) as fh:
            fh.write("\n".join(lines) + "\n")
            path = fh.name
        buf = io.StringIO()
        try:
            with (
                mock.patch.object(sys, "argv", ["strat_stats.py", str(path)]),
                contextlib.redirect_stdout(buf),
            ):
                rc = strat_stats.main()
        finally:
            Path(path).unlink(missing_ok=True)
        return rc, buf.getvalue()

    @staticmethod
    def _dup_tokens(text, marker):
        dups = []
        for line in text.splitlines():
            if marker not in line:
                continue
            tokens = [t.split("=")[0] for t in line.split(marker, 1)[1].split()]
            if len(set(tokens)) != len(tokens):
                dups.append(line)
        return dups

    def test_failure_line_has_no_repeated_categories(self):
        rc, text = self._run(
            [
                "[2026-10-06 00:05:00] [失败] 『测试好友』发送未确认成功",
                "[2026-10-07 00:05:00] [失败] 『测试好友』发送未确认成功",
                "[2026-10-07 00:06:00] [失败] 未能定位到好友『测试好友』, 跳过该好友",
                "[2026-10-08 00:05:00] [失败] 『测试好友』视觉兜底也未成功: x",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(self._dup_tokens(text, "失败/交接行"), [], text)
        self.assertIn("转视觉兜底=1", text)

    def test_gate_table_lists_each_verdict_once(self):
        rc, text = self._run(
            [
                "[2026-10-06 00:05:00] [核对] 身份核对: 目标『测试好友』在当前会话面板确认",
                "[2026-10-07 00:05:00] [核对] 身份核对: 目标『测试好友』在当前会话面板确认",
                "[2026-10-07 00:05:01] [核对] 身份核对: 面板未渲染或读不到文本, 无法判定, 放行",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(self._dup_tokens(text, "身份闸门"), [], text)
        rows = [ln for ln in text.splitlines() if ln.strip().startswith("确认")]
        self.assertEqual(len(rows), 2, f"确认行应按天各一条, 实得 {len(rows)}: {text}")


class TestLogStringContract(unittest.TestCase):
    """契约测试: 源码里真实存在的 [核对]/[失败]/身份核对 文案, 必须都能被分类。

    为什么需要它: 本工具的判定靠**字符串前缀**, 而热路径的文案随时会被改写。上一轮的
    教训就是「闸门恒判无法判定, 日志里看得见但没人统计」; 如果连统计器都对不上文案,
    明早就会得到一份「一切正常」的空表(假绿)。改文案而不改这里 = 测试红。
    """

    REPO = Path(__file__).resolve().parent.parent

    @staticmethod
    def _flatten(node):
        """f-string / 常量串还原成样例文本, 插值位置填 'X'。"""
        if isinstance(node, ast.JoinedStr):
            out = []
            for part in node.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    out.append(part.value)
                else:
                    out.append("X")
            return "".join(out)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return None

    @classmethod
    def _walk_strings(cls, node, skip):
        """产出源码里出现的字符串文本; f-string 当作**整体**叶子(不再拆出其中的常量段),
        否则 '[失败] 『' 这种半截前缀会被误当成一条独立文案。skip 里的节点是 docstring。"""
        if id(node) in skip:
            return
        if isinstance(node, ast.JoinedStr):
            yield cls._flatten(node)
            return
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value
            return
        for child in ast.iter_child_nodes(node):
            yield from cls._walk_strings(child, skip)

    @staticmethod
    def _docstring_ids(tree):
        ids = set()
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            # Lambda.body 是单个表达式而不是语句列表, 必须先确认是 list
            if (
                isinstance(body, list)
                and body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
            ):
                ids.add(id(body[0].value))
        return ids

    @classmethod
    def _strings(cls, rel, prefixes):
        tree = ast.parse((cls.REPO / rel).read_text(encoding="utf-8"))
        skip = cls._docstring_ids(tree)
        found = set()
        for text in cls._walk_strings(tree, skip):
            if text and any(text.startswith(p) for p in prefixes):
                found.add(text)
        return sorted(found)

    def test_every_identity_string_in_sender_is_classified(self):
        texts = self._strings("src/sender.py", ("身份核对",))
        # 实测数量下限: 确认/冲突/未渲染/不可用/返回异常 至少 5 条独立文案
        self.assertGreaterEqual(len(texts), 5, f"源码里身份核对文案数异常少: {texts}")
        unclassified = [t for t in texts if strat_stats._classify_gate(t) is None]
        self.assertEqual(
            unclassified, [], "新增/改写的身份核对文案未被 strat_stats 归类"
        )

    def test_every_hexie_log_in_scheduler_is_classified(self):
        texts = self._strings("src/scheduler.py", ("[核对] ",))
        self.assertGreaterEqual(len(texts), 3, f"源码里 [核对] 文案数异常少: {texts}")
        bad = []
        for t in texts:
            body = t[len("[核对] ") :]
            if body.replace("X", "").strip() == "":
                continue  # 纯转发 sender 文案(如 [核对] {id_detail}), 由 sender 契约覆盖
            if strat_stats._classify_gate(body) is None:
                bad.append(t)
        self.assertEqual(bad, [], "新增/改写的 [核对] 日志未被 strat_stats 归类")

    def test_every_shibai_log_in_scheduler_is_classified(self):
        texts = self._strings("src/scheduler.py", ("[失败] ",))
        self.assertGreaterEqual(len(texts), 6, f"源码里 [失败] 文案数异常少: {texts}")
        bad = []
        for t in texts:
            body = t[len("[失败] ") :]
            if body.replace("X", "").strip() == "":
                continue
            if strat_stats._classify_fail(body) is None:
                bad.append(t)
        self.assertEqual(bad, [], "新增/改写的 [失败] 日志未被 strat_stats 归类")

    def test_every_confirm_detail_in_sender_is_classified(self):
        """confirm_send 的**返回值文案**就是 strat_stats 的判据来源(与 [核对] 同一族契约)。

        2026-10-07 新增的「硬证据/降级」两条成色必须分别落桶: 谁改了措辞而没改统计,
        当晚的确认表就会把降级确认算成硬确认(=又一份「一切正常」的空表)。
        """
        texts = self._strings(
            "src/sender.py", ("确认成功", "未能确认", "确认发送时出错")
        )
        self.assertGreaterEqual(len(texts), 4, f"源码里确认文案数异常少: {texts}")
        bad = [t for t in texts if strat_stats._classify_confirm(t) is None]
        self.assertEqual(bad, [], "新增/改写的发送确认文案未被 strat_stats 归类")
        # 成色分开: 硬确认与降级确认不能同桶
        self.assertEqual(
            {
                strat_stats._classify_confirm(t)
                for t in texts
                if t.startswith(("确认成功", "未能确认", "确认发送时出错"))
            }
            & {"硬证据确认", "降级确认(证据不可读)", "未能确认", "确认异常"},
            {"硬证据确认", "降级确认(证据不可读)", "未能确认", "确认异常"},
            "确认表少了某一组成色",
        )

    def test_every_queren_log_in_scheduler_is_classified(self):
        texts = self._strings("src/scheduler.py", ("[确认] ",))
        self.assertGreaterEqual(len(texts), 2, f"源码里 [确认] 文案数异常少: {texts}")
        bad = []
        for t in texts:
            body = t[len("[确认] ") :]
            if body.replace("X", "").strip() == "":
                continue  # 纯转发 confirm_send 的 detail, 由上一条契约覆盖
            if strat_stats._classify_confirm(body) is None:
                bad.append(t)
        self.assertEqual(bad, [], "新增/改写的 [确认] 日志未被 strat_stats 归类")


class TestHistoryFiles(unittest.TestCase):
    """轮转出的历史文件必须读得到: 留 40 天却不认 .gz, 等于没留。"""

    _LINE = "[核对] 身份核对: 目标『测试好友』在当前会话面板确认"

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "runtime").mkdir()

    def tearDown(self):
        shutil.rmtree(self.root)

    def _write(self, name: str, lines: list[str], gz: bool = False) -> Path:
        path = self.root / "runtime" / name
        text = "\n".join(lines) + "\n"
        if gz:
            with gzip.open(path, "wt", encoding="utf-8") as fh:
                fh.write(text)
        else:
            path.write_text(text, encoding="utf-8")
        return path

    def test_gz_file_parses_like_plain(self):
        plain = self._write("spark.log.9", [f"{_TS} {self._LINE}"])
        packed = self._write("spark.log.10.gz", [f"{_TS} {self._LINE}"], gz=True)
        self.assertEqual(
            strat_stats.parse([packed]),
            strat_stats.parse([plain]),
            ".gz 解析结果不一致",
        )

    def test_all_reads_rotated_gz_and_skips_manual_backup(self):
        """--all: 当前文件 + 轮转文件(含 .gz)一起进统计, 手工备份与测试日志排除。"""
        cur = self._write("spark.log", [f"[2026-10-08 00:12:34] {self._LINE}"])
        old = self._write(
            "spark.log.1.gz", [f"[2026-10-07 00:12:34] {self._LINE}"], gz=True
        )
        # 轮转编号越大越旧, --all 靠 mtime 排序; 夹具显式定序, 不吃创建速度
        os.utime(old, (1_700_000_000, 1_700_000_000))
        os.utime(cur, (1_700_000_600, 1_700_000_600))
        self._write("spark.log.pre-sync-20260927", ["[2026-01-01 00:00:00] 不该被读"])
        self._write("spark.test.log", ["[2026-01-01 00:00:00] 不该被读"])
        buf = io.StringIO()
        with (
            mock.patch.object(strat_stats, "REPO", self.root),
            mock.patch.object(sys, "argv", ["strat_stats", "--all"]),
            contextlib.redirect_stdout(buf),
        ):
            rc = strat_stats.main()
        out = buf.getvalue()
        self.assertEqual(rc, 0, out)
        self.assertIn("2026-10-08", out)
        self.assertIn("2026-10-07", out)
        self.assertNotIn("2026-01-01", out)


if __name__ == "__main__":
    unittest.main()
