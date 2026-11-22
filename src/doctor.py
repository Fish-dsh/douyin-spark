"""spark.py --doctor —— 今晚到底能不能跑。

`--status` 回答「昨晚成了吗」, 本模块回答「现在能不能跑」。方向相反: 前者看结果,
后者看前提 —— 配置 / 登录态 / 定时 / 显示 / 磁盘 / 单实例锁。之所以单列, 是因为
「整晚没跑」和「跑了但没发出去」的排查路径完全不同, 而它们的共同起点都是
「先看看哪一项不对」, 不该靠人(或 Agent)逐条想起来查。

只读纪律(同 status): 不写 spark.log(会污染 01:45 巡检的「当日收尾」判定), 不改状态
文件, 不起浏览器。所以它可以被反复调用。

退出码: 0 = 没有 fail(warn 不阻断当夜运行) / 1 = 有 fail(照着 fix 那行做)。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path

from . import runtime_lock
from .config import BASE_DIR, CONFIG_FILE, RUNTIME_DIR

# cookie 37 天过期, 抖音还会主动踢登录态; 30 天没落盘就该人肉复查一次。
# 2026-11-01 那次「未登录」就是被踢, 不是超期, 所以这只是预警线不是硬线。
_STORAGE_STALE_DAYS = 30
# 截图与日志会缓慢涨; 低于这个余量时 pip/浏览器都可能写失败。
_DISK_WARN_GB = 0.2
# 窗口判定、state 日期、日志时间戳全按机器本地时 → 偏移错了整晚错位。
_TZ_OFFSET = timedelta(hours=8)
# 只读命令(crontab -l)的兜底超时。写成命名常量而不是字面量: tests/test_perf.py
# 禁止 src/ 里出现裸 timeout=<数字>(perf.py 之外), 而这里等的是本地命令不是页面,
# 没法定成页面档位。
_CMD_TIMEOUT_SEC = 10

_OK, _WARN, _FAIL, _SKIP = "ok", "warn", "fail", "skip"
_TAG = {_OK: "[ok]  ", _WARN: "[warn]", _FAIL: "[fix] ", _SKIP: "[--]  "}


@dataclass
class Check:
    """一项体检结果。fix 非空时必须是一条能直接照做的动作, 不是解释。"""

    name: str
    level: str
    message: str
    fix: str = ""


def _copy_hint() -> str:
    if sys.platform == "win32":
        return "copy config.example.json config.json"
    return "cp config.example.json config.json"


def _load_raw() -> tuple[dict | None, str]:
    """读 config.json(含一层 extends), 只当纯文本 —— 不调 load_config, 它会写日志。"""
    try:
        raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, f"找不到 {CONFIG_FILE}"
    except Exception as exc:
        return None, f"{CONFIG_FILE.name} 解析失败: {exc}"
    if not isinstance(raw, dict):
        return None, f"{CONFIG_FILE.name} 顶层不是 JSON 对象"
    base = raw.pop("extends", None)
    if isinstance(base, str) and base.strip():
        try:
            merged = json.loads(
                (CONFIG_FILE.parent / base.strip()).read_text(encoding="utf-8")
            )
        except Exception:
            merged = None
        if isinstance(merged, dict):
            merged.update(raw)
            raw = merged
    return raw, ""


def _window_of(raw: dict) -> tuple[str, str]:
    win = raw.get("send_window")
    if not isinstance(win, dict):
        return "23:30", "00:30"
    return str(win.get("start") or "23:30"), str(win.get("end") or "00:30")


def _valid_clock(text: str) -> bool:
    try:
        datetime.strptime(text, "%H:%M")
    except ValueError:
        return False
    return True


def _check_config(raw: dict) -> list[Check]:
    out: list[Check] = []
    accounts = raw.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        # 2026-11-02 断火一整晚就是这个形状: 配置里没有账号, 跑完 rc=0 什么都不发。
        out.append(
            Check(
                "配置",
                _FAIL,
                f"{CONFIG_FILE.name} 里 accounts 是空的, 今晚会「成功」发 0 个人",
                "把抖音账号填进 accounts(参考 config.example.json)",
            )
        )
    else:
        names = [str(a.get("name", "?")) for a in accounts if isinstance(a, dict)]
        out.append(Check("配置", _OK, f"{len(accounts)} 个账号({', '.join(names)})"))

    start, end = _window_of(raw)
    if not (_valid_clock(start) and _valid_clock(end)):
        # in_window 对坏格式一律按「整天」放行 —— 比不跑更危险(白天也可能开跑)。
        out.append(
            Check(
                "发送窗口",
                _WARN,
                f"{start}-{end} 不是合法时刻, 会被当成整天放行",
                '把 send_window 改成 "HH:MM" 两个字段',
            )
        )
    else:
        out.append(Check("发送窗口", _OK, f"每天 {start}-{end}"))

    profile = str(raw.get("perf_profile") or "").strip().lower()
    if profile and profile not in ("low", "mid", "high"):
        out.append(
            Check(
                "档位",
                _WARN,
                f"perf_profile={profile!r} 不认识, 运行时回落到 low",
                '改成 "low" / "mid" / "high"',
            )
        )
    else:
        out.append(Check("档位", _OK, profile or "未配(默认 low)"))
    return out


def _storage_path(acc: dict) -> Path:
    name = str(acc.get("name", "?"))
    raw = str(acc.get("storage_file") or f"storage_{name}.json")
    path = Path(raw)
    return path if path.is_absolute() else RUNTIME_DIR / path


def _check_login(raw: dict, now: datetime) -> list[Check]:
    accounts = [a for a in (raw.get("accounts") or []) if isinstance(a, dict)]
    if not accounts:
        return []
    out: list[Check] = []
    for acc in accounts:
        name = str(acc.get("name", "?"))
        path = _storage_path(acc)
        try:
            stat = path.stat()
        except OSError:
            out.append(
                Check(
                    f"登录态 {name}",
                    _FAIL,
                    f"没有 {path.name}, 今晚这一轮会直接判未登录",
                    f"python login.py --manual 扫码生成, 再放到 {RUNTIME_DIR}",
                )
            )
            continue
        if stat.st_size < 1024:
            out.append(
                Check(
                    f"登录态 {name}",
                    _FAIL,
                    f"{path.name} 只有 {stat.st_size} 字节, 基本是坏文件",
                    "重新扫码生成",
                )
            )
            continue
        days = (now.timestamp() - stat.st_mtime) / 86400
        if days > _STORAGE_STALE_DAYS:
            out.append(
                Check(
                    f"登录态 {name}",
                    _WARN,
                    f"{path.name} 已经 {days:.0f} 天没更新(cookie 37 天过期, 也可能被踢)",
                    "有头环境重跑 python login.py 扫码",
                )
            )
        else:
            out.append(Check(f"登录态 {name}", _OK, f"{path.name} {days:.0f} 天前更新"))
    return out


def _cron_texts() -> dict[str, str]:
    """把能找到的 crontab 原文都收上来。

    板子上 `ssh orangepi` 落地是 root, 而 cron 装在 orangepi 用户下 —— 只看
    `crontab -l` 会误判「没装」。所以 root 直接读 spool 目录, 一次看全。
    """
    out: dict[str, str] = {}
    cmd = shutil.which("crontab")
    if cmd:
        try:
            proc = subprocess.run(
                [cmd, "-l"],
                capture_output=True,
                text=True,
                timeout=_CMD_TIMEOUT_SEC,
                check=False,
            )
            if (proc.stdout or "").strip():
                out["crontab -l"] = proc.stdout
        except Exception:
            pass
    for base in ("/var/spool/cron/crontabs", "/var/spool/cron", "/etc/cron.d"):
        try:
            entries = list(Path(base).iterdir())
        except OSError:
            continue
        for path in entries:
            if path.name.startswith(".") or not path.is_file():
                continue
            try:
                out.setdefault(
                    str(path), path.read_text(encoding="utf-8", errors="replace")
                )
            except OSError:
                continue
    return out


def _check_cron() -> Check:
    if sys.platform == "win32":
        return Check("定时", _SKIP, "Windows 没有 cron(本机只做开发演练)", "")
    texts = _cron_texts()
    hits = [where for where, text in texts.items() if "run_cron" in text]
    if hits:
        return Check("定时", _OK, f"{hits[0]} 里有 run_cron.sh")
    if not texts:
        return Check(
            "定时",
            _SKIP,
            "这台机器没有 cron, 也没有 crontab 命令",
            f"用外部调度器每天 00:00 执行 {BASE_DIR / 'run_cron.sh'}",
        )
    return Check(
        "定时",
        _FAIL,
        f"{len(texts)} 处 crontab 里都没有 run_cron.sh —— 今晚不会有人叫醒它",
        f"crontab -e 加一行: 0 0 * * * {BASE_DIR / 'run_cron.sh'}",
    )


def _x_socket(display: str) -> Path:
    num = display.lstrip(":").split(".")[0] or "0"
    return Path(f"/tmp/.X11-unix/X{num}")


def _check_display(raw: dict) -> Check:
    if sys.platform == "win32":
        return Check("显示", _SKIP, "Windows 有桌面会话, 不需要 Xvfb", "")
    if raw.get("headless") is not False:
        # 抖音会拦 headless 并让渲染挂死 → 整晚全 skip(2026-09-30 事故)。
        return Check(
            "显示",
            _FAIL,
            f"headless={raw.get('headless')!r}, 必须是 false",
            '在 config.json 里写 "headless": false 和 "display": ":99"',
        )
    display = str(raw.get("display") or "")
    if not display:
        return Check(
            "显示",
            _FAIL,
            "headless=false 但没配 display",
            '加 "display": ":99"',
        )
    sock = _x_socket(display)
    if sock.exists():
        return Check("显示", _OK, f"DISPLAY={display}, X socket 在")
    return Check(
        "显示",
        _FAIL,
        f"DISPLAY={display} 但 {sock} 不在, 浏览器会起不来",
        f"启动 Xvfb: Xvfb {display} -screen 0 1280x800x24 &(或 systemctl start xvfb)",
    )


def _check_timezone(now: datetime) -> Check:
    # astimezone() 对 aware 的 now 会先换算成本地时区, 那样永远等于本机偏移 ——
    # 裸 now 才需要它来补 tzinfo。注意别写成 `or`: 偏移 0 的 timedelta(0) 是 falsy,
    # 会被当成「没有偏移」再绕回本机时区, UTC 机器就此漏报。
    offset = now.utcoffset()
    if offset is None:
        offset = now.astimezone().utcoffset()
    if offset == _TZ_OFFSET:
        return Check("时区", _OK, "+08:00")
    return Check(
        "时区",
        _WARN,
        f"偏移 {offset}, 应为 +08:00 —— 窗口判定会整体错位",
        "sudo timedatectl set-timezone Asia/Shanghai",
    )


def _check_disk() -> Check:
    target = RUNTIME_DIR if RUNTIME_DIR.is_dir() else BASE_DIR
    try:
        free = shutil.disk_usage(target).free / 2**30
    except OSError as exc:
        return Check("磁盘", _WARN, f"读不到可用空间: {exc}", "")
    if free < _DISK_WARN_GB:
        return Check(
            "磁盘",
            _WARN,
            f"只剩 {free:.2f} GB, 截图和日志可能写不进去",
            "清 runtime/screenshots 里旧的截图",
        )
    return Check("磁盘", _OK, f"剩 {free:.1f} GB")


def _check_lock() -> Check:
    path = Path(runtime_lock.lock_path())
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return Check("单实例锁", _OK, "没有锁文件, 随时可以手动跑")
    if runtime_lock.pid_alive(pid):
        return Check(
            "单实例锁",
            _OK,
            f"PID {pid} 正在运行 —— 现在手动跑会以 rc=4 退出",
            "",
        )
    return Check("单实例锁", _OK, f"残留锁指向已退出的 PID {pid}, 下次启动自动接管")


def _window_note(raw: dict, now: datetime) -> str:
    """离下一次窗口还有多久 —— 判断「今晚会不会跑」最直接的一行。"""
    start, end = _window_of(raw)
    if not (_valid_clock(start) and _valid_clock(end)):
        return "发送窗口配错了, 任何时间都可能开跑"
    s = datetime.combine(now.date(), time.fromisoformat(start))
    e = datetime.combine(now.date(), time.fromisoformat(end))
    if e < s:
        e += timedelta(days=1)
    if s <= now <= e:
        return f"现在就在发送窗口内({start}-{end}), 下一轮 cron 00:00 会接手"
    nxt = s if s > now else s + timedelta(days=1)
    delta = nxt - now
    hours, rest = divmod(int(delta.total_seconds()), 3600)
    minutes = rest // 60
    return f"下一次窗口: {nxt.strftime('%m-%d %H:%M')}(还有 {hours} 小时 {minutes} 分)"


def build(now: datetime | None = None) -> dict:
    now = now or datetime.now()
    raw, err = _load_raw()
    checks: list[Check] = []
    if raw is None:
        checks.append(Check("配置", _FAIL, err, _copy_hint()))
        raw = {}
    else:
        checks.extend(_check_config(raw))
        checks.extend(_check_login(raw, now))
        checks.append(_check_cron())
        checks.append(_check_display(raw))
        checks.append(_check_timezone(now))
        checks.append(_check_disk())
        checks.append(_check_lock())

    levels = [c.level for c in checks]
    fails = levels.count(_FAIL)
    return {
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "ok": fails == 0,
        "passes": levels.count(_OK),
        "warns": levels.count(_WARN),
        "fails": fails,
        "checks": [
            {
                "name": c.name,
                "level": c.level,
                "message": c.message,
                "fix": c.fix,
            }
            for c in checks
        ],
        "window_note": _window_note(raw, now),
    }


def render(data: dict) -> str:
    head = (
        f"【体检】{data['generated_at']} · 通过 {data['passes']}"
        f" · 注意 {data['warns']} · 要修 {data['fails']}"
    )
    lines = [head]
    for item in data["checks"]:
        tag = _TAG.get(item["level"], "[?]   ")
        lines.append(f"  {tag} {item['name']}  {item['message']}")
        if item["fix"]:
            lines.append(f"         → {item['fix']}")
    lines.append("")
    lines.append(f"  {data['window_note']}")
    lines.append("  昨晚结果看: python spark.py --status")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="spark.py --doctor",
        description="今晚能不能跑: 配置/登录/定时/显示/时区/磁盘/锁",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON(给程序读)")
    args = parser.parse_args(argv)
    data = build()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(render(data))
    return 1 if data["fails"] else 0
