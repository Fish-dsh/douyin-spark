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
# Xvfb 起来没起来, systemd 说 active 只是第一层证据; 真正能证明「这个显示号可以连」
# 的是 X server 在 socket 目录里留下的那条 socket。以前只查 systemctl, 于是
# 「unit 处于 active 但 Xvfb 正在崩溃重启」这类状态会被判健康 —— 而有头浏览器
# 连不上 X 就是整晚渲染不出来。单测把这里指到临时目录即可覆盖。
_X11_SOCKET_DIR = Path("/tmp/.X11-unix")

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


_DISPLAY_RE = re.compile(r"^:\d+(\.\d+)?$")
# 与 src/config.py 的 ALLOW_HEADLESS_ENV 同名同义: 桌面调试的逃生口。
# 这里也认它, 否则「preflight 放行的配置, 巡检判失败」——同一条硬门禁两套口径,
# 人只会学会无视巡检。
_ALLOW_HEADLESS_ENV = "SPARK_ALLOW_HEADLESS"


def _headless_problem(cfg: dict) -> str:
    """有头模式硬门禁的判定正文; 通过返回空串。判据与 src/config.py 一致。"""
    headless = cfg.get("headless")
    if headless is not False:
        if os.environ.get(_ALLOW_HEADLESS_ENV) == "1":
            # 逃生口只放行 headless 这一条, display 照查(见下)。
            pass
        else:
            return (
                f"headless={headless!r}; 应为 false"
                "(headless 会被抖音拦截并渲染挂死)"
            )
    display = cfg.get("display")
    if display is None or (isinstance(display, str) and not display.strip()):
        env_display = (os.environ.get("DISPLAY") or "").strip()
        if _DISPLAY_RE.match(env_display):
            return ""
        return (
            "display 未配置, 当前环境也没有可用的 DISPLAY; "
            '应为 display=":99", 或在有桌面的会话里再跑'
        )
    if not isinstance(display, str) or not _DISPLAY_RE.match(display.strip()):
        return f"display={display!r}; 应为 :N 形式(如 \":99\")"
    return ""


def check_headed_config() -> tuple[bool, str]:
    """有头模式配置断言(2026-09-30 事故根因: 板子 config.json 丢 headless/display
    两键 → 实际跑 headless → 渲染挂死 → 全 skip 漏发 10 人)。"""
    # 无 config.json 不再是「跳过」而是**失败**: 跳过等于放行, 而 2026-09-30 事故
    # 正是「配置文件不完整 → 静默跑 headless → 渲染挂死 → 全 skip 漏发」。
    # 没有配置文件就是没有可核验的有头配置, 必须报出来。
    if not CONFIG_FILE.is_file():
        return False, "config.json 不存在, 无法确认有头模式配置(先 cp config.example.json config.json)"
    # 解析失败/非字典同样按**失败**处理。此前的写法是 return True「跳过检查」,
    # 与上面「无 config.json 即失败」自相矛盾: 配置文件坏掉和配置文件没写,
    # 对有头模式而言后果完全一样 —— 都无从确认, 而 spark.py 可能正以 headless
    # 跑着。无法确认 ≠ 通过。
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"config.json 解析失败, 无法确认有头模式配置: {exc}"
    if not isinstance(cfg, dict):
        return False, f"config.json 顶层不是对象({type(cfg).__name__}), 无法确认有头模式配置"
    problem = _headless_problem(cfg)
    if problem:
        return False, f"config.json 有头模式配置缺失/错误: {problem}"
    display = cfg.get("display")
    if isinstance(display, str) and display.strip():
        return True, f"有头模式配置正常 (display={display.strip()})"
    return True, (
        f"有头模式配置正常 (display 用当前环境 DISPLAY="
        f"{(os.environ.get('DISPLAY') or '').strip()})"
    )


def _configured_display() -> tuple[str, str]:
    """实际生效的 display 与它的来源。返回 (显示号, 来源), 拿不到就 ("", "")。

    配置里留空 display 不是「没配置」—— src/config.py 的有头门禁允许这种情况,
    含义是「用当前进程的 DISPLAY」。这里必须用同一套回落逻辑, 否则会出现最坏的
    组合: 配置留空、实际跑在 :0、而 Xvfb 检查去查了一个根本不存在的显示号, 或者
    干脆什么 socket 都不查、只凭 systemctl 说 active 就报 ok。
    """
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return "", ""
    if not isinstance(cfg, dict):
        return "", ""
    display = cfg.get("display")
    if isinstance(display, str) and display.strip():
        return display.strip(), "config"
    env_display = (os.environ.get("DISPLAY") or "").strip()
    if _DISPLAY_RE.match(env_display):
        return env_display, "env"
    return "", ""


