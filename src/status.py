"""一句话状态: 最近一夜成了吗 / 失败在哪 / 下一步查什么。

为什么要有它(2026-11-22): 这个项目本来就是给 Agent 用的, 但"昨晚怎么样"只能
`grep runtime/spark.log` —— 人懒得看, Agent 每次接手都要重新学一遍日志格式。
所以把每天最先要回答的问题压成一条 stdout; `--json` 给程序读。

只读纪律(同 weekly_report): 只解析日志与心跳, 绝不写 spark.log —— 否则当晚就
冒出今天的日志行, 污染 health_check 的"当日收尾"判定。也因此这里不调
load_config(它会 log), 只把 config.json 当纯文本读。

退出码恒为 0: 这是查询命令, "昨晚有失败"不是查询失败。要不要报警看输出/JSON。
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import date, datetime, timedelta

from . import weekly_report
from .config import CONFIG_FILE, RUNTIME_DIR

_LOOKBACK_DAYS = 7
_FAIL_RE = re.compile(r"【([^】]{1,40})】(处理失败|发送未确认成功)")
_REASON_RE = re.compile(r"[(（]([^()（）]{2,60})[)）]")
_TIMING_RE = re.compile(r"\[timing\] 本次运行总耗时\s*(\S+)")

# 失败原因 → 下一步能做什么。键是日志里出现过的子串, 命中即给出该条建议。
_ADVICE = (
    (
        "登录态",
        "登录态失效: 在有头环境重跑 python login.py 扫码, 再把 storage.json 同步到板子",
    ),
    (
        "重试",
        "本地重试也没成: 看当晚 runtime/screenshots 截图, 多半是页面文字变了",
    ),
    (
        "未确认",
        "发送没被确认: 面板开了但没读到新气泡 —— 先看截图, 再对照识别策略",
    ),
)


def _window() -> tuple[str, str]:
    """发送窗口。读不到 config.json 就用默认值, 不打印任何警告。"""
    try:
        raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "23:30", "00:30"
    win = raw.get("send_window") or {}
    return str(win.get("start", "23:30")), str(win.get("end", "00:30"))


def _label(night: date, now: datetime) -> str:
    delta = (now.date() - night).days
    if delta <= 0:
        return "今天凌晨"
    if delta == 1:
        return "昨晚"
    return f"{delta} 天前"


def _detail(night: date) -> tuple[list[dict], str]:
    """当晚的失败明细与总耗时(同一遍扫描取两样, 日志几百 KB 不值得扫两次)。"""
    fails: list[dict] = []
    seen: set[tuple[str, str]] = set()
    timing = ""
    prefix = f"[{night.isoformat()}"
    for path in weekly_report.log_files():
        for line in weekly_report._read_lines(path):
            if not line.startswith(prefix):
                continue
            matched = _TIMING_RE.search(line)
            if matched:
                timing = matched.group(1)
            matched = _FAIL_RE.search(line)
            if not matched:
                continue
            friend, kind = matched.group(1), matched.group(2)
            if friend.startswith("账号"):  # 账号级汇总行, 不是某个好友
                continue
            reason = _REASON_RE.search(line[matched.end() :])
            text = reason.group(1) if reason else kind
            if (friend, text) in seen:
                continue
            seen.add((friend, text))
            fails.append({"friend": friend, "reason": text})
    return fails, timing


def _last_logged() -> date | None:
    """日志里最后一条带日期的行。只在"最近没有完整一夜"时才调用 —— 那时才需要
    回答"最后一次到底是什么时候"。"""
    newest: date | None = None
    for path in weekly_report.log_files():
        for line in weekly_report._read_lines(path):
            matched = weekly_report._LINE_DATE_RE.match(line)
            if not matched:
                continue
            try:
                day = date.fromisoformat(matched.group(1))
            except ValueError:
                continue
            if newest is None or day > newest:
                newest = day
    return newest


def _heartbeat(now: datetime) -> dict | None:
    try:
        raw = json.loads((RUNTIME_DIR / "heartbeat.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        age = int((now.timestamp() - float(raw.get("ts") or 0)) // 60)
    except (TypeError, ValueError):
        return None
    return {
        "stage": raw.get("stage", "?"),
        "iso": raw.get("iso", "?"),
        "age_min": age,
    }


def _advice(fails: list[dict]) -> list[str]:
    out: list[str] = []
    for key, tip in _ADVICE:
        if any(key in f["reason"] for f in fails) and tip not in out:
            out.append(tip)
    return out


def build(now: datetime | None = None) -> dict:
    """归集最近一夜的结局。没有任何完整记录的夜不算数 —— 半路死掉的一夜更危险。"""
    now = now or datetime.now()
    days = weekly_report.collect(
        now.date() - timedelta(days=_LOOKBACK_DAYS), now.date()
    )
    night: date | None = None
    for back in range(_LOOKBACK_DAYS + 1):
        candidate = now.date() - timedelta(days=back)
        bucket = days.get(candidate)
        if bucket is not None and bucket.finished:
            night = candidate
            break

    data: dict = {
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "has_record": night is not None,
        "night": night.isoformat() if night else None,
        "night_label": _label(night, now) if night else "",
        "sent": 0,
        "failed": 0,
        "accounts": {},
        "failures": [],
        "timing": "",
        "window": list(_window()),
        "heartbeat": _heartbeat(now),
        "advice": [],
    }
    if night is None:
        last = _last_logged()
        data["last_log_date"] = last.isoformat() if last else None
        data["advice"] = [
            "最近 7 天没有完整的一夜: 先确认 cron 还在(crontab -l | grep run_cron)",
            "再确认日志有没有写(ls -l runtime/spark.log)",
        ]
        return data

    bucket = days[night]
    fails, timing = _detail(night)
    data.update(
        {
            "sent": bucket.sent,
            "failed": bucket.failed,
            "accounts": {k: list(v) for k, v in bucket.accounts.items()},
            "failures": fails,
            "timing": timing,
        }
    )
    if bucket.skipped:
        data["skipped"] = sorted(bucket.skipped)
    data["advice"] = _advice(fails)
    return data


def render(data: dict) -> str:
    """人读的一句话; 只有真出问题才展开细节。"""
    lines: list[str] = []
    if not data["has_record"]:
        lines.append("【状态】最近 7 天没有完整的一夜记录")
        last = data.get("last_log_date")
        lines.append(f"  最近一次日志: {last}" if last else "  日志里一行都没有")
        lines.extend(f"  → {tip}" for tip in data["advice"])
        return "\n".join(lines)

    total = data["sent"] + data["failed"]
    head = f"【状态】{data['night_label']}({data['night']}) 那一轮: "
    if total == 0:
        lines.append(head + "没有待续好友, 无需发送")
    else:
        lines.append(head + f"发送 {data['sent']} / {total}, 失败 {data['failed']}")
        for item in data["failures"]:
            lines.append(f"  ✗ {item['friend']} · {item['reason']}")
        per = " · ".join(
            f"{name} {v[0]}/{v[0] + v[1]}" for name, v in data["accounts"].items()
        )
        tail = f"  {per}" if per else ""
        if data["timing"]:
            tail += f" ｜ 耗时 {data['timing']}"
        if tail:
            lines.append(tail)
    if data.get("skipped"):
        lines.append("  跳过: " + "、".join(data["skipped"]))
    hb = data["heartbeat"]
    if hb and hb["age_min"] <= 180:
        lines.append(f"  心跳: {hb['stage']} @ {hb['iso']}({hb['age_min']} 分钟前)")
    for tip in data["advice"]:
        lines.append(f"  → {tip}")
    if not data["advice"]:
        start, end = data["window"]
        lines.append(f"  下一个窗口: 每天 {start}-{end}(cron 00:00 触发)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="spark.py --status", description="一句话状态: 最近一夜的结果"
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON(给程序读)")
    args = parser.parse_args(argv)
    data = build()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(render(data))
    return 0
