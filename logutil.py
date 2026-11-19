"""统一日志出口: 所有模块(login/spark/未来模块)共用这一个实现.

把日志的双写逻辑(终端 + 追加写 spark.log)收敛到单一函数, 避免多个模块
各自实现一份导致格式/文件路径漂移。用法:

    from logutil import log
    log("hello", prefix="login")   # 输出 [YYYY-MM-DD HH:MM:SS login] hello

进程内总是追加同一切口, 保证 spark.log 时序完整、单一来源。
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

_PROJECT_DIR = Path(__file__).resolve().parent


def _offline_entry() -> bool:
    """当前进程是否是**非生产入口**(单测或 diagnostics 探针), 用于把监控面挪走。

    两者都会跑 process_account / discovery 等真实代码路径, 一旦写进
    `runtime/spark.log`, 就往**生产监控面**注入了 `==== 账号 X 完成: …` 这类
    伪造收尾行 —— 2026-10-04 板子上单测一口气写了 42 条伪造完成行,
    足以让 health_check 对真实的漏发判 OK(告警被测试数据顶掉)。
    判据取 `__main__.__file__`:`python -m unittest` 下它固定是
    `…/unittest/__main__.py`; 直接跑 `python tests/test_x.py` 也能覆盖。
    """
    main_file = getattr(sys.modules.get("__main__"), "__file__", "") or ""
    if not main_file:
        return False
    path = Path(main_file)
    if path.name.startswith("test_"):
        return True
    if path.parent.name == "diagnostics":
        return True
    return path.name == "__main__.py" and path.parent.name in {
        "unittest",
        "pytest",
        "_pytest",
    }


def _runtime_dir() -> Path:
    """日志/状态所在目录: 默认项目内 runtime/, 可用 SPARK_RUNTIME_DIR 指向开发目录。

    2026-11-01 本机/板端分离: 开发跑 run_dev.sh 时日志落 runtime-dev/spark.log,
    不再往生产监控面(runtime/spark.log)里掺行。
    """
    raw = os.environ.get("SPARK_RUNTIME_DIR")
    if raw and raw.strip():
        path = Path(raw.strip())
        return path if path.is_absolute() else _PROJECT_DIR / path
    return _PROJECT_DIR / "runtime"


LOG_FILE = Path(
    os.environ.get("SPARK_LOG_FILE")
    or _runtime_dir() / ("spark.test.log" if _offline_entry() else "spark.log")
)

_log_fh = None
_write_count = 0

_LOG_MAX_BYTES = 5 * 1024 * 1024


def _rotate_if_needed():
    global _log_fh
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > _LOG_MAX_BYTES:
            if _log_fh and not _log_fh.closed:
                _log_fh.close()
            old2 = LOG_FILE.with_suffix(".log.2")
            old1 = LOG_FILE.with_suffix(".log.1")
            if old1.exists():
                old1.rename(old2)
            LOG_FILE.rename(old1)
            _log_fh = None
    except OSError:
        pass


def _get_fh():
    global _log_fh
    if _log_fh is None or _log_fh.closed:
        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            _log_fh = open(LOG_FILE, "a", encoding="utf-8")
        except OSError:
            _log_fh = None
    return _log_fh


def log(msg: str, prefix: str = "") -> None:
    global _write_count
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tag = f" {prefix}" if prefix else ""
    line = f"[{ts}{tag}] {msg}"
    print(line, flush=True)
    _write_count += 1
    if _write_count % 50 == 0:
        _rotate_if_needed()
    fh = _get_fh()
    if fh is not None:
        try:
            fh.write(line + "\n")
            fh.flush()
        except OSError:
            pass


def close_log() -> None:
    global _log_fh
    if _log_fh and not _log_fh.closed:
        _log_fh.close()
    _log_fh = None


import atexit

atexit.register(close_log)
