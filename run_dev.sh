#!/bin/bash
# 本机开发入口(2026-11-01 立): 与板端生产**隔离**运行。
#   config.dev.json   extends 生产 config.json, 只覆盖桌面机差异
#                     (perf_profile=high → 放开 GPU/多渲染进程/大 JS 堆)
#   runtime-dev/      state/storage/日志/截图全落这里, 不碰生产 runtime/
# 生产入口仍只有一条: cron → run_cron.sh(不读这两个环境变量, 行为零变化)。
# 想在窗口外演练: ./run_dev.sh --dry-run / ./run_dev.sh --force
#
# 全新 clone 也能直接跑(2026-11-19): 缺 .venv 或缺 config.json 时先给出可照抄的
# 下一步, 而不是让 python 抛一句 FileNotFoundError 或让 config.py 静默 exit(1)。
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
    echo "错误: 找不到 .venv/bin/python。先装环境:" >&2
    echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt" >&2
    echo "  .venv/bin/playwright install chromium" >&2
    exit 1
fi

# config.dev.json 用 extends 指向 config.json; 后者被 .gitignore 排除, 所以全新
# clone 里必然不存在。这里按模板补一份, 并明确提醒它是**占位**配置。
if [ ! -f config.json ]; then
    cp config.example.json config.json
    chmod 600 config.json
    echo "提示: 已按 config.example.json 生成 config.json(占位)。" >&2
    echo "      真跑前请确认 accounts 与 send_window; 登录态用 " >&2
    echo "      .venv/bin/python login.py --manual 生成。" >&2
fi

export SPARK_CONFIG="${SPARK_CONFIG:-config.dev.json}"
export SPARK_RUNTIME_DIR="${SPARK_RUNTIME_DIR:-runtime-dev}"
exec .venv/bin/python spark.py "$@"
