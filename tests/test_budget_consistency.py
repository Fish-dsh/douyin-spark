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
RUN_CRON_PS1 = BASE_DIR / "run_cron.ps1"
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


class TestWindowsEntryKeepsSameBudget(unittest.TestCase):
    """run_cron.ps1 是 run_cron.sh 的 Windows 版, 同一笔预算必须同三个数字。

    两边判据不同实现(Wait-Process 代替 timeout), 但"首轮 90 分钟 / 硬截止 01:35 /
    距截止不足 20 分钟不起新一轮"是同一份约定 —— 改了 Linux 侧忘了 Windows 侧,
    同一晚在两台机器上会得出不同结论, 而告警口径只有一套。
    """

    def test_hard_stop_is_95_minutes(self):
        self.assertRegex(_read(RUN_CRON_PS1), r"(?m)^\$HardStopMin = 95\b")

    def test_timeout_initial_value_is_90(self):
        self.assertRegex(_read(RUN_CRON_PS1), r"(?m)^\$Tmo = 90\b")

    def test_no_round_starts_within_20_minutes_of_deadline(self):
        text = _read(RUN_CRON_PS1)
        self.assertRegex(text, r"(?m)^\$MinLeft = 20\b")
        self.assertIn("$left -lt $MinLeft", text)

    def test_timeout_is_narrowed_not_hardcoded(self):
        text = _read(RUN_CRON_PS1)
        self.assertIn("if ($left -lt $tmo) { $tmo = $left }", text)
        self.assertIn("$tmo * 60", text)

    def test_deadline_epoch_is_exported(self):
        # spark.py 靠它算补扫余量; 缺了不会报错, 只会让硬截止在进程内失效。
        self.assertIn("SPARK_DEADLINE_EPOCH", _read(RUN_CRON_PS1))

    def test_timeout_kills_the_whole_tree(self):
        # 只 Stop-Process 会留下 chromium 子进程: 父进程死后浏览器继续吃内存,
        # 下一轮再开新实例。taskkill /T 才是与 Linux 侧 timeout 对齐的语义。
        text = _read(RUN_CRON_PS1)
        self.assertIn("taskkill.exe", text)
        self.assertIn('"/T", "/F"', text)

    def test_exit_code_contract_is_mirrored(self):
        # 124 由脚本自己置(超时杀进程), 其余直接 exit; 非 1/3 一律透传 spark.py 的码。
        text = _read(RUN_CRON_PS1)
        self.assertIn("$rc = 124", text)
        for code in ("130", "0", "4", "1"):
            self.assertIn(f"exit {code}", text)
        self.assertIn("exit $rc", text)


class TestDeadlineUsesLogicalRunDay(unittest.TestCase):
    """硬截止必须挂在「逻辑运行日」的 00:00 上, 不是墙上今天的 00:00。

    发送窗口跨午夜(23:30-00:30), 23 点后手动补跑的那一次属于明天那一晚。
    照「今天零点」算, 硬截止就成了 22 小时前的过去时刻 → spark.py 的补扫余量
    恒为负 → 补扫阶段永远放弃(2026-11-20 审查发现, 两侧同型)。
    """

    def test_shell_uses_tomorrow_after_23(self):
        text = _read(RUN_CRON)
        # 判据复用 _now_min() 的负数语义(23:30 → -30), 不另取一次墙上时钟:
        # `date +%H` 带前导零, `[ "08" -ge 23 ]` 在 bash 里是 integer expected。
        self.assertIn('if [ "$(_now_min)" -lt 0 ]', text)
        self.assertIn('date -d "tomorrow 00:00"', text)
        # 逻辑运行日必须真的参与 deadline 计算, 否则那行 if 是死代码。
        self.assertIn("_day_zero + _HARD_STOP_MIN * 60", text)

    def test_powershell_uses_tomorrow_after_23(self):
        text = _read(RUN_CRON_PS1)
        self.assertIn(
            "if ((Get-NowMin) -lt 0) { $runDate = $runDate.AddDays(1) }", text
        )
        self.assertIn("[DateTimeOffset]::new($runDate).ToUnixTimeSeconds()", text)

    def test_no_wall_clock_deadline_left(self):
        # 回潮守卫: 「今天零点」的两种旧写法都不许再出现。
        self.assertNotIn('date -d "$(date +%F) 00:00"', _read(RUN_CRON))
        self.assertNotIn("[DateTimeOffset]::new((Get-Date).Date)", _read(RUN_CRON_PS1))


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
