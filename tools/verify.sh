#!/usr/bin/env bash
# 本地一条命令跑完提交前自检: 语法 -> lint -> 单测。
#
# 为什么是脚本而不是 GitHub Actions: 这个仓库的推送令牌没有 workflow 权限,
# 往 .github/workflows/ 写文件会被服务端拒掉。所以把验证收敛到本地,
# 谁 clone 下来都能一键复现同一套检查。
#
# 用法:
#   tools/verify.sh            # 全跑
#   tools/verify.sh --fast     # 跳过单测, 只做语法和 lint
#
# 注意: 本地通过 != 远端 CI 通过。这里没有第二台机器、第二个 Python 版本,
# 也没有干净环境 —— 它证明的是"在我这台机器上过了", 不是"在别人机器上会过"。
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
  exit 1
fi

# 测试会在 import 期写运行时目录, 别让它碰仓库里的 runtime/。
export SPARK_RUNTIME_DIR="${SPARK_RUNTIME_DIR:-/tmp/spark-verify-runtime}"

FAILED=0
step() { printf '\n== %s ==\n' "$1"; }

step "1/3 语法 (compileall)"
if "$PY" -m compileall -q src tests tools spark.py login.py logutil.py; then
  echo "OK"
else
  echo "FAIL" >&2
  FAILED=1
fi

step "2/3 lint (ruff)"
if "$PY" -m ruff check . ; then
  echo "OK"
else
  echo "FAIL: 没装 ruff 的话跑 pip install ruff==0.16.8" >&2
  FAILED=1
fi

if [ "${1:-}" = "--fast" ]; then
  step "3/3 单测"
  echo "跳过 (--fast)"
else
  step "3/3 单测 (pytest)"
  # 优先 pytest, 没装就退回 unittest discover。
  if "$PY" -c "import pytest" >/dev/null 2>&1; then
    if "$PY" -m pytest tests -q -p no:cacheprovider; then :; else FAILED=1; fi
  else
    if "$PY" -m unittest discover -s tests; then :; else FAILED=1; fi
  fi
fi

if [ "$FAILED" -ne 0 ]; then
  printf '\n自检未通过\n' >&2
  exit 1
fi
printf '\n全部通过（本地环境，不代表远端 CI）\n'
