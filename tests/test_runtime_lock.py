"""runtime_lock 单测: 探活语义 / 争锁 / 释放 / 陈旧回收(跨平台)。

为什么要单独一份: 这段是**平台相关最容易踩坑**的代码(Windows 上 os.kill 的语义与
POSIX 完全不同, 见 runtime_lock.pid_alive 的注释), 而它原本只被
tests/test_scheduler.py 的 TestPidLockExemption 间接覆盖 —— 那组用例测的是「调度器
怎么用锁」, 且跨进程那条还得 skip 掉非 Linux。锁本身的行为没有任何直接断言,
抽模块时留下的空档就是这里补的。

隔离手法与调度器那组一致: acquire() 用 os.path.dirname(os.path.dirname(__file__))
定位 runtime/, 所以把模块 __file__ 指向临时目录即可完全隔离 —— 绝不碰真实的
runtime/.spark.pid。
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from src import runtime_lock as lock_mod


def _isolated(tmpd: str) -> tuple[Path, Path]:
    """在 tmpd 下伪造一个项目根, 返回 (项目根, 锁文件路径)。"""
    proj = Path(tmpd)
    (proj / "src").mkdir(parents=True, exist_ok=True)
    return proj, proj / "runtime" / ".spark.pid"


class _LockCase(unittest.TestCase):
    """把模块的 __file__ 与 _LOCK_FILE 指到临时项目里的公共底座。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.proj, self.lock = _isolated(self._tmp.name)
        patcher = patch.object(
            lock_mod, "__file__", str(self.proj / "src" / "runtime_lock.py")
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self._lock_file = patch.object(lock_mod, "_LOCK_FILE", "")
        self._lock_file.start()
        self.addCleanup(self._lock_file.stop)
        self._env = patch.dict(os.environ)
        self._env.start()
        os.environ.pop("SPARK_SKIP_PID_LOCK", None)
        self.addCleanup(self._env.stop)


class TestLockPath(unittest.TestCase):
    def test_path_is_derived_from_module_not_cwd(self):
        """锁路径按模块位置推导, 所以从任何目录拉起都指向同一个文件。"""
        path = lock_mod.lock_path()
        self.assertEqual(Path(path).name, ".spark.pid")
        self.assertEqual(Path(path).parent.name, "runtime")
        self.assertEqual(
            Path(path).parent.parent, Path(lock_mod.__file__).resolve().parent.parent
        )


class TestPidAlive(unittest.TestCase):
    def test_own_pid_is_alive(self):
        self.assertTrue(lock_mod.pid_alive(os.getpid()))

    def test_non_positive_pid_is_not_alive(self):
        # 0 和负数没有进程语义; Windows 上尤其不能落到 OpenProcess 去试。
        self.assertFalse(lock_mod.pid_alive(0))
        self.assertFalse(lock_mod.pid_alive(-1))

    def test_absent_pid_is_not_alive(self):
        # 4_000_000 超过 Linux 默认 pid_max(4194304 之上再留余量)也超过 Windows 实际取值。
        self.assertFalse(lock_mod.pid_alive(4_000_000))


class TestPidIsSpark(unittest.TestCase):
    def test_current_process_is_not_spark(self):
        """测试进程的 cmdline 是 pytest, 不能被认为持有 spark 锁。"""
        self.assertFalse(lock_mod.pid_is_spark(os.getpid()))

    def test_non_linux_returns_false_without_proc(self):
        """非 Linux 上没有 /proc: 返回 False 即「按 PID 复用接管」, 不让锁卡死整晚。"""
        if sys.platform == "win32":
            self.assertFalse(lock_mod.pid_is_spark(os.getpid()))
        else:
            with patch("pathlib.Path.read_bytes", side_effect=OSError("no /proc")):
                self.assertFalse(lock_mod.pid_is_spark(1234))


class TestAcquireRelease(_LockCase):
    def test_acquire_writes_own_pid(self):
        self.assertTrue(lock_mod.acquire())
        self.assertEqual(self.lock.read_text(encoding="utf-8"), str(os.getpid()))

    def test_release_removes_own_lock(self):
        self.assertTrue(lock_mod.acquire())
        lock_mod.release()
        self.assertFalse(self.lock.exists())
        self.assertEqual(lock_mod._LOCK_FILE, "")

    def test_release_without_acquire_is_noop(self):
        """没拿到锁的进程退出时调用 release 不得删别人的锁。"""
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        self.lock.write_text("999999", encoding="utf-8")
        lock_mod.release()
        self.assertTrue(self.lock.exists(), "release 只能删自己写的那一个")

    def test_exemption_writes_nothing(self):
        with patch.dict(os.environ, {"SPARK_SKIP_PID_LOCK": "1"}):
            self.assertTrue(lock_mod.acquire())
        self.assertFalse(self.lock.exists())
        self.assertFalse(
            (self.proj / "runtime").exists(), "豁免进程连 runtime/ 都不该创建"
        )

    def test_live_holder_blocks_second_instance(self):
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        self.lock.write_text(str(os.getpid()), encoding="utf-8")
        with patch.object(lock_mod, "pid_is_spark", return_value=True):
            self.assertFalse(lock_mod.acquire())
        self.assertEqual(self.lock.read_text(encoding="utf-8"), str(os.getpid()))

    def test_stale_by_age_is_reclaimed(self):
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        self.lock.write_text(str(os.getpid()), encoding="utf-8")
        old = time.time() - (lock_mod.PID_STALE_SEC + 3600)
        os.utime(self.lock, (old, old))
        self.assertTrue(lock_mod.acquire())
        self.assertEqual(self.lock.read_text(encoding="utf-8"), str(os.getpid()))

    def test_unwritable_lock_degrades_to_true(self):
        """写不了锁文件不该让整晚停摆: 降级为不互斥继续跑。"""
        with patch("builtins.open", side_effect=OSError("read-only fs")):
            self.assertTrue(lock_mod.acquire())
        self.assertEqual(lock_mod._LOCK_FILE, "")


if __name__ == "__main__":
    unittest.main()
