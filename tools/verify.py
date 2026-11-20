#!/usr/bin/env python3
"""提交前自检: 语法 -> JS 语法 -> lint -> 单测, 一条命令跑完。

为什么是本地脚本而不是 GitHub Actions: 这个仓库的推送令牌没有 workflow 权限,
往 .github/workflows/ 写文件会被服务端拒掉。所以把验证收敛到本地, 谁 clone
下来都能一键复现同一套检查。

用法:
    python tools/verify.py            # 全跑
    python tools/verify.py --fast     # 跳过单测, 只做语法、JS 语法和 lint
    python tools/verify.py --python /path/to/python   # 指定解释器

退出码: 0 全过 / 1 有检查失败 / 2 没失败但有步骤因缺工具被跳过
        (2 表示"没验证到", 不是"验证过了", 别把它当绿)。
        --fast 主动跳过单测不计入 2 —— 那是使用者按需裁剪, 不是缺工具。

注意: 本地通过 != 远端 CI 通过。这里只有一台机器、一个 Python 版本、
一个非干净环境 —— 它证明的是"在我这台机器上过了", 不是"在别人机器上会过"。

本文件是唯一实现, tools/verify.sh 只是转发到这里的壳 —— 两份逻辑必然漂移,
所以壳里不留任何检查代码。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# compileall 的目标: 会进生产的 Python 全在这儿。
COMPILE_TARGETS = ("src", "tests", "tools", "spark.py", "login.py", "logutil.py")


def _venv_python() -> Path | None:
    """项目虚拟环境里的解释器, 两个平台各一种布局。"""
    for cand in (
        ROOT / ".venv" / "bin" / "python",
        ROOT / ".venv" / "Scripts" / "python.exe",
    ):
        if cand.exists():
            return cand
    return None


def pick_python(explicit: str | None) -> str:
    """解释器优先级: --python > 环境变量 PY > .venv > 当前解释器。

    PY= 是给特殊场景留的口子, 比如在 WSL 里指向另一套解释器做交叉验证。
    """
    if explicit:
        return explicit
    env = os.environ.get("PY")
    if env:
        return env
    venv = _venv_python()
    if venv is not None:
        return str(venv)
    return sys.executable


def run(py: str, args: list[str]) -> int:
    """在仓库根跑一条命令, 原样转发它的输出与退出码。"""
    return subprocess.call([py, *args], cwd=str(ROOT))


def has_module(py: str, name: str) -> bool:
    """解释器里能不能 import 这个模块 —— 比 which ruff 可靠, 认 venv 里的。"""
    return (
        subprocess.call(
            [py, "-c", f"import {name}"],
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        == 0
    )


class Report:
    """累计失败与跳过, 最后换算成退出码。"""

    def __init__(self) -> None:
        self.failed = 0
        self.skipped = 0

    def step(self, title: str) -> None:
        print(f"\n== {title} ==")

    def fail(self) -> None:
        print("FAIL", file=sys.stderr)
        self.failed += 1

    def skip(self, reason: str) -> None:
        print(f"跳过: {reason}")
        self.skipped += 1

    def ok(self) -> None:
        print("OK")


def step_compile(py: str, rep: Report) -> None:
    rep.step("1/4 语法 (compileall)")
    if run(py, ["-m", "compileall", "-q", *COMPILE_TARGETS]) == 0:
        rep.ok()
    else:
        rep.fail()


def step_js(py: str, rep: Report) -> None:
    """src/ 里的三引号 JS 常量只有在浏览器里执行时才暴露语法错,
    生产日志里表现为 Page.evaluate: SyntaxError, 本地完全看不见。
    node 是可选依赖(这不是 Node 项目), 没装记 SKIP, 别记 FAIL。"""
    rep.step("2/4 JS 语法 (node --check, 可选)")
    if shutil.which("node") is None:
        rep.skip("没装 node (Debian/Ubuntu: sudo apt install nodejs)")
    elif run(py, [str(ROOT / "tools" / "check_js.py")]) == 0:
        rep.ok()
    else:
        rep.fail()


def step_lint(py: str, rep: Report) -> None:
    """ruff / pytest 属于开发依赖, 干净机器上没装是正常的 —— 那是"没验证到",
    报错退出只会让新人以为代码有问题。缺工具记 SKIP, 工具报错才记 FAIL。"""
    rep.step("3/4 lint (ruff)")
    if not has_module(py, "ruff"):
        rep.skip("没装 ruff (pip install -r requirements-dev.txt)")
    elif run(py, ["-m", "ruff", "check", "."]) == 0:
        rep.ok()
    else:
        rep.fail()


def step_tests(py: str, rep: Report) -> None:
    rep.step("4/4 单测")
    if has_module(py, "pytest"):
        if run(py, ["-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"]) != 0:
            rep.fail()
        return
    # 别写成"等价覆盖": unittest discover 只跑 unittest.TestCase,
    # tests/ 里有些用例是 pytest 风格的, 退回这条会静默少跑。
    print(
        "提示: 没装 pytest, 退回 unittest discover(标准库测试子集, pytest 风格用例不会跑到)"
    )
    if run(py, ["-m", "unittest", "discover", "-s", "tests"]) != 0:
        rep.fail()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="verify.py",
        description="提交前自检: 语法 + JS 语法 + lint + 单测",
    )
    ap.add_argument("--fast", action="store_true", help="跳过单测, 只做前三步")
    ap.add_argument(
        "--python", default=None, help="指定解释器(默认 .venv, 再退当前解释器)"
    )
    args = ap.parse_args(argv)

    py = pick_python(args.python)

    # 测试会在 import 期写运行时目录, 别让它碰仓库里的 runtime/。
    os.environ.setdefault(
        "SPARK_RUNTIME_DIR", str(Path(tempfile.gettempdir()) / "spark-verify-runtime")
    )

    rep = Report()
    step_compile(py, rep)
    step_js(py, rep)
    step_lint(py, rep)
    if args.fast:
        rep.step("4/4 单测")
        # 这里刻意**不**记 SKIP: --fast 是使用者主动选择的"这次不跑单测",
        # 属于按需裁剪, 不属于"缺工具没验证到"。记进去会让退出码恒为 2,
        # 而 .githooks/pre-push 就是拿 --fast 跑的 —— 每次推送都被自己的闸门拦下。
        print("按 --fast 要求跳过(不是缺检, 不计入退出码)")
    else:
        step_tests(py, rep)

    if rep.failed:
        print("\n自检未通过", file=sys.stderr)
        return 1
    if rep.skipped:
        print(
            f'\n没有失败, 但有 {rep.skipped} 项被跳过 —— 那不是"通过", 是"没验证到"。'
        )
        return 2
    print("\n全部通过（本地环境，不代表远端 CI）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
