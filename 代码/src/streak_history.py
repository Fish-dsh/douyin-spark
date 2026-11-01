"""火花续期历史记录模块。

用于追踪每日火花续期发现/处理情况，并与历史比对检测漏发。

核心功能:
  - save_daily_record: 每次运行后保存今日发现/发送/失败列表
  - load_daily_record: 加载历史某日的记录
  - check_streak_history: 比对今日发现与昨日发送，返回漏发好友列表
  - trigger_if_gap_detected: 若发现异常触发本地恢复/告警
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from logutil import log

HISTORY_FILE = (
    Path(__file__).resolve().parent.parent / "runtime" / "streak_history.json"
)


def _local_now() -> datetime:
    """机器本地时(与 spark.py/health_check.py 时间口径一致, 板子 Asia/Shanghai)。

    刻意不带 tz: 全项目按「机器本地时」运行, 统一在此处理以通过 DTZ005。
    """
    return datetime.now().astimezone()


# P5 覆盖对照: per-account 自愈冷却 24h
_COVERAGE_COOLDOWN_SEC = 86400


def _coverage_cooldown_file(account: str | None) -> Path:
    return HISTORY_FILE.parent / f".coverage_heal_{account or 'default'}"


def coverage_heal_on_cooldown(account: str | None) -> bool:
    """覆盖缺失自愈是否处于 24h 冷却期(per-account)。"""
    f = _coverage_cooldown_file(account)
    if not f.is_file():
        return False
    try:
        ts = float(f.read_text(encoding="utf-8").strip() or 0)
    except Exception:
        return False
    return (time.time() - ts) < _COVERAGE_COOLDOWN_SEC


def record_coverage_heal(account: str | None) -> None:
    """记录覆盖缺失自愈触发时间, 开启 24h 冷却。"""
    f = _coverage_cooldown_file(account)
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(str(time.time()), encoding="utf-8")
    except Exception as exc:
        log(f"[streak-history] 写入覆盖自愈冷却标记失败: {exc}")


def recent_renewed_friends(account: str | None, days: int = 3) -> list[str]:
    """近 N 天(不含今日)「发现且成功续上」的好友名单。

    只取 discovered ∩ sent: 成功续上的火花理应持续存在, 今日发现列表应再次
    出现这些好友。只 discovered 未 sent 的(昨日已漏发)火花可能已自然断掉,
    纳入会造成无法挽回的误报, 故排除。
    """
    history = load_history(account)
    today = _local_now().strftime("%Y-%m-%d")
    cutoff = (_local_now() - timedelta(days=days)).strftime("%Y-%m-%d")
    names: list[str] = []
    for d, rec in sorted(history.items()):
        if d >= today or d < cutoff or not isinstance(rec, dict):
            continue
        sent = set(rec.get("sent", []) or [])
        for n in rec.get("discovered", []) or []:
            if n in sent and n not in names:
                names.append(n)
    return names


def _is_same_person(old: str, candidates: set[str]) -> bool:
    """改名漂移判别: 历史名与今日任一名字是否为同一人。

    抖音昵称会被对方自己改(2026-10-04『我是个英语天才』→ 10-05『叶文婧是个英语
    天才』), 而本页会话名是唯一的, 改名前后互为前/后缀或子串。精确比对会把改名
    当成「列表漏检」→ 触发覆盖门禁把整账号发送熔断(2026-10-07 事故)。
    最短 4 字门槛: 短名(如 2~3 字)共用前后缀的概率高, 宁可不合并也不误合并。
    """
    if old in candidates:
        return True
    if len(old) < 4:
        return False
    for c in candidates:
        if len(c) < 4:
            continue
        if old in c or c in old:
            return True
        if old[-4:] == c[-4:] or old[:4] == c[:4]:
            return True
    return False


def check_recent_coverage(account: str | None, today_seen: list[str]) -> list[str]:
    """发现覆盖对照: 近3天已续好友应出现在今日发现(含已发送)名单中。

    today_seen 应传入 今日 pending + 今日已发送(state.sent), 已发送过的好友
    不算缺失。返回缺失好友列表(空 = 无缺失)。
    """
    expected = recent_renewed_friends(account)
    if not expected:
        return []
    seen = set(today_seen)
    missing = [n for n in expected if not _is_same_person(n, seen)]
    if missing:
        log(
            f"[streak-history][warn] 发现覆盖缺失: 近3天已续好友 "
            f"{missing} 未出现在今日发现列表(疑似列表漏检, 参考 2026-09-29 事故)"
        )
    return missing


def _history_file(account: str | None = None) -> Path:
    """获取指定账号的历史记录文件路径。"""
    if account is None:
        return HISTORY_FILE
    return HISTORY_FILE.parent / f"streak_history_{account}.json"


def load_history(account: str | None = None) -> dict[str, Any]:
    """加载历史记录(按日期倒序排列)。"""
    hf = _history_file(account)
    if not hf.is_file():
        return {}
    try:
        data = json.loads(hf.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception as exc:
        log(f"[streak-history] 读取历史记录失败: {exc}")
    return {}


def save_daily_record(
    account: str | None,
    discovered: list[str],
    sent: list[str],
    failed: list[str],
) -> None:
    """保存今日的火花续期记录。

    Args:
        account:  账号名(None 表示默认账号)
        discovered: 发现的待续好友列表
        sent:       成功发送的好友列表
        failed:     发送失败的好友列表
    """
    today = _local_now().strftime("%Y-%m-%d")
    history = load_history(account)

    # 若今日已有记录则追加(避免重复写入)
    if today in history:
        existing = history[today]
        existing["discovered"] = discovered
        existing["sent"] = sent
        existing["failed"] = failed
        existing["updated_at"] = _local_now().isoformat()
    else:
        history[today] = {
            "date": today,
            "discovered": discovered,
            "sent": sent,
            "failed": failed,
            "created_at": _local_now().isoformat(),
        }

    # 只保留最近 30 天
    cutoff = (_local_now() - timedelta(days=30)).strftime("%Y-%m-%d")
    history = {k: v for k, v in history.items() if k >= cutoff}

    hf = _history_file(account)
    hf.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp = hf.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(str(tmp), str(hf))
        log(
            f"[streak-history] 今日记录已保存: 发现{len(discovered)}个 "
            f"发送{len(sent)}个 失败{len(failed)}个"
        )
    except Exception as exc:
        log(f"[streak-history] 保存历史记录失败: {exc}")


def get_latest_record(account: str | None = None) -> dict[str, Any] | None:
    """获取最新的火花续期记录。"""
    history = load_history(account)
    if not history:
        return None
    # 取最新的日期(键按字符串排序即可, 因为格式是 YYYY-MM-DD)
    latest_date = max(history.keys())
    return history.get(latest_date)


def get_previous_record(account: str | None = None) -> dict[str, Any] | None:
    """获取最近一次的火花续期记录(跳过最新那条, 用于比对)。"""
    history = load_history(account)
    if len(history) < 2:
        return None
    dates = sorted(history.keys(), reverse=True)
    return history.get(dates[1])


def check_streak_history(account: str | None = None) -> tuple[list[str], str]:
    """比对「昨日待续名单」与「昨日实际发送」, 检测昨天的漏发。

    注意比对口径: 必须是昨天的 discovered − 昨天的 sent。用今日名单去比昨日发送
    会把新形成的火花误判成漏发(见函数内注释)。

    Returns:
        (missing_friends, detail):
        - missing_friends: 昨日该续但没发出去的好友列表(空 = 昨天没漏)
        - detail: 详细信息字符串
    """
    today_record = get_latest_record(account)
    prev_record = get_previous_record(account)

    if not today_record:
        return [], "无今日记录, 无法比对"
    if not prev_record:
        return [], "无昨日记录, 跳过比对"

    today_sent = set(today_record.get("sent", []))
    today_discovered = set(today_record.get("discovered", []))
    prev_discovered = set(prev_record.get("discovered", []))
    prev_sent = set(prev_record.get("sent", []))

    # 判据是**昨天自己有没有发漏**, 不是"今天新出现的火花昨天发过没"。
    # 老写法 `today_discovered - prev_sent` 会把「今天新形成的火花」(昨天压根不在
    # 待续名单里, 自然也不在昨天的 sent 里)一律判成漏发 → 每天必然误报一次,
    # 每次都白烧一整轮旧 AI 自愈(2026-10-04: 丹/依旧/再過一個世紀 三个新火花触发
    # streak_gap, 叠加引擎 429 共烧掉 16 分钟, 把账号 13800000001 挤出发送窗口)。
    # 昨天的 discovered(待续名单) − 昨天的 sent(实际发出) 才是真漏发。
    missing = list(prev_discovered - prev_sent)
    detail_parts = [
        f"今日发现{len(today_discovered)}个, 已发送{len(today_sent)}个",
        f"昨日发现{len(prev_discovered)}个, 发送{len(prev_sent)}个",
    ]
    if missing:
        detail_parts.append(f"昨日漏发:{', '.join(missing)}")
    else:
        detail_parts.append("无漏发")

    return missing, ", ".join(detail_parts)


def trigger_if_gap_detected(
    account: str | None = None,
    self_heal_fn=None,
) -> bool:
    """检测历史记录差异并触发本地恢复判定(漏发无本地手段 → 告警)。

    Args:
        account:       账号名(None 表示默认账号)
        self_heal_fn:  self_heal 函数(默认从 self_heal 模块导入)

    Returns:
        True 如果触发了自愈, False 否则
    """
    missing, detail = check_streak_history(account)

    if not missing:
        log(f"[streak-history] {detail}, 无需自愈")
        return False

    log(f"[streak-history] 检测到异常! {detail}")

    if self_heal_fn is None:
        from .self_heal import self_heal as _self_heal

        self_heal_fn = _self_heal

    if self_heal_fn:
        log(
            f"[streak-history] 触发本地恢复策略: 昨日 {len(missing)} 个待续好友未发出 "
            f"({', '.join(missing)})"
        )
        try:
            # 注意: self_heal 签名无 account 参数, 此处不可传 account=account
            # (旧写法 TypeError 被 except 吞掉, 自愈从未真正触发)
            self_heal_fn(
                "streak_gap",
                f"昨日 {len(missing)} 个待续好友未发送(账号 {account}): "
                f"{', '.join(missing)}",
            )
            return True
        except Exception as exc:
            log(f"[streak-history] 触发自愈失败: {exc}")
    else:
        log("[streak-history] self_heal 不可用, 仅记录告警")

    return False
