#!/usr/bin/env python3
"""健康检查: 校验「今日自动续火花是否按预期执行」。

无人值守的静默失败(如 run_cron.sh 丢执行权限、板子断网、登录失效、自愈链未收尾)
会让 spark.log 当日无收尾记录、state.json 日期停留在昨天。本脚本把这些可观测
信号汇总成一次可告警的检查, 建议用 cron 每天定时执行(01:45, 须晚于主任务
`timeout 90m` 的最晚收尾时刻 01:30)。

时间口径: 与 spark.py 一致按「机器本地时」(板子 Asia/Shanghai), 不用 UTC。

检查项(全部通过才算健康):
0. 若主任务进程仍在: 仅当「业务心跳新鲜(≤30 分钟内有业务事件)」才豁免判定;
   进程活着但心跳陈旧 = 疑似卡死, 不再静音告警, 继续走下面的检查
1. runtime/state.json 存在且 date == 今天(本地时)
2. runtime/spark.log 存在、含今天的日期标记, 且当日有「收尾」记录
   (正常完成统计 或 明确无待续好友 —— 而非中途退出/自愈链失败);
   收尾统计中「失败 N 个」(N>0) 同样视为失败(部分失败=漏发好友)

失败时:
- 打印诊断到 stderr
- 若 config.json 配置了 notify_cmd(如 webhook curl), 执行之, 并把失败摘要
  通过环境变量 HEALTH_FAIL_MSG 传给命令; **无论送达与否**都追加一行到
  runtime/alerts.log(append-only 台账)——告警链路自己坏了时, 事后仍查得到报过什么
- 退出码 1; 全部通过退出码 0

注意: 本脚本刻意不写 runtime/spark.log(否则自身的日志会满足日志检查, 形成自证循环)。

用法:
    python src/health_check.py                     # 完整检查
    python src/health_check.py --quiet             # 健康时静默(供 cron 日志干净)
    python src/health_check.py --notify-only-on-change  # 状态翻转时才提醒, 连续失败不重复告警

cron 建议(板子, 实际 crontab 同为 01:45 —— 必须晚于主任务最晚收尾 01:35):
    45 1 * * * /opt/douyin-spark/.venv/bin/python /opt/douyin-spark/src/health_check.py --quiet --notify-only-on-change
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_FILE = BASE_DIR / "runtime" / "state.json"
LOG_FILE = BASE_DIR / "runtime" / "spark.log"
CONFIG_FILE = BASE_DIR / "config.json"
FAIL_MARKER = BASE_DIR / "runtime" / ".health_failed"
PID_FILE = BASE_DIR / "runtime" / ".spark.pid"
HEARTBEAT_FILE = BASE_DIR / "runtime" / "heartbeat.json"
# 抽成模块常量仅为可测: 单测把根目录指到临时区即可验证"活锁/陈旧锁"判定
_PROC_ROOT = Path("/proc")

# spark.py 正常收尾的两类日志标记(均带 [YYYY-MM-DD 前缀)
# 注意: 无待续好友的真实日志是「今日(2026-08-15)没有需要续火花的好友, 程序结束」,
# 日期插在中间, 故标记只用中间稳定子串, 防止与生产格式漂移。
# 收尾行真实格式(多账号串行会带账号名): "==== 账号 {name} 完成: 发送 N 个 / 失败 M 个 ===="。
# 曾按单账号写成 "==== 完成:" → 被账号名隔断 → 永远匹配失败 → 健康检查恒 FAIL,
# 且因 --notify-only-on-change 的去重标记永不翻转, 告警被永久静音(2026-08-31~09-25 实测)。
_COMPLETE_MARK = "完成:"
_NO_PENDING_MARK = "没有需要续火花的好友"
_COMPLETE_RE = re.compile(r"发送\s*(\d+)\s*个\s*/\s*失败\s*(\d+)\s*个")
_ACCOUNT_RE = re.compile(r"账号\s+(\S+)\s+完成:")
_FAIL_NAME_RE = re.compile(r"【(.+?)】处理失败")
# 另外两种合法收尾(多账号下每种都算"该账号跑完了"):
#   无待续: 今日(2026-10-04)没有需要续火花的好友, 账号 main 结束
#   明确跳过: 【账号 main 未配置 account/password 且无登录态, 跳过】
_NO_PENDING_ACCOUNT_RE = re.compile(r"没有需要续火花的好友, 账号\s+(\S+)\s+结束")
_SKIP_ACCOUNT_RE = re.compile(r"【账号\s+(.+?)\s+未配置 account/password")


def _as_int(text: str) -> int:
    """正则捕获组 → int; 捕获组是纯数字必然成功, 包 try 只为消除静态检查的无差别告警。"""
    try:
        return int(text)
    except ValueError:
        return 0


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _configured_accounts() -> list[tuple[str, Path]]:
    """config.json 声明的账号 → state 文件清单; 无 accounts 配置/解析失败返回空表。

    与 config.get_account_state_file 同规则(state_file 字段优先, 缺省
    state_{name}.json)。健康检查要**逐账号**判收尾, 少查一个账号就等于给
    「跑了一半」开绿灯(2026-10-04 实测: 账号 13800000001 只发了 3/8 被杀,
    却因 main 收尾正常而整轮判 OK)。
    """
    files: list[tuple[str, Path]] = []
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(cfg, dict):
            for i, acc in enumerate(cfg.get("accounts") or []):
                if not isinstance(acc, dict):
                    continue
                name = str(acc.get("name", f"account{i}"))
                files.append(
                    (
                        name,
                        STATE_FILE.parent / acc.get("state_file", f"state_{name}.json"),
                    )
                )
    except Exception:
        pass
    return files


def _account_state_files() -> list[tuple[str, Path]]:
    """待检查的 state 文件清单: 多账号配置逐账号, 否则回退默认 state.json。"""
    return _configured_accounts() or [("default", STATE_FILE)]


def check_state_date() -> tuple[bool, str]:
    """校验各账号 state 文件存在且 date == 今天(逐账号, 全部通过才健康)。"""
    today_local = today()
    problems: list[str] = []
    ok_names: list[str] = []
    for name, state_file in _account_state_files():
        if not state_file.is_file():
            problems.append(f"[{name}] state 文件不存在: {state_file}")
            continue
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception as exc:
            problems.append(f"[{name}] state 解析失败: {exc}")
            continue
        day = data.get("date") if isinstance(data, dict) else None
        if day != today_local:
            problems.append(
                f"[{name}] state 日期 {day!r} != 今天 {today_local!r} "
                "(脚本未运行或 state 未更新)"
            )
        else:
            ok_names.append(name)
    if problems:
        return False, "; ".join(problems)
    return True, f"state 已更新到 {today_local} (账号: {', '.join(ok_names)})"


def _today_outcomes(
    lines: list[str],
) -> tuple[dict[str, tuple[int, int]], set[str], set[str]]:
    """按账号归集当日收尾结局 → (完成统计, 无待续账号, 明确跳过账号)。"""
    stats: dict[str, tuple[int, int]] = {}
    no_pending: set[str] = set()
    skipped: set[str] = set()
    for ln in lines:
        if _COMPLETE_MARK in ln:
            m = _COMPLETE_RE.search(ln)
            if m:
                acct = _ACCOUNT_RE.search(ln)
                stats[acct.group(1) if acct else ""] = (
                    _as_int(m.group(1)),
                    _as_int(m.group(2)),
                )
                continue
        m = _NO_PENDING_ACCOUNT_RE.search(ln)
        if m:
            no_pending.add(m.group(1))
            continue
        m = _SKIP_ACCOUNT_RE.search(ln)
        if m:
            skipped.add(m.group(1))
    return stats, no_pending, skipped


def _failed_friend_names(lines: list[str]) -> list[str]:
    names: list[str] = []
    for ln in lines:
        m = _FAIL_NAME_RE.search(ln)
        if m and m.group(1) not in names:
            names.append(m.group(1))
    return names


def _rotated_log_files() -> list[Path]:
    """spark.log 的同目录轮转副本(.1 / .2.gz ...), 新的在前。

    板子的 BOARD-truncate-logs 每 15 分钟看一次 /var/log 用量(zram 50M), 超过
    75% 就执行 logrotate --force /etc/logrotate.conf —— 这会连带把本项目写在本目录
    的 runtime/spark.log 一起切走(2026-11-01 15:15 实例: 当日两个账号 00:24 都已
    收尾, 白天被强制轮转, live 文件归零)。只读 live 文件就会把「日志被轮转」误判成
    「今日没运行」, 对用户发假告警。故按 mtime 倒序把所有轮转副本一并纳入。
    """
    candidates = []
    for q in LOG_FILE.parent.glob(LOG_FILE.name + ".*"):
        if q.name.endswith("-journal") or not q.is_file():
            continue
        candidates.append(q)
    candidates.sort(key=lambda q: q.stat().st_mtime, reverse=True)
    return candidates


def _read_log_text(path: Path) -> str:
    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
                return fh.read()
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def check_log_has_today() -> tuple[bool, str]:
    if not LOG_FILE.is_file() and not _rotated_log_files():
        return False, f"spark.log 不存在: {LOG_FILE}"
    today_local = today()
    lines: list[str] = []
    seen: set[str] = set()
    for path in [LOG_FILE, *_rotated_log_files()]:
        for ln in _read_log_text(path).splitlines():
            if ln.startswith(f"[{today_local}") and ln not in seen:
                seen.add(ln)
                lines.append(ln)
    if not lines:
        return False, f"spark.log 无 {today_local} 的运行记录"

    stats, no_pending, skipped = _today_outcomes(lines)

    # ── 逐账号收尾(2026-10-04 事故) ────────────────────────────────
    # 老口径只统计**出现过**完成行的账号: 账号 A 正常收尾、账号 B 进程被
    # timeout 杀在半路 → stats 里只剩 A → 汇总"失败 0"照样判健康,
    # 于是 5 个好友漏发整轮静默。改为按 config 声明的账号逐个点名,
    # 缺谁谁就是没跑完; 被跳过的账号同样算没续上火花(登录态失效场景)。
    configured = [name for name, _ in _configured_accounts()]
    finished = set(stats) | no_pending | skipped
    if configured:
        unfinished = [n for n in configured if n not in finished]
        if unfinished:
            done = ", ".join(sorted(finished)) or "无"
            return False, (
                f"spark.log 今日以下账号无收尾记录(疑似中途失败/进程被杀): "
                f"{', '.join(unfinished)} (已收尾: {done})"
            )
        if skipped:
            # 跳过 = 该账号今日根本没续火花(典型: storage 登录态失效),
            # 绝不能与"正常收尾"混为一谈地判健康。
            return False, (
                f"spark.log 今日账号 {', '.join(sorted(skipped))} 被跳过"
                "(未配置 account/password 且无登录态), 今日未续火花"
            )
        scope = [n for n in configured if n in stats]
    else:
        # 无 accounts 配置(单账号/纯模板): 退回"当日有任一收尾即算"的旧口径
        scope = list(stats)

    if scope:
        sent = sum(stats[n][0] for n in scope)
        failed = sum(stats[n][1] for n in scope)
        if failed > 0:
            names = _failed_friend_names(lines)
            detail = f"收尾统计: 发送 {sent} / 失败 {failed}"
            if names:
                shown = ", ".join(names[:5]) + ("..." if len(names) > 5 else "")
                detail += f" (漏发: {shown})"
            return False, f"spark.log 今日{detail}, 存在漏发好友"
        return True, f"spark.log 今日收尾正常 (发送 {sent} / 失败 0)"

    tail = " ".join(lines[-20:])
    if _NO_PENDING_MARK in tail:
        return True, "spark.log 今日无待续好友, 正常收尾"
    return False, "spark.log 今日有记录但未见收尾(完成统计/无待续), 疑似中途失败"


def _live_run_pid() -> int | None:
    """主任务是否仍在运行 → 返回其 PID; 未运行(含陈旧锁)返回 None。

    用于让健康检查在"任务还没跑完"时**不判定**而非误判失败——判定过早会产生
    假告警(告警疲劳会让人从此忽略真告警)。陈旧锁(PID 已死或已被复用给别的进程)
    一律不算运行中, 否则一个死锁文件能让告警永久静音。
    """
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    try:
        # 只认确实在跑 spark.py 的进程: PID 复用给无关进程时不能算"运行中"。
        # **必须是 `spark.py` 而不是 `spark`** —— 项目路径本身就是 /opt/douyin-spark，
        # 一个 argv 里带该目录的残留进程或任何人手工跑的验证脚本
        # 都会命中裸 `spark`，于是健康检查看见"主任务还在跑"直接退 0，
        # 而真实的主任务早就死了 —— 判据松一格就等于整晚没人判定。
        cmdline = (
            (_PROC_ROOT / str(pid) / "cmdline").read_bytes().decode("utf-8", "replace")
        )
    except OSError:
        return None
    return pid if "spark.py" in cmdline else None


def _heartbeat_status(now_ts: float | None = None) -> tuple:
    """读业务心跳 → (是否新鲜, 供告警用的位置描述)。

    心跳由主流程在每个业务事件上敲(runtime/heartbeat.json)。返回的「新鲜」
    用于把豁免判据从「进程活着」升级为「进程活着且业务在推进」; 位置描述
    (iso/stage/account/friend)用于让挂死告警直接指出卡在哪一步。
    无心跳文件(老版本/从未跑过)按不新鲜处理, 但位置注明缺失, 由既有检查项
    兜底判定 —— 不因心跳缺失单独判失败(判据松紧: 只收紧豁免, 不新增失败源)。
    """
    try:
        data = json.loads(HEARTBEAT_FILE.read_text(encoding="utf-8"))
        age_min = (now_ts if now_ts is not None else time.time()) - float(data["ts"])
        age_min /= 60
    except (OSError, ValueError, KeyError, TypeError):
        return False, "heartbeat.json 缺失/不可读"
    where = "/".join(
        str(data.get(k) or "-") for k in ("iso", "stage", "account", "friend")
    )
    return age_min <= _HEARTBEAT_FRESH_MIN, f"{where} (心跳 {age_min:.0f} 分钟前)"


def load_notify_cmd() -> str | None:
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    cmd = cfg.get("notify_cmd") if isinstance(cfg, dict) else None
    return cmd if isinstance(cmd, str) and cmd.strip() else None


def alerts_log_path() -> Path:
    """告警台账路径。跟着 FAIL_MARKER 的目录走: 单测把标记换到临时区, 台账自然跟着走。"""
    return FAIL_MARKER.parent / "alerts.log"


def append_alert_ledger(summary: str, delivered: bool) -> None:
    """把每一次告警(连同送达与否)追加进台账; 写失败绝不影响退出码。

    无人值守最怕的不是坏掉, 是**坏掉且没留下证据**: 告警链路自己失效时, 过去只剩
    .health_failed 一个布尔标记, 失败文本从未落盘, 事后连「昨晚到底报了什么」都
    查不到。台账是 append-only 的事故时间线, 供月度回看。
    """
    try:
        line = (
            f"[{datetime.now().isoformat(timespec='seconds')}] "
            f"delivered={'1' if delivered else '0'} {summary}\n"
        )
        path = alerts_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception as exc:
        print(f"[health] 告警台账写入失败(忽略): {exc}", file=sys.stderr)


# curl 默认对 4xx/5xx 也返回 0: 不加 -f/--fail 时「退出码 0」根本不证明 webhook 收下了。
_CURL_FAIL_RE = re.compile(r"(?:^|\s)-[A-Za-z]*f[A-Za-z]*(?=\s|$)|--fail[A-Za-z-]*")


def _cmd_proves_delivery(cmd: str) -> bool:
    """非 curl 命令按退出码判定; curl 必须显式带 -f(或 -s 组合里的 f)才算证据。"""
    if "curl" not in cmd:
        return True
    return bool(_CURL_FAIL_RE.search(cmd))


def notify(fail_summary: str) -> bool:
    """执行 notify_cmd 外发告警; 返回 True = **有证据**确认送达(命令存在、退出码 0、且能证明 HTTP 成功)。"""
    cmd = load_notify_cmd()
    if not cmd:
        print(
            "[health] 未配置 notify_cmd, 仅以退出码告警(可加 cron mail 或 webhook)",
            file=sys.stderr,
        )
        return False
    unproven = not _cmd_proves_delivery(cmd)
    env = {**os.environ, "HEALTH_FAIL_MSG": fail_summary}
    try:
        proc = subprocess.run(["sh", "-c", cmd], env=env, timeout=60, check=False)
    except Exception as exc:
        print(f"[health] notify_cmd 执行失败: {exc}", file=sys.stderr)
        return False
    if proc.returncode != 0:
        print(
            f"[health] notify_cmd 返回非 0({proc.returncode}), 告警可能未送达",
            file=sys.stderr,
        )
        return False
    if unproven:
        # **照发不误**, 只是不信这个退出码: curl 不带 -f 时 webhook 返回 4xx/5xx 也是 0。
        # 若因为「无法证实」就干脆不发, 等于把修一个假绿 bug 换成制造一个静默 bug ——
        # 板端 config.json 现为 `curl -s -X POST ...`(无 -f), 这条分支就是它的日常路径。
        # 按未送达处理的效果是「不写失败标记 → 每晚重复告警」, 宁可重复, 不可静音。
        print(
            "[health] 已执行 notify_cmd 但退出码不证明送达(curl 缺 -f/--fail, "
            "HTTP 4xx/5xx 同样返回 0) → 按未送达处理, 下次仍会告警。"
            "建议把命令改成 curl -fsS ...(改 config.json 需人工确认)",
            file=sys.stderr,
        )
        return False
    print("[health] notify_cmd 已确认送达")
    return True


def check_headed_config() -> tuple[bool, str]:
    """有头模式配置断言(2026-09-30 事故根因: 板子 config.json 丢 headless/display
    两键 → 实际跑 headless → 渲染挂死 → 全 skip 漏发 10 人)。"""
    if not CONFIG_FILE.is_file():
        return True, "config.json 不存在, 跳过有头模式配置检查"
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return True, "config.json 解析失败, 跳过有头模式配置检查"
    if not isinstance(cfg, dict):
        return True, "config.json 非字典, 跳过有头模式配置检查"
    headless = cfg.get("headless")
    display = cfg.get("display")
    if headless is False and display:
        return True, f"有头模式配置正常 (display={display})"
    return (
        False,
        (
            f"config.json 有头模式配置缺失/错误: headless={headless!r}, display={display!r}; "
            '应为 headless=false 且 display=":99" (headless 会被抖音拦截并渲染挂死)'
        ),
    )


def check_xvfb_display() -> tuple[bool, str]:
    """Xvfb 常驻服务检查; 有 config.json(板子语境)才检查, 无 systemd 环境跳过。"""
    if not CONFIG_FILE.is_file():
        return True, "config.json 不存在, 跳过 Xvfb 检查"
    if shutil.which("systemctl") is None:
        return True, "无 systemctl, 跳过 Xvfb 检查"
    try:
        proc = subprocess.run(
            ["systemctl", "is-active", "xvfb"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:
        return True, f"systemctl 检查失败({exc}), 跳过 Xvfb 检查"
    state = (proc.stdout or proc.stderr or "").strip()
    if proc.returncode == 0 and state == "active":
        return True, "xvfb.service active"
    return (
        False,
        (
            f"xvfb.service 状态={state or 'unknown'}, 有头模式将无虚拟显示可用 "
            "(启动: systemctl start xvfb)"
        ),
    )


def check_worklog_drift() -> tuple[bool, str]:
    """WORKLOG 记档对账(默认关, 设 `HEALTH_WORKLOG_DRIFT=1` 才生效)。

    板子故意不开: 自愈链路会自己 `git commit` 基线, 按天对账会天天误报 → 告警疲劳。
    开发机要防的正是「AI 改完不记档」, 可在 shell 里临时导出该变量后跑本脚本。
    """
    if os.environ.get("HEALTH_WORKLOG_DRIFT") != "1":
        return True, "记档对账未启用(HEALTH_WORKLOG_DRIFT=1 开启)"
    wlog = BASE_DIR / "tools" / "wlog.py"
    if not wlog.is_file():
        return True, f"未找到 {wlog.name}, 跳过记档对账"
    try:
        proc = subprocess.run(
            [sys.executable, str(wlog), "check"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception as exc:
        return True, f"记档对账执行失败({exc}), 跳过"
    if proc.returncode == 0:
        return True, "WORKLOG 记档无欠账"
    detail = (proc.stderr or proc.stdout or "").strip().replace("\n", " | ")
    return False, f"WORKLOG 有未记档的状态类提交: {detail[:300]}"


# 主任务正常运行时长上限(90 分钟 timeout + 抖动 + 余量)。超过即视为挂死, 不再豁免判定。
_MAX_LEGIT_RUN_MIN = 100
# 心跳新鲜度阈值: 热路径业务事件间隔正常 <5 分钟(好友间停顿≤15s + 单好友≤3分钟),
# 保守取 30 分钟, 只拦「进程活着但卡死」, 不误伤慢设备。
_HEARTBEAT_FRESH_MIN = 30


def main() -> int:
    parser = argparse.ArgumentParser(description="抖音自动续火花健康检查")
    parser.add_argument("--quiet", action="store_true", help="健康时静默输出")
    parser.add_argument(
        "--notify-only-on-change",
        action="store_true",
        help="状态翻转时才提醒(连续失败不重复告警), 用 runtime/.health_failed 标记",
    )
    args = parser.parse_args()

    # 任务还没跑完就别急着下结论: 判定过早 = 假告警, 而假告警会让人从此忽略真告警。
    # 正常路径(cron 01:45 + timeout 90m → 最晚 01:30 结束)根本走不到这里,
    # 本项只兜住人工补跑/超时未触发这类时序交错; 陈旧锁不算运行中。
    # 豁免判据(2026-10-06 升级): 「进程活着」→「进程活着且业务心跳新鲜」——
    # 活着但卡死的进程会持有锁、静音整晚告警, 心跳不新鲜即视同挂死继续判定。
    running = _live_run_pid()
    if running is not None:
        age_min = None
        try:
            age_min = (time.time() - PID_FILE.stat().st_mtime) / 60
        except OSError:
            pass
        fresh, where = _heartbeat_status()
        if age_min is not None and fresh and age_min < _MAX_LEGIT_RUN_MIN:
            print(
                f"[health] 主任务运行中(PID {running})且业务心跳新鲜, 本次不判定也不改失败标记"
                f" (卡于: {where})",
                file=sys.stderr,
            )
            return 0
        print(
            f"[health] 主任务(PID {running})存活但豁免条件不满足"
            f"(进程龄 {f'{age_min:.0f}' if age_min is not None else '?'} 分钟"
            f"/上限 {_MAX_LEGIT_RUN_MIN}, 心跳{'新鲜' if fresh else f'不新鲜: {where}'}),"
            " 视为疑似挂死, 继续判定",
            file=sys.stderr,
        )

    failures = []
    for fn in (
        check_state_date,
        check_log_has_today,
        check_headed_config,
        check_xvfb_display,
        check_worklog_drift,
    ):
        ok, msg = fn()
        if not ok:
            failures.append(msg)

    if not failures:
        if not args.quiet:
            print("[health] OK: 今日续火花运行正常")
        if FAIL_MARKER.is_file():
            try:
                FAIL_MARKER.unlink()
                print("[health] 已清除失败标记(恢复健康)")
                append_alert_ledger("RECOVERED 失败标记已清除(今日检查通过)", True)
            except Exception:
                pass
        return 0

    summary = "; ".join(failures)
    print(f"[health] FAIL: {summary}", file=sys.stderr)

    if args.notify_only_on_change and FAIL_MARKER.is_file():
        print("[health] 连续失败且已提醒过, 本次跳过提醒", file=sys.stderr)
        return 1

    delivered = notify(summary)
    append_alert_ledger(summary, delivered)
    if delivered:
        try:
            FAIL_MARKER.write_text(datetime.now().isoformat(), encoding="utf-8")
        except Exception:
            pass
    else:
        print(
            "[health] 告警未确认送达, 不标记已提醒, 下次继续尝试",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
