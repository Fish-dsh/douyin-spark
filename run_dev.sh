#!/bin/bash
# 本机开发入口(2026-11-01 立): 与板端生产**隔离**运行。
#   config.dev.json   extends 生产 config.json, 只覆盖桌面机差异
#                     (perf_profile=high → 放开 GPU/多渲染进程/大 JS 堆)
#   runtime-dev/      state/storage/日志/截图全落这里, 不碰生产 runtime/
# 生产入口仍只有一条: cron → run_cron.sh(不读这两个环境变量, 行为零变化)。
# 想在窗口外演练: ./run_dev.sh --dry-run / ./run_dev.sh --force
set -euo pipefail
cd "$(dirname "$0")"
export SPARK_CONFIG="${SPARK_CONFIG:-config.dev.json}"
export SPARK_RUNTIME_DIR="${SPARK_RUNTIME_DIR:-runtime-dev}"
exec .venv/bin/python spark.py "$@"
