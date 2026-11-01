#!/usr/bin/env python3
"""wlog.py · WORKLOG 记档守卫(确定性机制, 不依赖「AI 还记得写」)。

四个动作 + 一个对账:
    add    把文本追加到指定会话条目末尾(锚点唯一性校验 + 原子写, 取代手搓字符串替换;
           含反引号等 shell 敏感字符的长文本用 --text-file 传, 别让它过 shell)
    new    新起一条时间线 bullet(补记整天漏档用), 只插入不动历史
    stub   提交动了「状态类文件」却没动 WORKLOG → 插一行待补欠债(同轮补记 / 收尾对账用)
    check  对账: 默认按天找「改了状态类文件却当天没有任何 WORKLOG 条目」的日子; --strict 逐 commit。
           --after REV 只核对基线(守卫装好那笔提交)之后, --strict 下同提交自带 WORKLOG 改动不算欠账。
           自愈基线(heal:) 单列不计。有欠账退出码 1。

设计约束:
- 只做插入, 不重排、不改写历史条目(遵 WORKLOG 维护约定)。
- 任何写操作都走 临时文件 + os.replace, 失败不留半截文件。
- 幂等: 同一 commit 不会重复留 stub。
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
WORKLOG = REPO_DIR / "WORKLOG.md"
HEADING = "## 会话时间线"

# 「状态类」= 改变系统实际运行内容的文件(代码/依赖/运行配置), 文档与证据不算。
STATE_PATTERNS = (
    "src/*.py",
    "*.py",
    "requirements.txt",
    "config.json",
    "config.example.json",
    "run_cron.sh",
    "logrotate.conf",
    "ruff.toml",
)

STUB_FMT = "- **{date} · (auto-stub) commit {sha}**：{subject} ⚠️ 待补动机/结论(tools/wlog.py 自动留痕)"


def _git(*args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(REPO_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败: {(proc.stderr or '').strip()}")
    return proc.stdout


def is_state_path(path: str) -> bool:
    """该文件是否属于「改了就得记档」的状态类文件。"""
    p = path.strip().lstrip("./")
    if not p or p == "WORKLOG.md":
        return False
    return any(fnmatch.fnmatch(p, pat) for pat in STATE_PATTERNS)


def session_anchor(session: str) -> re.Pattern[str]:
    return re.compile(rf"^- \*\*[\d-]+ · 会话 {re.escape(session)}（")


def append_to_session(text: str, session: str, dry_run: bool = False) -> str:
    """把 text 追加到「会话 N」那条时间线 bullet 末尾。返回结果描述。"""
    if "\n" in text.strip():
        raise ValueError("追加文本含换行, 会破坏「一条会话一个 bullet」结构, 已拒绝")
    lines = WORKLOG.read_text(encoding="utf-8").split("\n")
    hits = [i for i, l in enumerate(lines) if session_anchor(session).match(l)]
    if len(hits) != 1:
        raise ValueError(
            f"会话 {session} 锚点匹配 {len(hits)} 处(需恰好 1 处), 未做任何修改"
        )
    i = hits[0]
    if not lines[i].rstrip().endswith(("。", "**", "）", ")")):
        raise ValueError(f"会话 {session} 条目结尾异常, 拒绝追加: ...{lines[i][-30:]}")
    lines[i] = lines[i].rstrip() + " " + text.strip()
    if not dry_run:
        _write(lines)
    return f"{'[dry-run] ' if dry_run else ''}已追加到会话 {session}(条目现长 {len(lines[i])} 字)"


def _timeline_bounds(lines: list[str]) -> tuple[int, int, int]:
    """返回 (标题行下标, 首条 bullet 下标, 末条 bullet 下标)。"""
    try:
        h = lines.index(HEADING)
    except ValueError:
        raise ValueError(f"未找到标题 {HEADING!r}, 未做任何修改") from None
    i, first, last = h + 1, None, None
    while i < len(lines) and not lines[i].startswith("#"):
        if lines[i].startswith("- "):
            if first is None:
                first = i
            last = i
        i += 1
    if first is None:
        raise ValueError("时间线里找不到条目行, 未做任何修改")
    return h, first, last


def _bullet_date(line: str) -> str | None:
    m = re.match(r"^- \*\*(\d{4}-\d{2}-\d{2}) ·", line)
    return m.group(1) if m else None


def _insert(entry: str, date: str | None = None, dry_run: bool = False) -> int:
    """插入整行 bullet(只插入, 不动任何历史条目)。

    date=None, 或日期不早于现有最新条目 → 时间线顶部;
    否则按「最新在前」落到第一条日期严格早于它的条目之前(同日条目之下);
    找不到更早的日期锚点则落在时间线末尾。
    """
    lines = WORKLOG.read_text(encoding="utf-8").split("\n")
    _h, first, last = _timeline_bounds(lines)
    dated = [(j, _bullet_date(lines[j])) for j in range(first, last + 1)]
    dated = [(j, d) for j, d in dated if lines[j].startswith("- ") and d]
    i = last + 1
    if date is None or (dated and date >= dated[0][1]):
        i = first
    else:
        for j, d in dated:
            if d < date:
                i = j
                break
    lines.insert(i, entry)
    if not dry_run:
        _write(lines)
    return i


def insert_stub(sha: str, subject: str, date: str, dry_run: bool = False) -> str:
    """在时间线顶部插一条 auto-stub(最新在前)。同一 sha 已存在则跳过。"""
    lines = WORKLOG.read_text(encoding="utf-8").split("\n")
    if any(sha in l for l in lines):
        return f"commit {sha} 已在 WORKLOG 中, 无需留痕"
    entry = STUB_FMT.format(date=date, sha=sha, subject=subject.strip())
    i = _insert(entry, dry_run=dry_run)
    return f"{'[dry-run] ' if dry_run else ''}已留 stub(第 {i + 1} 行): {entry[:60]}…"


def new_entry(text: str, date: str, subject: str, dry_run: bool = False) -> str:
    """新起一条时间线 bullet, 用于补记整天漏档(落到该日应有的位置, 不打乱既有顺序)。"""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise ValueError(
            f"--date 需为 YYYY-MM-DD, 收到 {date!r}(日期格式不对 check 认不出这一天)"
        )
    if "\n" in text.strip():
        raise ValueError("条目正文含换行, 会破坏「一条会话一个 bullet」结构, 已拒绝")
    entry = f"- **{date} · {subject.strip()}**：{text.strip()}"
    i = _insert(entry, date, dry_run)
    return (
        f"{'[dry-run] ' if dry_run else ''}已新起条目(第 {i + 1} 行, {len(entry)} 字)"
    )


def commit_state_files(sha: str) -> list[str]:
    out = _git("show", "--pretty=format:", "--name-only", sha)
    return [l.strip() for l in out.split("\n") if l.strip() and is_state_path(l)]


def parse_log_entries(max_commits: int = 200, after: str | None = None) -> list[dict]:
    """一次 git 调用解出 [{sha, subject, date, files}] (最新在前)。"""
    rng = [f"{after}..HEAD"] if after else [f"-n{max_commits}"]
    raw = _git(
        "log",
        *rng,
        "--name-only",
        "--date=short",
        "--format=%x1e%h%x1f%ad%x1f%s",
    )
    entries = []
    for chunk in raw.split("\x1e"):
        chunk = chunk.strip("\n")
        if not chunk.strip():
            continue
        head, _, rest = chunk.partition("\n")
        parts = head.split("\x1f")
        if len(parts) != 3:
            continue
        sha, date, subject = parts
        files = [l.strip() for l in rest.split("\n") if l.strip()]
        entries.append({"sha": sha, "date": date, "subject": subject, "files": files})
    return entries


def recorded_dates(text: str) -> set[str]:
    """WORKLOG 已记档的日期集合(时间线 bullet 的 `**YYYY-MM-DD ·` 前缀)。"""
    return set(re.findall(r"^- \*\*(\d{4}-\d{2}-\d{2}) ·", text, re.MULTILINE))


def unrecorded_commits(
    max_commits: int = 200, strict: bool = False, after: str | None = None
) -> tuple[list[dict], list[dict]]:
    """返回 (未记档的状态类提交, 自愈基线提交)。后者按约定可不记档, 单列不报错。

    默认按**天**判: 该提交改了状态类文件, 而 WORKLOG 里没有当天的任何条目 → 欠账
    (WORKLOG 是会话粒度, 逐 commit 比会误伤几十条已用叙事记档的历史)。
    strict=True 按 **commit sha** 判: 配合 --after REV(守卫装好那笔提交)使用, 保证
    「基线之后每个状态类 commit 要么并进会话条目/要么有 auto-stub 欠债行/要么同提交自带记档」。
    """
    text = WORKLOG.read_text(encoding="utf-8") if WORKLOG.is_file() else ""
    days = recorded_dates(text)
    todo, heal = [], []
    for e in parse_log_entries(max_commits, after):
        if e["sha"] in text:
            continue
        if e["subject"].startswith("heal:"):
            heal.append(e)
            continue
        if not any(is_state_path(f) for f in e["files"]):
            continue
        if strict and "WORKLOG.md" in e["files"]:
            continue  # 代码与记档同一提交 = 理想姿势, 不是欠账
        if strict or e["date"] not in days:
            todo.append(e)
    return todo, heal


def _write(lines: list[str]) -> None:
    tmp = WORKLOG.with_suffix(".md.tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    os.replace(str(tmp), str(WORKLOG))


def cmd_add(a: argparse.Namespace) -> int:
    if bool(a.text) == bool(a.text_file):
        raise ValueError("--text 与 --text-file 必须恰好给一个")
    text = a.text if a.text else Path(a.text_file).read_text(encoding="utf-8")
    print(append_to_session(text, a.session, a.dry_run))
    return 0


def cmd_new(a: argparse.Namespace) -> int:
    if bool(a.text) == bool(a.text_file):
        raise ValueError("--text 与 --text-file 必须恰好给一个")
    text = a.text if a.text else Path(a.text_file).read_text(encoding="utf-8")
    print(new_entry(text, a.date, a.subject, a.dry_run))
    return 0


def cmd_stub(a: argparse.Namespace) -> int:
    sha = _git("rev-parse", "--short", a.rev).strip()
    subject = _git("show", "-s", "--format=%s", sha).strip()
    date = _git("show", "-s", "--format=%ad", "--date=short", sha).strip()
    state_files = commit_state_files(sha)
    touched = _git("show", "--pretty=format:", "--name-only", sha)
    if "WORKLOG.md" in [l.strip() for l in touched.split("\n")]:
        print(f"commit {sha} 已含 WORKLOG 改动, 无需留痕")
        return 0
    if not state_files:
        print(f"commit {sha} 未触碰状态类文件, 无需留痕")
        return 0
    print(insert_stub(sha, subject, date, a.dry_run))
    return 0


def cmd_check(a: argparse.Namespace) -> int:
    todo, heal = unrecorded_commits(a.max_commits, a.strict, a.after)
    if heal:
        print(f"[wlog] 自愈基线提交 {len(heal)} 个未记档(按约定可略过)")
    if not todo:
        print("[wlog] OK: 状态类提交均已记档")
        return 0
    # 欠账清单走 stdout: 调用方(智能体)读 stdout, 而某些环境会吞掉/乱序 stderr
    scope = "逐 commit" if a.strict else "按天"
    print(f"[wlog] 有 {len(todo)} 个状态类提交未记档({scope}):")
    for e in todo:
        print(f"  - {e['date']} {e['sha']} {e['subject']}")
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="WORKLOG 记档守卫")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="追加文本到指定会话条目")
    p_add.add_argument("--session", required=True, help="会话编号, 如 32")
    p_add.add_argument("--text", help="追加文本(单行、无 shell 特殊字符时可用)")
    p_add.add_argument(
        "--text-file", help="从文件读追加文本(含反引号等字符时用, 免被 shell 吞)"
    )
    p_add.add_argument("--dry-run", action="store_true")
    p_add.set_defaults(fn=cmd_add)

    p_new = sub.add_parser("new", help="新起一条时间线条目(补记整天漏档)")
    p_new.add_argument(
        "--date", required=True, help="条目日期 YYYY-MM-DD(补记的历史当天)"
    )
    p_new.add_argument(
        "--subject", required=True, help="条目标题, 如「会话 27b（补记：…）」"
    )
    p_new.add_argument("--text", help="条目正文(单行、无 shell 特殊字符时可用)")
    p_new.add_argument(
        "--text-file", help="从文件读正文(含反引号等字符时用, 免被 shell 吞)"
    )
    p_new.add_argument("--dry-run", action="store_true")
    p_new.set_defaults(fn=cmd_new)

    p_stub = sub.add_parser("stub", help="为某提交留待补欠债行")
    p_stub.add_argument("--rev", default="HEAD")
    p_stub.add_argument("--dry-run", action="store_true")
    p_stub.set_defaults(fn=cmd_stub)

    p_chk = sub.add_parser("check", help="对账未记档的状态类提交")
    p_chk.add_argument("--max-commits", type=int, default=200)
    p_chk.add_argument(
        "--strict",
        action="store_true",
        help="逐 commit 判(守卫装好后用); 默认按天判, 不误伤已用叙事记档的历史",
    )
    p_chk.add_argument(
        "--after",
        metavar="REV",
        help="只核对该提交之后的历史(守卫装好那笔提交 = 基线, 更早的漏账另有账)",
    )
    p_chk.set_defaults(fn=cmd_check)

    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except (RuntimeError, ValueError) as exc:
        print(f"[wlog] 中止: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