def check_xvfb_display() -> tuple[str, str]:
    """Xvfb 常驻服务检查。返回 (状态, 说明), 状态取 "ok" / "fail" / "skip"。

    判据分两层, 都要过:
      1. systemd 说 xvfb.service 是 active —— 这是服务管理面的证据;
      2. 配置的那个显示号在 /tmp/.X11-unix 里真有 socket —— 这才是「有头浏览器
         连得上」的证据。

    只查第 1 层是不够的: unit 处于 active 而 Xvfb 正在崩溃重启时, systemd 依旧
    报 active, 而浏览器连上去就是一个连不上的显示。反过来, 板上把 display 配成
    ":88" 也照样能过旧检查 —— 那台机器上根本没有 :88。

    「查不了」不再当成「没问题」。以前无 systemctl 且无 socket 时直接 return
    True「跳过」, 调用方看到的是和真通过一样的布尔值 —— 一个无从核验的环境
    就被记成了健康。这里把三态分开: 有证据通过才 ok, 有证据不对才 fail,
    其余一律 skip, 由调用方显式打印「未核验」而不是混进「正常」。

    显示号本身也可以来自环境变量: config.json 里 display 留空是有头门禁允许的
    (含义是「用当前进程的 DISPLAY」)。所以这里跟 src/config.py 用同一套回落顺序
    —— 先配置、再环境 —— 并且两边都找不到时返回 skip: 连该查哪个 socket 都不
    知道的情况下, 只看 systemctl 的 active 会把「某个 Xvfb 在跑」误当成
    「浏览器会用上它」。
    """
    if not CONFIG_FILE.is_file():
        return "skip", "config.json 不存在, 未核验 Xvfb(先 cp config.example.json config.json)"
    display, source = _configured_display()
    socket_ok = False
    socket_name = ""
    if display:
        socket_name = "X" + display.lstrip(":").split(".")[0]
        socket_ok = (_X11_SOCKET_DIR / socket_name).exists()
    if not display:
        # 配置留空且环境也没有合法 DISPLAY —— 连「该查哪个 socket」都不知道。
        # 这时 systemctl 说 active 也不能算 ok: 那只能证明某个 Xvfb 在跑,
        # 证明不了浏览器会用上它。
        return "skip", (
            "config.json 的 display 为空, 环境也没有可用的 DISPLAY, "
            "无法确定该查哪个 X socket, 未核验 Xvfb"
            "(部署机应把 display 配成 \":99\")"
        )
    where = f"{display}(来自{source})" if source == "env" else display
    if shutil.which("systemctl") is None:
        if socket_ok:
            return "ok", f"无 systemctl, 但 {_X11_SOCKET_DIR}/{socket_name} 存在"
        return "skip", (
            f"无 systemctl 且 {_X11_SOCKET_DIR}/{socket_name} 不存在, 未核验 Xvfb"
            "(开发机正常现象; 部署机上这项必须能核验)"
        )
    try:
        proc = subprocess.run(
            ["systemctl", "is-active", "xvfb"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:
        return "skip", f"systemctl 调用失败({exc}), 未核验 Xvfb"
    state = (proc.stdout or proc.stderr or "").strip()
    if proc.returncode != 0 or state != "active":
        return (
            "fail",
            (
                f"xvfb.service 状态={state or 'unknown'}, 有头模式将无虚拟显示可用 "
                "(启动: systemctl start xvfb)"
            ),
        )
    if not socket_ok:
        return (
            "fail",
            (
                f"xvfb.service 报 active, 但 display={where} 没有对应 socket "
                f"({_X11_SOCKET_DIR}/{socket_name} 不存在); "
                "多半是 unit 在崩溃重启, 或者 display 配错了显示号"
            ),
        )
    return "ok", f"xvfb.service active (display={where}, socket={socket_name} 有)"


# 注: 早期还有一个「记档对账」检查项, 依赖维护者的私有工作日志工具, 与本项目运行无关,
# 公开版已移除。健康检查只保留与「这一晚到底跑成没有」直接相关的项。

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
    skipped = []
    for fn in (check_state_date, check_log_has_today, check_headed_config):
        ok, msg = fn()
        if not ok:
            failures.append(msg)
    # Xvfb 这一项是三态的: ok / fail / skip。「查不了」不能混进「正常」——
    # 以前它和无 systemctl 的「跳过」共用同一个 True, 结果是无从核验的环境
    # 与真通过输出完全一样。skip 时明确打印「未核验」, 但不算失败(开发机没有
    # systemd 是常态, 判失败会让本机天天红)。
    xvfb_state, xvfb_msg = check_xvfb_display()
    if xvfb_state == "fail":
        failures.append(xvfb_msg)
    elif xvfb_state == "skip":
        skipped.append(xvfb_msg)

    if not failures:
        if not args.quiet:
            if skipped:
                print(
                    "[health] OK: 今日续火花运行正常"
                    f"（但有未核验项: {'; '.join(skipped)}）"
                )
            else:
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
