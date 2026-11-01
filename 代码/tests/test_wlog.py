"""tools/wlog.py 单测: 在临时 git 仓里验证记档守卫的三类行为(不触碰真实 WORKLOG)。"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "wlog", Path(__file__).resolve().parent.parent / "tools" / "wlog.py"
)
wlog = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(wlog)

_SKELETON = """# WORKLOG.md · 项目工作日志

## 会话时间线

- **2026-10-01 · 会话 32（示例）**：① 已记档的内容。
"""


class TestIsStatePath(unittest.TestCase):
    def test_state_files(self):
        for p in ("src/scheduler.py", "spark.py", "requirements.txt", "run_cron.sh"):
            self.assertTrue(wlog.is_state_path(p), p)

    def test_non_state_files(self):
        for p in ("WORKLOG.md", "README.md", "docs/deployment.md", "AGENTS.md"):
            self.assertFalse(wlog.is_state_path(p), p)


class TempRepoCase(unittest.TestCase):
    """建一个一次性 git 仓, 并把 wlog 的 REPO_DIR/WORKLOG 指过去。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.worklog = self.root / "WORKLOG.md"
        self.worklog.write_text(_SKELETON, encoding="utf-8")
        self._old = (wlog.REPO_DIR, wlog.WORKLOG)
        wlog.REPO_DIR = self.root
        wlog.WORKLOG = self.worklog
        self._git("init", "-q")
        self._git("config", "user.email", "t@example.com")
        self._git("config", "user.name", "t")

    def tearDown(self):
        wlog.REPO_DIR, wlog.WORKLOG = self._old
        self.tmp.cleanup()

    def _git(self, *args, date="2026-10-01T12:00:00"):
        env = {
            **os.environ,
            "GIT_AUTHOR_DATE": date,
            "GIT_COMMITTER_DATE": date,
        }
        proc = subprocess.run(
            ["git", *args],
            cwd=str(self.root),
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def _commit(self, name: str, subject: str, date="2026-10-01T12:00:00") -> str:
        (self.root / name).parent.mkdir(parents=True, exist_ok=True)
        (self.root / name).write_text(f"# {name}\n", encoding="utf-8")
        self._git("add", name)
        self._git(
            "-c", "commit.gpgsign=false", "commit", "-q", "-m", subject, date=date
        )
        return self._git("rev-parse", "--short", "HEAD")

    def _commit_all(self, subject: str, date="2026-10-01T12:00:00") -> str:
        self._git("add", "-A")
        self._git(
            "-c", "commit.gpgsign=false", "commit", "-q", "-m", subject, date=date
        )
        return self._git("rev-parse", "--short", "HEAD")


class TestAppendToSession(TempRepoCase):
    def test_appends_to_matching_session(self):
        msg = wlog.append_to_session("② 新追加。", "32")
        self.assertIn("会话 32", msg)
        self.assertIn("② 新追加。", self.worklog.read_text(encoding="utf-8"))

    def test_missing_anchor_is_error(self):
        with self.assertRaises(ValueError):
            wlog.append_to_session("x", "99")

    def test_ambiguous_anchor_is_error(self):
        text = self.worklog.read_text(encoding="utf-8")
        # 同一会话号出现两条(重号/误复制)时宁可不动, 也不猜要追加哪条
        self.worklog.write_text(text + text[text.index("- **") :], encoding="utf-8")
        with self.assertRaises(ValueError):
            wlog.append_to_session("x", "32")

    def test_dry_run_does_not_write(self):
        before = self.worklog.read_text(encoding="utf-8")
        wlog.append_to_session("② 不该落盘。", "32", dry_run=True)
        self.assertEqual(before, self.worklog.read_text(encoding="utf-8"))


class TestInsertStub(TempRepoCase):
    def test_stub_lands_above_oldest_and_is_idempotent(self):
        sha = self._commit("src/x.py", "fix: 改了点东西")
        msg = wlog.insert_stub(sha, "fix: 改了点东西", "2026-10-01")
        self.assertIn("已留 stub", msg)
        lines = self.worklog.read_text(encoding="utf-8").split("\n")
        head = lines.index("## 会话时间线")
        self.assertIn(sha, lines[head + 2])  # 空行后第一条即最新欠债
        again = wlog.insert_stub(sha, "fix: 改了点东西", "2026-10-01")
        self.assertIn("无需留痕", again)
        text = self.worklog.read_text(encoding="utf-8")
        self.assertEqual(text.count("(auto-stub)"), 1, "重复留痕会刷脏日志")


class TestNewEntry(TempRepoCase):
    def _add_older_entry(self):
        text = self.worklog.read_text(encoding="utf-8").rstrip("\n")
        self.worklog.write_text(
            text + "\n- **2026-09-26 · 会话 27（示例）**：更早的条目。\n",
            encoding="utf-8",
        )

    def test_backfill_lands_between_days(self):
        """补记落在该日应有的位置: 不打乱既有顺序, 只在中间插一行。"""
        self._add_older_entry()
        before = self.worklog.read_text(encoding="utf-8").split("\n")
        msg = wlog.new_entry("① 补记内容。", "2026-09-27", "会话 27b（补记）")
        self.assertIn("已新起条目", msg)
        after = self.worklog.read_text(encoding="utf-8").split("\n")
        new_lines = [j for j, l in enumerate(after) if l not in before]
        self.assertEqual(len(new_lines), 1, "只许插入一行")
        k = new_lines[0]
        self.assertIn("会话 27b", after[k])
        self.assertEqual(after[:k] + after[k + 1 :], before, "除新增行外不得改动任何行")
        self.assertIn("会话 32", after[k - 1])
        self.assertIn("会话 27（", after[k + 1])

    def test_date_not_older_than_newest_lands_on_top(self):
        self._add_older_entry()
        wlog.new_entry("① 今日新会话。", "2026-10-01", "会话 33")
        lines = self.worklog.read_text(encoding="utf-8").split("\n")
        self.assertIn("会话 33", lines[lines.index("## 会话时间线") + 2])

    def test_bad_date_is_refused(self):
        """日期写错格式 = 该天仍然没有条目, check 会继续报欠账, 所以必须当场拒。"""
        with self.assertRaises(ValueError):
            wlog.new_entry("x", "2026-9-27", "会话 27b")
        with self.assertRaises(ValueError):
            wlog.new_entry("x", "27th Sep", "会话 27b")


class TestUnrecorded(TempRepoCase):
    def test_day_granularity_and_strict(self):
        # 会话 32 已记 2026-10-01 → 当天状态提交按天判不算欠账
        self._commit("src/a.py", "fix: 当天已记档的改动", date="2026-10-01T12:00:00")
        # 另一天(无条目)的状态提交 = 真欠账
        sha_late = self._commit(
            "src/b.py", "refactor: 漏记的引擎迁移", date="2026-10-02T12:00:00"
        )
        todo, _heal = wlog.unrecorded_commits()
        self.assertEqual([e["sha"] for e in todo], [sha_late])
        strict, _ = wlog.unrecorded_commits(strict=True)
        self.assertIn(sha_late, [e["sha"] for e in strict])
        self.assertEqual(len(strict), 2, "--strict 应把两个未引用的状态提交都列出")

    def test_ignores_docs_only_and_heal_commits(self):
        (self.root / "docs").mkdir()
        self._commit("docs/x.md", "docs: 只改文档", date="2026-10-03T12:00:00")
        self._commit("src/y.py", "heal: 修复前基线 abc", date="2026-10-03T12:00:00")
        todo, heal = wlog.unrecorded_commits()
        self.assertEqual(todo, [])
        self.assertEqual(len(heal), 1)

    def test_stub_then_check_clean(self):
        """闭环: stub 留痕后, --strict 不再报该 commit(欠债行即算已记)。"""
        sha = self._commit(
            "requirements.txt", "refactor: 换引擎", date="2026-10-02T12:00:00"
        )
        self.assertIn(sha, [e["sha"] for e in wlog.unrecorded_commits(strict=True)[0]])
        wlog.insert_stub(sha, "refactor: 换引擎", "2026-10-02")
        self.assertEqual(wlog.unrecorded_commits(strict=True)[0], [])

    def test_strict_skips_commit_that_records_itself(self):
        """--strict 的理想姿势: 代码与记档同一提交不是欠账; 只改代码的那笔仍必须报。"""
        (self.root / "src").mkdir()
        (self.root / "src" / "a.py").write_text("# a\n", encoding="utf-8")
        self.worklog.write_text(
            _SKELETON.replace("① 已记档的内容。", "① 已记档 src/a.py 的改动。"),
            encoding="utf-8",
        )
        self._commit_all("fix: 代码与 WORKLOG 同提交", date="2026-10-02T12:00:00")
        self.assertEqual(wlog.unrecorded_commits(strict=True)[0], [])
        debit = self._commit(
            "src/b.py", "fix: 同天只改代码", date="2026-10-02T13:00:00"
        )
        todo, _ = wlog.unrecorded_commits(strict=True)
        self.assertEqual([e["sha"] for e in todo], [debit])

    def test_after_narrows_scope_to_baseline(self):
        """--after REV: 只核对守卫装好之后的历史, 基线及其之前的旧账另有账。"""
        self._commit("src/old.py", "fix: 基线之前", date="2026-09-30T10:00:00")
        base = self._commit(
            "src/base.py", "feat(wlog): 守卫装好", date="2026-10-01T10:00:00"
        )
        debit = self._commit(
            "src/new.py", "fix: 基线之后漏账", date="2026-10-02T10:00:00"
        )
        self.assertEqual(len(wlog.unrecorded_commits(strict=True)[0]), 3)
        todo, _ = wlog.unrecorded_commits(strict=True, after=base)
        self.assertEqual([e["sha"] for e in todo], [debit])


class TestCli(TempRepoCase):
    def _run(self, argv):
        """CLI 测试会 print, 吞掉以免污染单测输出(看起来像动了真仓)。"""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            return wlog.main(argv)

    def test_stub_skips_commit_that_already_touches_worklog(self):
        """代码与记档同提交(理想姿势): 不该再被留 stub。"""
        (self.root / "src").mkdir()
        (self.root / "src" / "a.py").write_text("# a\n", encoding="utf-8")
        self.worklog.write_text(
            _SKELETON.replace("① 已记档的内容。", "① 已记档 src/a.py。"),
            encoding="utf-8",
        )
        sha = self._commit_all("fix: 代码与 WORKLOG 同提交", date="2026-10-02T12:00:00")
        self.assertEqual(self._run(["stub", "--rev", sha]), 0)
        self.assertNotIn("(auto-stub)", self.worklog.read_text(encoding="utf-8"))

    def test_stub_writes_for_state_only_commit(self):
        sha = self._commit("src/b.py", "refactor: 只改代码", date="2026-10-02T12:00:00")
        self.assertEqual(self._run(["stub", "--rev", sha]), 0)
        self.assertIn(sha, self.worklog.read_text(encoding="utf-8"))

    def test_add_from_text_file(self):
        """--text-file 绕开 shell: 含反引号的文本原样入库。"""
        f = self.root / "tail.txt"
        f.write_text("⑬ 用 `wlog.py add` 记档。", encoding="utf-8")
        self.assertEqual(
            self._run(["add", "--session", "32", "--text-file", str(f)]), 0
        )
        self.assertIn(
            "⑬ 用 `wlog.py add` 记档。", self.worklog.read_text(encoding="utf-8")
        )

    def test_add_refuses_multiline_text(self):
        """「一条会话一个 bullet」不变量: 含换行的追加必须被拒, 文件原样不动。"""
        before = self.worklog.read_text(encoding="utf-8")
        f = self.root / "two.txt"
        f.write_text("第一行\n第二行\n", encoding="utf-8")
        self.assertEqual(
            self._run(["add", "--session", "32", "--text-file", str(f)]), 2
        )
        self.assertEqual(self.worklog.read_text(encoding="utf-8"), before)

    def test_add_requires_exactly_one_text_source(self):
        self.assertEqual(self._run(["add", "--session", "32"]), 2)
        self.assertEqual(
            self._run(["add", "--session", "32", "--text", "x", "--text-file", "y"]), 2
        )

    def test_new_backfill_closes_day_debt(self):
        """整天漏档的闭环: new 补记一条即清当日欠账(补记走工具, 不手改正源)。"""
        self._commit("src/c.py", "fix: 当天没人记档", date="2026-09-27T12:00:00")
        self.assertEqual(self._run(["check"]), 1)
        self.assertEqual(
            self._run(
                [
                    "new",
                    "--date",
                    "2026-09-27",
                    "--subject",
                    "会话 27b（补记）",
                    "--text",
                    "① 按 diff 补记。",
                ]
            ),
            0,
        )
        self.assertEqual(self._run(["check"]), 0)

    def test_new_requires_exactly_one_text_source(self):
        before = self.worklog.read_text(encoding="utf-8")
        self.assertEqual(
            self._run(["new", "--date", "2026-09-27", "--subject", "s"]), 2
        )
        self.assertEqual(
            self._run(
                [
                    "new",
                    "--date",
                    "2026-09-27",
                    "--subject",
                    "s",
                    "--text",
                    "x",
                    "--text-file",
                    "y",
                ]
            ),
            2,
        )
        self.assertEqual(self.worklog.read_text(encoding="utf-8"), before)

    def test_check_exit_code(self):
        self._commit("src/q.py", "fix: 欠账", date="2026-10-05T12:00:00")
        self.assertEqual(self._run(["check"]), 1)
        self.assertEqual(self._run(["add", "--session", "99", "--text", "x"]), 2)

    def test_check_after_option_is_plumbed(self):
        """check 必须认 --after: 守卫装好后用它把基线之前的旧账挡在门外。"""
        self._commit("src/old.py", "fix: 基线之前", date="2026-09-30T12:00:00")
        base = self._commit(
            "src/base.py", "feat(wlog): 守卫装好", date="2026-10-01T12:00:00"
        )
        self.assertEqual(self._run(["check", "--strict"]), 1)
        self.assertEqual(self._run(["check", "--strict", "--after", base]), 0)


if __name__ == "__main__":
    unittest.main()
