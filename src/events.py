"""结构化运行事件(JSONL):只追加写,与 spark.log 完全隔离。

为什么单独一个文件(2026-11-01,会话50):
    spark.log 是 health_check.py 的**输入**,它靠中文行(「==== 账号 X 完成」/
    「收尾」)判断当日是否收尾。往里塞 JSONL 会污染那个解析器,等于拿日志格式
    去赌一个老解析器。事件流因此单独落 runtime/events.jsonl,
    由 tools/strat_stats.py 与 src/weekly_report.py 消费。

三条设计约束(外部评审两轮之后定下来的):
    1. 写事件**绝不允许**影响发送主流程:任何异常都被吞掉,连目录不可写也算。
    2. task_id 由任务自己在启动时生成(task/心跳/事件三方共享同一个值),
       观察者(watchdog)只读不猜——否则旧任务的心跳会被误认成新任务还活着。
    3. attempt 之间用 parent_attempt_id 串成链路,才可能算出「这一层是否
       独有贡献」(前序都失败、只有它救回来),否则只能数「某层成功过几次」,
       那会把「本来就会成功」的层算成功臣。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from uuid import uuid4

from logutil import _offline_entry

from .config import RUNTIME_DIR

EVENTS_FILE = RUNTIME_DIR / (
    "events.test.jsonl" if _offline_entry() else "events.jsonl"
)  # 单测/探针不写生产事件面, 沿用 logutil 的 spark.test.log 同一判据

_TASK_ID = ""
_seq = 0


def start_task() -> str:
    """生成本轮 task_id。必须由任务自己调用,watchdog 只读不生成。"""
    global _TASK_ID, _seq
    _TASK_ID = f"{datetime.now():%Y%m%dT%H%M%S}-{os.getpid()}"  # noqa: DTZ005 — 机器本地时
    _seq = 0
    return _TASK_ID


def current_task_id() -> str:
    return _TASK_ID


def attempt_id() -> str:
    """一次「尝试」的标识,同一轮内单调递增。"""
    global _seq
    _seq += 1
    return f"{_TASK_ID or 'adhoc'}:{_seq}"


def emit(
    layer: str,
    action: str,
    result: str,
    *,
    friend: str = "",
    account: str = "",
    verified: bool | None = None,
    attempt: str | None = None,
    parent: str | None = None,
    error_class: str | None = None,
    **extra: object,
) -> None:
    """追加一行事件。字段名刻意与外部评审给的最小集对齐。

    layer:  执行层(discover/identity/sticker/input/send_button/enter/vision/heal/watchdog)
    action: 做了什么(click/send/verify/classify/...)
    result: ok / failed / unknown / skipped
    verified: 是否**验证过**成功(False 与 None 不同: None=没验证)
    """
    rec: dict[str, object] = {
        "event_id": uuid4().hex[:12],
        "task_id": _TASK_ID,
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),  # noqa: DTZ005 — 机器本地时
        "layer": layer,
        "action": action,
        "result": result,
        "friend": friend,
        "account": account,
        "verified": verified,
        "attempt_id": attempt or attempt_id(),
        "parent_attempt_id": parent,
        "error_class": error_class,
    }
    if extra:
        rec.update(extra)
    try:
        EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with EVENTS_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
    except Exception:  # 事件写失败绝不阻断发送
        pass


def layer_stats(rows: list[dict]) -> dict[str, dict[str, int]]:
    """按层统计"贡献账单" —— 四个指标都是外部评审点名要的。

    为什么要 unique_recovery 而不只是"成功次数": 某一层"成功过 N 次"完全可能
    是因为上一层本来就会成功(它只是跟着跑了一遍)。只有「前序全失败、仅它成功」
    才算独有贡献, 这条链路靠 parent_attempt_id 还原。

    false_positive: 同一次 attempt 先报 ok、后续验证又判否 —— 那是谎报, 比失败更坏。
    """
    by_attempt: dict[str, list[dict]] = {}
    for rec in rows:
        aid = str(rec.get("attempt_id") or "")
        if aid:
            by_attempt.setdefault(aid, []).append(rec)

    stats: dict[str, dict[str, int]] = {}

    def bucket(layer: str) -> dict[str, int]:
        return stats.setdefault(
            layer,
            {
                "attempts": 0,
                "verified_success": 0,
                "unique_recovery": 0,
                "false_positive": 0,
            },
        )

    for rec in rows:
        layer = str(rec.get("layer") or "?")
        b = bucket(layer)
        b["attempts"] += 1
        if rec.get("verified") is True:
            b["verified_success"] += 1
            parent = str(rec.get("parent_attempt_id") or "")
            if parent and parent in by_attempt:
                parent_rows = by_attempt[parent]
                if all(
                    r.get("verified") is not True and r.get("result") != "ok"
                    for r in parent_rows
                ):
                    b["unique_recovery"] += 1
        if rec.get("result") == "ok":
            later = by_attempt.get(str(rec.get("attempt_id") or ""), [])
            idx = later.index(rec)
            if any(r.get("verified") is False for r in later[idx + 1 :]):
                b["false_positive"] += 1
    return stats


def read_events(limit: int | None = None) -> list[dict]:
    """读回事件(容错:坏行跳过)。周报/统计脚本用。"""
    out: list[dict] = []
    try:
        with EVENTS_FILE.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    if limit is not None:
        return out[-limit:]
    return out
