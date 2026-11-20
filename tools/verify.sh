#!/usr/bin/env bash
# 提交前自检的 POSIX 入口 —— 只是转发到 tools/verify.py。
#
# 检查逻辑只有一份, 在 tools/verify.py 里; 这个壳存在的唯一理由是让
# .githooks/pre-push 和习惯 `./tools/verify.sh` 的人继续可用, 同时 Windows
# 上也能直接 `python tools/verify.py` 跑同一套检查。
#
# 用法与退出码见 tools/verify.py --help (0 全过 / 1 有失败 / 2 有跳过)。
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

# 解释器优先级: 环境变量 PY > 项目虚拟环境 .venv/bin/python > PATH 里的 python3。
if [ -n "${PY:-}" ]; then
  :
elif [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
elif command -v python3 >/dev/null 2>&1; then
  PY=python3
else
  echo "找不到 python3, 也没有 .venv/bin/python" >&2
  echo "先建环境: python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt" >&2
  exit 1
fi

exec "$PY" tools/verify.py "$@"
