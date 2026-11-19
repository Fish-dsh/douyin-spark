#!/usr/bin/env bash
# 本地一条命令跑完提交前自检: 语法 -> JS 语法 -> lint -> 单测。
#
# 为什么是脚本而不是 GitHub Actions: 这个仓库的推送令牌没有 workflow 权限,
# 往 .github/workflows/ 写文件会被服务端拒掉。所以把验证收敛到本地,
# 谁 clone 下来都能一键复现同一套检查。
#
# 用法:
#   tools/verify.sh            # 全跑
#   tools/verify.sh --fast     # 跳过单测, 只做语法、JS 语法和 lint
#   PY=/path/to/python tools/verify.sh    # 指定解释器(交叉验证另一版本时用)
#
# 退出码: 0 全过 / 1 有检查失败 / 2 没失败但有步骤因缺工具被跳过
#         (2 表示"没验证到", 不是"验证过了", 别把它当绿)。
#
# 注意: 本地通过 != 远端 CI 通过。这里只有一台机器、一个 Python 版本、
# 一个非干净环境 —— 它证明的是"在我这台机器上过了", 不是"在别人机器上会过"。
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

# 解释器优先级: 环境变量 PY > 项目虚拟环境 .venv/bin/python > PATH 里的 python3。
# (PY= 是给特殊场景留的口子, 比如在 WSL 里指向另一套解释器做交叉验证。)
if [ -n "${PY:-}" ]; then
  :
elif [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
elif command -v python3 >/dev/null 2>&1; then
  PY=python3
else
  echo "找不到 python3, 也没有 .venv/bin/python" >&2
  echo "先建环境: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

# 测试会在 import 期写运行时目录, 别让它碰仓库里的 runtime/。
export SPARK_RUNTIME_DIR="${SPARK_RUNTIME_DIR:-/tmp/spark-verify-runtime}"

FAILED=0
SKIPPED=0
step() { printf '\n== %s ==\n' "$1"; }
skip() { echo "跳过: $1"; SKIPPED=$((SKIPPED + 1)); }

step "1/4 语法 (compileall)"
if "$PY" -m compileall -q src tests tools spark.py login.py logutil.py; then
  echo "OK"
else
  echo "FAIL" >&2
  FAILED=1
fi

# JS 语法: src/ 里那些三引号 JS 常量只有在浏览器里执行时才暴露语法错,
# 生产日志里表现为 Page.evaluate: SyntaxError, 本地完全看不见。
# node 是可选依赖(这不是 Node 项目), 没装记 SKIP, 别记 FAIL。
step "2/4 JS 语法 (node --check, 可选)"
if ! command -v node >/dev/null 2>&1; then
  skip "没装 node (Debian/Ubuntu: sudo apt install nodejs)"
elif "$PY" tools/check_js.py; then
  echo "OK"
else
  echo "FAIL" >&2
  FAILED=1
fi

# ruff / pytest 属于开发依赖, 干净机器上没装是正常的 —— 那是"没验证到",
# 报错退出只会让新人以为代码有问题。缺工具记 SKIP, 工具报错才记 FAIL。
step "3/4 lint (ruff)"
if ! "$PY" -c "import ruff" >/dev/null 2>&1 && ! "$PY" -m ruff --version >/dev/null 2>&1; then
  skip "没装 ruff (pip install ruff==0.16.8)"
elif "$PY" -m ruff check . ; then
  echo "OK"
else
  echo "FAIL" >&2
  FAILED=1
fi

if [ "${1:-}" = "--fast" ]; then
  step "4/4 单测"
  skip "--fast 只做语法、JS 语法和 lint"
else
  step "4/4 单测"
  # 优先 pytest, 没装就退回 unittest discover(标准库, 一定在)。
  if "$PY" -c "import pytest" >/dev/null 2>&1; then
    if "$PY" -m pytest tests -q -p no:cacheprovider; then :; else FAILED=1; fi
  else
    echo "提示: 没装 pytest, 退回 unittest discover(等价覆盖, 输出更啰嗦)"
    if "$PY" -m unittest discover -s tests; then :; else FAILED=1; fi
  fi
fi

if [ "$FAILED" -ne 0 ]; then
  printf '\n自检未通过\n' >&2
  exit 1
fi
if [ "$SKIPPED" -ne 0 ]; then
  printf '\n没有失败, 但有 %d 项被跳过 —— 那不是"通过", 是"没验证到"。\n' "$SKIPPED"
  exit 2
fi
printf '\n全部通过（本地环境，不代表远端 CI）\n'
