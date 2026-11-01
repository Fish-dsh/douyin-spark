#!/usr/bin/env python3
"""task_watchdog:任务心跳的"读者",只诊断不干预。

为什么需要它(2026-11-01,会话50):
    心跳文件 heartbeat.json 从 10 月就每晚在写, 但**没有任何程序读它** ——
    有数据、没消费者。外部评审的原话是"不需要再造一套监控系统,
    而是给现有数据增加一个独立的判定者", 这个脚本就是那个判定者。

三条硬规矩(评审确认过的):
    1. 只读:除自己的状态文件与事件行,不写任何生产文件。
    2. 不干预:发现异常只记录,**绝不**杀 Chromium、绝不重启主任务 ——
       杀了它 cron 也不会重跑,反而可能留下"发送结果不确定"的烂摊子。
    3. 不和 net_watchdog.sh 重叠:那个管网络(网口假死/切 WiFi 备线/重启),
       这个只管"任务有没有在推进"。两者互不依赖,也不从对方的健康状况推断自己。

用法:
    python -m tools.task_watchdog          # cron 每 5 分钟一次, 永远 rc=0
    python -m tools.task_watchdog --strict # 不健康时 rc=1(人工/测试用)
    python -m tools.task_watchdog --json   # 只打印判定结果, 不落盘
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from datetime import time as dtime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import RUNTIME_DIR

HEARTBEAT_FILE = RUNTIME_DIR / "heartbeat.json"
STATE_FILE = RUNTIME_DIR / "watchdog_state.json"
EVENTS_FILE = RUNTIME_DIR / "watchdog.jsonl"

# 生产窗口: cron 00:00 起(spark.py 自带 0~30 分钟抖动), 硬截止 01:35。
WINDOW_START = dtime(0, 0)
WINDOW_END = dtime(1, 35)
JITTER_MAX_SEC = 1800

# 每个阶段允许停留多久(秒)。超过 = 卡在这个阶段不动了。
# 取值偏宽松:宁可晚一点报,也不要误报 —— 一次假告警就毁掉整份报告的可信度。
STAGE_ALLOWANCE = {
    "run_start": 40 * 60,
    "jitter_done": 30 * 60,
    "discover_start": 25 * 60,
    "discover_done": 15 * 60,
    "verify_send_start": 15 * 60,
    "retry_start": 25 * 60,
    "account_done": 20 * 60,
}
STAGE_PREFIX_ALLOWANCE = {
    "account_start": 40 * 60,
    "friend_start": 10 * 60,
}
DEFAULT_ALLOWANCE = 20 * 60


def _allowance(stage: str) -> int:
    if stage in STAGE_ALLOWANCE:
        return STAGE_ALLOWANCE[stage]
    for prefix, sec in STAGE_PREFIX_ALLOWANCE.items():
        if stage.startswith(prefix):
            return sec
    return DEFAULT_ALLOWANCE


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _in_window(now: datetime) -> bool:
    t = now.time()
    return WINDOW_START <= t <= WINDOW_END


def classify(hb: dict | None, now: datetime) -> tuple[str, str]:
    """返回 (状态, 说明)。状态: HEALTHY / STALE / PAGE_STALLED / MISSING / OUT_OF_WINDOW。"""
    if not _in_window(now):
        return "OUT_OF_WINDOW", "不在 00:00-01:35 生产窗口内,本轮不判定"
    if not hb:
        return "MISSING", "生产窗口内但没有心跳文件:这轮任务可能根本没启动"

    task_id = str(hb.get("task_id") or "")
    if not task_id:
        return "MISSING", "心跳存在但没有 task_id(旧版本写入):无法判断属于哪一轮"

    ts = hb.get("ts")
    stage = str(hb.get("stage") or "?")
    try:
        age = now.timestamp() - float(ts)
    except (TypeError, ValueError):
        return "MISSING", "心跳时间字段不可解析"

    # 抖动期允许整段静默:那是睡着,不是卡死。
    if stage == "run_start" and age <= JITTER_MAX_SEC + 120:
        return "HEALTHY", f"抖动中(run_start {int(age)}s)"

    stage_ts = hb.get("stage_start_ts")
    try:
        stage_age = now.timestamp() - float(stage_ts) if stage_ts else age
    except (TypeError, ValueError):
        stage_age = age

    if age > _allowance(stage):
        return (
            "STALE",
            f"心跳已 {int(age)}s 没更新(阶段 {stage}, 允许 {_allowance(stage)}s)",
        )
    if stage_age > _allowance(stage):
        return (
            "PAGE_STALLED",
            f"心跳在跳但阶段 {stage} 已停留 {int(stage_age)}s(允许 {_allowance(stage)}s)",
        )
    return "HEALTHY", f"阶段 {stage}, 心跳 {int(age)}s 前更新"


def _record(
    status: str, detail: str, hb: dict | None, now: datetime, write: bool
) -> None:
    rec = {
        "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
        "status": status,
        "detail": detail,
        "heartbeat_task_id": (hb or {}).get("task_id", ""),
        "heartbeat_stage": (hb or {}).get("stage", ""),
        "heartbeat_iso": (hb or {}).get("iso", ""),
        "heartbeat_seq": (hb or {}).get("seq"),
    }
    if not write:
        return
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except OSError:
        pass
    try:
        with EVENTS_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="任务心跳判定器(只读)")
    ap.add_argument("--strict", action="store_true", help="不健康时返回 1")
    ap.add_argument("--json", action="store_true", help="只打印,不落盘")
    args = ap.parse_args(argv)

    now = datetime.now()  # noqa: DTZ005 — 机器本地时
    hb = _read_json(HEARTBEAT_FILE)
    status, detail = classify(hb, now)
    _record(status, detail, hb, now, write=not args.json)

    print(f"[watchdog] {now:%Y-%m-%d %H:%M:%S} {status}: {detail}")
    if args.strict and status not in ("HEALTHY", "OUT_OF_WINDOW"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
