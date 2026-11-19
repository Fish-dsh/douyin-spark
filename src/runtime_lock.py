"""PID 互斥锁(单实例守卫): 防止 cron 重叠导致两个 spark.py 争抢同一块 1.5G 内存。

为什么单独一个模块(2026-11-19 审计): 这套「探活 → 判身份 → 争锁 → 释放」原本混在
1300+ 行的 scheduler.py 里, 和发送链路毫无关系, 却是**平台相关最容易踩坑**的一段
(Windows 上 os.kill 的语义与 POSIX 完全不同, 详见 pid_alive)。抽出来后调度器只关心
"拿没拿到锁"这一个布尔值, 平台差异集中在 60 行里, 后来人改锁不必读整篇调度器。

对外只有四个: acquire() / release() / pid_alive() / pid_is_spark()。
约定: acquire() 返回 False 时调用方必须给非 0 退出码(见 scheduler.run 的 rc=4),
否则"锁被挂死实例长期持有"在监控面完全静默(天天"成功")。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from logutil import log

# PID 文件超过该年龄一律视为陈旧锁: 兜住 PID 复用、无权限读 /proc、写在 NFS 上的时钟漂移。
PID_STALE_SEC = 3 * 3600

# 进程实际写下的锁文件路径(acquire 成功时记录), release 只删自己写的那一个,
# 避免"进程 A 抢锁失败 → 退出时把进程 B 的锁删掉"这类跨进程误删。
_LOCK_FILE = ""


def lock_path() -> str:
    """锁文件路径: <项目根>/runtime/.spark.pid。

    按模块自身位置推导(不是 CWD), 所以从任何目录、任何入口被拉起都指向同一个锁;
    测试把本模块的 __file__ 指向临时目录即可完全隔离。
    """
    return os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "runtime", ".spark.pid"
    )


def _win_pid_alive(pid: int) -> bool:
    """Windows 探活: OpenProcess(仅查询权限) + GetExitCodeProcess == STILL_ACTIVE。"""
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(
        0x1000, False, pid
    )  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def pid_alive(pid: int) -> bool:
    """PID 是否存活(跨平台)。

    POSIX 惯用 `os.kill(pid, 0)` 探活, 但**Windows 上不能这么写**(2026-11-19 审计发现):
    那边没有 signal 0 的语义, `os.kill` 直接落到 `TerminateProcess`, 传 0 等于「把那个
    进程杀掉」。原实现拿它判存活, 后果是开发机/非 Linux 环境开第二个实例时, 不是拒绝
    启动, 而是**把正在跑的第一个实例干掉**; 单元测试也因此自杀(os.getpid() 写进锁文件)。
    这里按平台分流: POSIX 仍用 signal 0(EPERM = 进程在只是不归我们管, 也算活),
    Windows 走 OpenProcess + GetExitCodeProcess, 只查询状态、绝不碰进程。
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _win_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def pid_is_spark(pid: int) -> bool:
    """该 PID 是否确实是 spark.py 主任务(而非被复用的无关进程)。

    判据是 /proc/<pid>/cmdline(生产 Linux 唯一路径)。非 Linux 上没有 /proc, 这里返回
    False 即「按 PID 复用接管锁」——与改造前行为一致, 不会让锁卡死整晚。
    """
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace")
    except OSError:
        return False
    return "spark.py" in cmdline


def _exempted() -> bool:
    return os.environ.get("SPARK_SKIP_PID_LOCK", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def acquire() -> bool:
    """争 PID 锁。返回 True = 获锁(或豁免/写失败降级), False = 另一实例持锁。

    **豁免位 `SPARK_SKIP_PID_LOCK=1`**(持锁期间手工跑验证子进程专用): 主任务持锁期间想手工跑
    `spark.py --dry-run/--verify-send` 时, 子进程若照常争锁, 会看见父 PID 存活 → 走
    "另一实例运行中"分支 → 裸 return → **rc=0 且日志无「待发送≥1」** → `_log_evidence`
    判定无证据(2026-10-01 评审发现)。置位后本进程不写 PID 文件, 因此也不会在退出时误删
    父进程的锁。
    """
    global _LOCK_FILE
    if _exempted():
        log("[lock] SPARK_SKIP_PID_LOCK 已置位, 跳过 PID 互斥(不写 PID 文件)")
        return True
    path = lock_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.isfile(path):
            stale = False
            try:
                age = time.time() - os.path.getmtime(path)
                if age > PID_STALE_SEC:
                    stale = True
                    log(
                        f"[warn] PID 文件已存在 {age / 3600:.1f}h"
                        f"(超过 {PID_STALE_SEC / 3600:.0f}h 上限), 视为陈旧锁并接管"
                    )
            except OSError:
                pass
            if not stale:
                old_pid = None
                try:
                    old_pid = int(Path(path).read_text(encoding="utf-8").strip())
                except (ValueError, OSError):
                    old_pid = None
                if old_pid is not None and pid_alive(old_pid):
                    if pid_is_spark(old_pid):
                        log(f"[warn] 另一实例运行中(PID {old_pid}), 退出")
                        return False
                    log(
                        f"[warn] PID {old_pid} 存活但 cmdline 非 spark.py"
                        "(疑似 PID 复用), 视为陈旧锁并接管"
                    )
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        _LOCK_FILE = path
        return True
    except OSError as exc:
        # 写不了锁文件(只读目录/权限不足)不该让整晚停摆: 降级为"不互斥"继续跑,
        # 代价只是失去防重叠保护, 而重叠本身有 cron 时刻兜着(每晚只拉一次)。
        log(f"[warn] PID 锁获取失败(继续执行): {exc}")
        return True


def release() -> None:
    """删掉本进程写下的锁文件; 没拿到锁的进程调用它等于空操作。"""
    global _LOCK_FILE
    if not _LOCK_FILE:
        return
    try:
        os.unlink(_LOCK_FILE)
    except OSError:
        pass
    finally:
        _LOCK_FILE = ""
