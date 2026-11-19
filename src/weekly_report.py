#!/usr/bin/env python3
"""每周续火花账单(周报): 把过去 7 晚的结果压成一条手机上扫一眼就懂的消息。

为什么不做"实时告警": 使用者每周只有半天可看手机, 只有周日下午有半天手机, 深夜推送等于没推 ——
等看到时已经堆了六条, 反而不会再读。而抖音的火花熄灭后仍有一段补救窗口(补签 /
SVIP 恢复, 约 7 天), 所以真正要回答的问题不是"今晚成没成功", 而是
"这一周有没有人断了、现在还来不来得及救"。

设计要点:
- **只读**: 解析 runtime/spark.log 及其轮转文件(含 .gz), 绝不写 spark.log ——
  否则当晚就会冒出今天的日志行, 污染 health_check 的"当日收尾"判定
  (同 health_check 自身不写 spark.log 的自证循环教训)。
- 窗口: 默认回看 7 个**已结束**的夜晚。今天 00:00 那一轮尚未发生, 不能计入,
  否则周日中午会误报"今晚没跑"。逐好友的"最后一次成功"另按 30 天回看,
  否则断得久的人会直接从报告里消失(而他们恰恰最危险)。
- 送达: **不主动推送**。使用者一周只有半天手机, 推送既没收益又要维护一条通道
  (邮箱/ntfy 配置), 而"问一句就能查"已经满足需求 —— 报告打印到 stdout 并落盘
  `runtime/weekly_report.txt`, 想看的任何时候跑一次, 或直接读这个文件。

用法(必须 -m 方式启动: 包内相对导入 + 根目录的 logutil):
    python -m src.weekly_report              # 生成并打印(同时落盘)
    python -m src.weekly_report --days 14    # 回看两周

**不装 cron**: 不推送就没有"定时"的必要, 按需跑比定时跑到积灰更省事。
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path

from . import events, health_check
from .config import RUNTIME_DIR

LOG_FILE = RUNTIME_DIR / "spark.log"
REPORT_FILE = RUNTIME_DIR / "weekly_report.txt"

# 火花熄灭后的补救窗口: 据公开说法约 7 天内可通过补签/SVIP 恢复, 超过即彻底清零。
# 这是给使用者排序紧急程度的参考值, 不是平台承诺 —— 报告措辞用"约"。
GRACE_DAYS = 7
# 逐好友回看"最后一次成功"的天数: 要比报告窗口长, 否则断得久的人查不到来历。
SUCCESS_LOOKBACK_DAYS = 30
JUDGE_AFTER_HOUR = 2  # 凌晨 2 点前不判"今天这轮漏跑"(硬截止 01:35)

# 保守口径(2026-11-01 会话50, 外部评审建议后定): 7 天只是二手来源(补签 7 天 /
# SVIP 每月 1 次), 起算点与截止时刻都没有一手证据, 所以工程上按 6 天设计、
# 预留 1 天余量 —— 但报告必须把这句假设标出来, 不能把假设当规则。
ASSUMED_GRACE_DAYS = 6
OFFICIAL_RULE_LABEL = "RESCUE_WINDOW_ASSUMED_6D / OFFICIAL_RULE_UNVERIFIED"
# 使用者的可动手时间: 每周日下午(每周这半天有手机)。
# 所以"还剩几天"不是有效指标, 有效的是"距下次能动手还剩几小时"。
HUMAN_WEEKDAY = 6  # Sunday
HUMAN_HOUR = 14

_LINE_DATE_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2})[ T]")
_COMPLETE_RE = re.compile(
    r"====\s*账号\s+(\S+)\s+完成:\s*发送\s*(\d+)\s*个\s*/\s*失败\s*(\d+)\s*个"
)
_NO_PENDING_RE = re.compile(r"没有需要续火花的好友, 账号\s+(\S+)\s+结束")
_SKIP_RE = re.compile(r"【账号\s+(.+?)\s+未配置 account/password")
_SUCCESS_RE = re.compile(r"【(.+?)】发送成功")
_FAIL_RE = re.compile(r"【(.+?)】(?:处理失败|发送未确认成功)")


@dataclass
class Day:
    """一个夜晚(00:00 那轮)的结局。"""

    accounts: dict[str, tuple[int, int]] = field(default_factory=dict)
    no_pending: set[str] = field(default_factory=set)
    skipped: set[str] = field(default_factory=set)
    successes: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    line_count: int = 0

    @property
    def finished(self) -> set[str]:
        return set(self.accounts) | self.no_pending | self.skipped

    @property
    def sent(self) -> int:
        return sum(v[0] for v in self.accounts.values())

    @property
    def failed(self) -> int:
        return sum(v[1] for v in self.accounts.values())


def _as_int(text: str) -> int:
    try:
        return int(text)
    except ValueError:
        return 0


def _parse_line(day: Day, line: str) -> None:
    """把一行日志归到这一夜的结局里(认不出的行直接忽略)。"""
    matched = _COMPLETE_RE.search(line)
    if matched:
        day.accounts[matched.group(1)] = (
            _as_int(matched.group(2)),
            _as_int(matched.group(3)),
        )
        return
    matched = _NO_PENDING_RE.search(line)
    if matched:
        day.no_pending.add(matched.group(1))
        return
    matched = _SKIP_RE.search(line)
    if matched:
        day.skipped.add(matched.group(1))
        return
    matched = _SUCCESS_RE.search(line)
    if matched:
        day.successes.append(matched.group(1))
        return
    matched = _FAIL_RE.search(line)
    if matched:
        day.failures.append(matched.group(1))


def log_files() -> list[Path]:
    """SPARK 日志本体 + 轮转文件(spark.log.1 / spark.log.7.gz ...), 顺序无关。

    只认 logrotate 真正产出的名字(spark.log.N / spark.log.N.gz)。runtime/ 里还躺着
    一次性的历史归档 spark.log.pre-sync-20260927(2026-08~09 的旧开发期日志), 把它
    算进来会让"日志覆盖起点"一路退到两个月前, coverage 逻辑当场失效(实测踩过)。
    """
    files: list[Path] = []
    if LOG_FILE.is_file():
        files.append(LOG_FILE)
    parent = LOG_FILE.parent
    if parent.is_dir():
        rotated = re.compile(r"^" + re.escape(LOG_FILE.name) + r"\.\d+(?:\.gz)?$")
        files.extend(
            sorted(
                p for p in parent.glob(LOG_FILE.name + ".*") if rotated.match(p.name)
            )
        )
    return files


def _read_lines(path: Path) -> list[str]:
    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
                return fh.read().splitlines()
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def collect(start: date, end: date, files: list[Path] | None = None) -> dict[date, Day]:
    """归集 [start, end] 内每个自然日的结局; 没有任何行的日子不出现在结果里。"""
    days: dict[date, Day] = {}
    for path in files if files is not None else log_files():
        for line in _read_lines(path):
            matched = _LINE_DATE_RE.match(line)
            if not matched:
                continue
            try:
                day = date.fromisoformat(matched.group(1))
            except ValueError:
                continue
            if not (start <= day <= end):
                continue
            bucket = days.setdefault(day, Day())
            bucket.line_count += 1
            _parse_line(bucket, line)
    return days


def _read_first_line(path: Path) -> str:
    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
                return fh.readline()
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.readline()
    except OSError:
        return ""


def coverage_start(files: list[Path] | None = None) -> date | None:
    """日志最早能追溯到哪一天(取各轮转文件首行日期的最小值)。

    必须区分「那一晚没跑」和「那晚的日志已被轮转掉」: 板端 logrotate 是
    2026-10-27 前后才真正生效的, 之前的夜晚一律无日志。若不扣掉这段
    "无记录区间", 报告第一句话就会诬告板子断电两晚 —— 假告警一次就足以让人
    从此不再相信这份报告(同 health_check 里"假告警=告警疲劳"的教训)。
    """
    earliest: date | None = None
    for path in files if files is not None else log_files():
        matched = _LINE_DATE_RE.match(_read_first_line(path))
        if not matched:
            continue
        try:
            day = date.fromisoformat(matched.group(1))
        except ValueError:
            continue
        if earliest is None or day < earliest:
            earliest = day
    return earliest


def _last_success(days: dict[date, Day]) -> dict[str, date]:
    """好友 → 最后一次"发送成功"的日期(取历史里最晚的那次)。"""
    last: dict[str, date] = {}
    for day in sorted(days):
        for name in days[day].successes:
            last[name] = day  # 按日期升序遍历, 后写覆盖前写 = 最晚
    return last


def _account_names() -> list[str]:
    """config.json 声明的账号(判"有没有跑完"要用声明口径, 不是日志里出现过谁)。"""
    return [name for name, _ in health_check._configured_accounts()]


def _broken_friends(days: dict[date, Day], today: date) -> list[tuple[str, date, int]]:
    """现在仍断着的好友 → (名字, 最近一次失败日, 距今天数)。

    判定: 该好友最后一次"发送成功"早于他的最后一次失败, 就算还没补上。
    只报日志里失败过的人; 从未出现的好友无从判断, 不猜。
    """
    last_ok = _last_success(days)
    last_fail: dict[str, date] = {}
    for day in sorted(days):
        for name in days[day].failures:
            last_fail[name] = day
    broken: list[tuple[str, date, int]] = []
    for name, fail_day in sorted(last_fail.items()):
        ok_day = last_ok.get(name)
        if ok_day is not None and ok_day >= fail_day:
            continue
        broken.append((name, fail_day, (today - fail_day).days))
    return broken


def _fmt_md(day: date) -> str:
    return f"{day.month}-{day.day:02d}"


def next_human_time(now: datetime) -> datetime:
    """下次他能真正动手的时间: 每周日下午 HUMAN_HOUR 点。"""
    ahead = (HUMAN_WEEKDAY - now.weekday()) % 7
    cand = (now + timedelta(days=ahead)).replace(
        hour=HUMAN_HOUR, minute=0, second=0, microsecond=0
    )
    if cand <= now:
        cand += timedelta(days=7)
    return cand


def action_window(human: datetime, deadline: datetime) -> tuple[int, str]:
    """人工处理余量 M = 保守截止 - 下次能动手。返回 (小时, 等级)。"""
    hours = int((deadline - human).total_seconds() // 3600)
    if hours >= 48:
        level = "正常"
    elif hours >= 24:
        level = "注意"
    elif hours > 0:
        level = "紧急"
    else:
        level = "ACTION_WINDOW_AT_RISK"
    return hours, level


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def collect_health(now: datetime) -> dict:
    """健康面: 心跳/看门狗/结构化事件/告警日志/内存, 全部容错。

    原则: **没有数据 ≠ 健康**。读不到就明确写"未采集", 不写绿色对勾 ——
    这正是板上 alerts.log 断在 10-20 之后暴露出来的教训。
    """
    hb = _load_json(RUNTIME_DIR / "heartbeat.json")
    wd = _load_json(RUNTIME_DIR / "watchdog_state.json")
    rows = events.read_events()
    alert_last = ""
    alert_bad = 0
    alert_lines = 0
    try:
        with (RUNTIME_DIR / "alerts.log").open(encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                alert_lines += 1
                alert_last = line
                if "delivered=0" in line or "delivered=False" in line:
                    alert_bad += 1
    except OSError:
        pass
    mem_min = None
    for rec in rows:
        val = rec.get("mem_available_mb")
        if isinstance(val, (int, float)):
            mem_min = val if mem_min is None else min(mem_min, val)
    return {
        "heartbeat": hb,
        "watchdog": wd,
        "event_rows": len(rows),
        "layers": events.layer_stats(rows) if rows else {},
        "alerts_last": alert_last,
        "alerts_lines": alert_lines,
        "alerts_bad": alert_bad,
        "mem_min": mem_min,
    }


def _health_lines(health: dict, now: datetime) -> list[str]:
    out = ["", "【健康】(没有数据 ≠ 健康)"]
    hb = health.get("heartbeat")
    if hb:
        age_min = int((now.timestamp() - float(hb.get("ts") or 0)) // 60)
        out.append(
            f"  心跳：{hb.get('stage', '?')} @ {hb.get('iso', '?')}"
            f"（{age_min} 分钟前，task {str(hb.get('task_id') or '无')[:13]}）"
        )
    else:
        out.append("  ❌ 心跳：没有文件（这轮可能根本没启动）")

    wd = health.get("watchdog")
    if wd:
        out.append(f"  看门狗：{wd.get('status', '?')} @ {wd.get('ts', '?')}")
    else:
        out.append("  ❌ 看门狗：还没有运行过（未采集，不是「没问题」）")

    n = health.get("event_rows") or 0
    if n:
        layers = health.get("layers") or {}
        top = sorted(layers.items(), key=lambda kv: -kv[1]["attempts"])[:4]
        seg = "；".join(
            f"{name} {st['attempts']}次/验证{st['verified_success']}/独有{st['unique_recovery']}"
            for name, st in top
        )
        out.append(f"  事件：{n} 条 — {seg}")
    else:
        out.append("  ❌ 事件：events.jsonl 还没有数据（部署后开始采集）")

    if health.get("alerts_last"):
        bad = health.get("alerts_bad") or 0
        out.append(
            f"  告警日志：{health.get('alerts_lines')} 行，最后一行 {health['alerts_last'][:46]}"
            f"（未送达 {bad} 次）"
        )
    else:
        out.append("  ❌ 告警日志：没有记录（从未写入过）")

    mem = health.get("mem_min")
    if mem is not None:
        out.append(f"  最低可用内存：{mem:.0f}MB")
    else:
        out.append("  内存：未采集")
    return out


def _tail(note: str) -> list[str]:
    tail = ["", "今晚 00:00 会自动再跑一次。"]
    if note:
        tail.extend(["", note])
    return tail


def build_report(
    today: date,
    start: date,
    end: date,
    days: dict[date, Day],
    note: str = "",
    health: dict | None = None,
    now: datetime | None = None,
) -> str:
    """生成给人看的周报正文(纯文本, 手机/终端都读得下)。"""
    nights = (end - start).days + 1
    all_nights = [start + timedelta(days=i) for i in range(nights)]
    present = [d for d in all_nights if d in days]
    sent = sum(days[d].sent for d in present)
    failed = sum(days[d].failed for d in present)
    # 今天那一轮可能还在跑(窗口 23:30~次日 01:35), 没数据不算漏跑。
    missing_nights = [d for d in all_nights if d not in days and d < today]

    problems: list[str] = []
    configured = _account_names()
    if configured:
        for d in present:
            missing = [n for n in configured if n not in days[d].finished]
            if missing:
                problems.append(
                    f"⚠️ {_fmt_md(d)} 没跑完：{', '.join(missing)} 没有收尾记录"
                )
    for d in present:
        if days[d].failed > 0:
            names = list(dict.fromkeys(days[d].failures))
            detail = "、".join(names) if names else "（日志里没留名字）"
            problems.append(f"⚠️ {_fmt_md(d)} 有 {days[d].failed} 个没发出去：{detail}")
    if missing_nights:
        problems.append(
            "⚠️ 这几晚完全没有日志(板子没跑/断电/断网)："
            + "、".join(_fmt_md(d) for d in missing_nights)
        )

    # 断着的人要先算: 某人可能在上周就断了, 之后每晚"看起来很健康"(抖音已不再把他
    # 列为 need), 若只在"本周有失败"时才查断档, 这种人会永久从报告里消失。
    broken = _broken_friends(days, today)

    lines = [f"【续火花周报】{today.month}月{today.day}日", ""]
    lines.append(f"{_fmt_md(start)} ~ {_fmt_md(end)} 这 {nights} 晚：")
    if not problems and not broken:
        lines.append(f"  ✅ 全部正常，共续上 {sent} 次，0 次失败")
        lines.append("")
        lines.append("需要你动手的：没有 🎉")
        if health is not None:
            lines.extend(_health_lines(health, now or datetime.now()))  # noqa: DTZ005
        lines.extend(_tail(note))
        return "\n".join(lines)

    lines.append(f"  {len(present)} 晚有记录，共续上 {sent} 次，失败 {failed} 次")
    if problems:
        lines.append("")
        lines.extend(problems)

    lines.append("")
    cur = now or datetime.now()  # noqa: DTZ005 — 全项目时间口径=机器本地时
    human = next_human_time(cur)
    if broken:
        for name, fail_day, ago in broken:
            left = GRACE_DAYS - ago
            # 保守截止时刻: 失败日 + 6 天, 当天 23:59(不假设平台按自然日 0 点算)。
            deadline = datetime.combine(
                fail_day + timedelta(days=ASSUMED_GRACE_DAYS), dtime(23, 59)
            )
            hours, level = action_window(human, deadline)
            if left > 0:
                lines.append(
                    f"👉 {name}：从 {_fmt_md(fail_day)} 起没发成功，已 {ago} 天，"
                    f"补救窗口约还剩 {left} 天"
                )
            else:
                lines.append(
                    f"👉 {name}：已 {ago} 天，超过约 {GRACE_DAYS} 天的补救窗口，"
                    "可能要重新开始攒了"
                )
            lines.append(
                f"   保守口径最晚 {_fmt_md(deadline.date())} 要补上"
                f"（按 {ASSUMED_GRACE_DAYS} 天算）；你下次能动手 {_fmt_md(human.date())}"
                f" 下午，余量 {hours} 小时 → {level}"
            )
        lines.append(
            "   做法：打开抖音和 TA 连续互发消息 3 天（列表会显示“重燃中 x/3”）"
        )
    else:
        lines.append("需要你动手的：打开抖音看一眼上面那几晚的聊天（失败当晚已重试过）")

    lines.extend(_health_lines(health, cur) if health is not None else [])
    lines.extend(_tail(note))
    return "\n".join(lines)


def run(days_back: int = 7) -> int:
    now = datetime.now()  # noqa: DTZ005 — 全项目时间口径=机器本地时(见 config.py 头注释)
    today = now.date()
    # 昨晚那一轮的日志日期是"今天"(00:00 起跑), 截到昨天就会把最近一晚整个漏掉;
    # 而凌晨 2 点前今天这轮可能还在跑, 交给 build_report 的"今天不判漏跑"兜住。
    end = today if now.hour >= JUDGE_AFTER_HOUR else today - timedelta(days=1)
    start = end - timedelta(days=days_back - 1)
    lookback_start = today - timedelta(days=SUCCESS_LOOKBACK_DAYS)

    files = log_files()
    cov = coverage_start(files)
    days = collect(lookback_start, end, files)
    # 日志能追溯到的第一天之前一律不算"漏跑": 那是被轮转掉了, 不是板子没干活。
    eff_start = start if cov is None else max(start, cov)
    note = ""
    if eff_start > start:
        note = f"（日志最早只到 {_fmt_md(eff_start)}，更早的夜晚没有记录，未计入）"
    note = (note + " " if note else "") + f"（补救窗口口径：{OFFICIAL_RULE_LABEL}）"
    health = collect_health(now)
    report = build_report(today, eff_start, end, days, note, health=health, now=now)

    print(report)
    try:
        REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
        REPORT_FILE.write_text(report + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"[weekly] 周报落盘失败(忽略): {exc}", file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="抖音续火花周报(按需查看, 不推送)")
    parser.add_argument("--days", type=int, default=7, help="回看几晚(默认 7)")
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days 至少为 1")
    return run(days_back=args.days)


if __name__ == "__main__":
    sys.exit(main())
