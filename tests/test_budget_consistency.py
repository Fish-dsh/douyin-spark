"""时间预算三联的一致性守护（AGENTS.md 硬约束⑤）。

同一笔时间预算在仓库里有三份副本，改一份忘另一份就会退回"整晚没人判定"的
历史事故（见 docs/deployment.md 第 5 步）：

1. `run_cron.sh` 的 `_HARD_STOP_MIN=95`（= 01:35 硬截止）；
2. `run_cron.sh` 的 `_tmo=90`（= timeout 90m，第一轮的完整额度）；
3. 巡检 cron 的时刻（必须晚于硬截止：01:45 = `45 1`）。

这三份是**文本常量**，没有任何 Python 代码引用它们，所以只能靠断言脚本原文来
守住。本文件刻意不执行 bash（无 shell 依赖、Windows 上也能跑），只做读取与
正则匹配。
"""

import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
RUN_CRON = BASE_DIR / "run_cron.sh"
OPERATIONS = BASE_DIR / "docs" / "operations.md"
DEPLOYMENT = BASE_DIR / "docs" / "deployment.md"


def _read(path):
    return path.read_text(encoding="utf-8")


class TestHardStopBudget(unittest.TestCase):
    """硬截止与 timeout 初值必须和文档口径一致。"""

    def test_hard_stop_is_95_minutes(self):
        # 95 分钟 = 01:35。这个数字被 health_check 的 01:45 依赖。
        self.assertIn("_HARD_STOP_MIN=95", _read(RUN_CRON))

    def test_timeout_initial_value_is_90(self):
        self.assertRegex(_read(RUN_CRON), r"(?m)^\s*_tmo=90\s*$")

    def test_timeout_is_narrowed_by_variable_not_hardcoded(self):
        # 收窄逻辑（_tmo=_left）与 timeout 调用必须串起来：
        # 若有人把 timeout 写死成 "timeout 90m"，收窄就成了死代码。
        text = _read(RUN_CRON)
        self.assertIn('_tmo="$_left"', text)
        self.assertIn("timeout ${_tmo}m", text)

    def test_no_round_starts_within_20_minutes_of_deadline(self):
        self.assertIn('"$_left" -lt 20', _read(RUN_CRON))


class TestHealthCheckCronMatchesDeadline(unittest.TestCase):
    """巡检时刻必须晚于 01:35 硬截止，且两份文档口径一致。"""

    def test_operations_uses_0145(self):
        self.assertIn("45 1 * * *", _read(OPERATIONS))

    def test_deployment_uses_0145(self):
        self.assertIn("45 1 * * *", _read(DEPLOYMENT))

    def test_no_stale_0115_in_cron_examples(self):
        # 01:15 是 2026-11-01 修掉的历史口径（当时会在跑批途中判"无运行记录"）。
        for path in (OPERATIONS, DEPLOYMENT):
            for line in _read(path).splitlines():
                stripped = line.strip()
                if "health_check.py" in stripped and stripped.startswith("15 1"):
                    self.fail(f"{path.name} 里还有 01:15 的巡检 cron：{line}")


if __name__ == "__main__":
    unittest.main()
