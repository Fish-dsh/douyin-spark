#!/usr/bin/env bash
# 把代码部署到跑生产的那台机器 —— 唯一入口, 别再手写 tar。
#
# 为什么要有这个脚本(2026-11-02 真实事故): 手写 tar 时把本地那份 19 字节的
# config.json 桩一起打了进去, 覆盖掉部署机上真正的配置。当晚 accounts 为空,
# 跑了 0 个好友却返回 rc=0, 巡检判健康, 告警通道也没配 —— 静默断火一整晚。
#
# 三条硬规矩:
#   1. 永远不传 config.json / runtime/ / .venv / .git —— 配置与登录态各机独立。
#      代码走这个脚本; 登录态(runtime/storage.json)另外单独传, 见 docs/deployment.md
#   2. 传完必须校验目标机 accounts 非空(走 src/config.py::preflight), 失败就回滚
#   3. 传完跑一次 import 冒烟, 失败也回滚
#
# 用法:
#   SPARK_HOST=user@host ./tools/deploy.sh
#   SPARK_HOST      远端(支持 ssh config 别名), 必填
#   SPARK_REMOTE_DIR 远端部署目录, 默认 /opt/douyin-spark
#   SPARK_REMOTE_USER 远端跑服务的用户, 默认当前用户名
set -euo pipefail

HOST="${SPARK_HOST:?请先设置 SPARK_HOST, 例如 SPARK_HOST=user@host ./tools/deploy.sh}"
REMOTE_DIR="${SPARK_REMOTE_DIR:-/opt/douyin-spark}"
REMOTE_USER="${SPARK_REMOTE_USER:-$(whoami)}"
LOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
NEW_TGZ="/tmp/spark-deploy-$STAMP.tgz"
PREV_TGZ="/tmp/spark-prev-$STAMP.tgz"
CONF_BAK="/tmp/spark-config-$STAMP.json"
SMOKE="$(mktemp)"
trap 'rm -f "$SMOKE" "$NEW_TGZ"' EXIT

cat >"$SMOKE" <<'PY'
import sys
sys.path.insert(0, '.')
from src.config import load_config, preflight

cfg = load_config()
preflight(cfg)                    # accounts 为空 / 条目缺 name 会抛 ConfigError
print('accounts:', [a.get('name') for a in cfg['accounts']])
print('notify_cmd:', 'yes' if cfg.get('notify_cmd') else 'NO(失败时不会有任何告警)')
import src.scheduler  # noqa: F401   冒烟: 主链路模块能 import
print('import ok')
PY

cd "$LOCAL_DIR"
echo "[deploy] 打包 (排除配置、登录态与虚拟环境)"
tar --exclude=.venv --exclude=runtime --exclude=__pycache__ --exclude=.git \
    --exclude=runtime-dev --exclude='*.log' --exclude=config.json \
    -czf "$NEW_TGZ" .

echo "[deploy] 备份远端当前代码与配置"
ssh "$HOST" "cd $REMOTE_DIR && tar --exclude=.venv --exclude=runtime \
    --exclude=__pycache__ --exclude=.git --exclude=config.json \
    -czf $PREV_TGZ . && cp -a config.json $CONF_BAK && echo backed-up"

rollback() {
    echo "[deploy][ERROR] 校验失败, 回滚代码与配置" >&2
    ssh "$HOST" "cd $REMOTE_DIR && tar -xzf $PREV_TGZ && cp -a $CONF_BAK config.json \
        && chown -R $REMOTE_USER:$REMOTE_USER . && echo rolled-back" \
        || echo "[deploy][ERROR] 回滚本身也失败了, 需要人工介入" >&2
    exit 1
}

echo "[deploy] 上传并解包到 $HOST:$REMOTE_DIR"
scp -q "$NEW_TGZ" "$HOST:$NEW_TGZ"
# 解包后必须收拾权限: 从 Windows 挂载盘(WSL /mnt/c)或某些 tar 打包出来的成员带着
# 0777, 于是 src/ tests/ 下每个 .py 都变成可执行 —— 板端 ruff 会刷出一整屏
# EXE002 The file is executable but no shebang is present, 真正的 lint 错误被淹掉。
# 这里统一: 所有 .py 去掉 x 位, 入口脚本与 tools/ 里的 shell/node 脚本保留 x 位。
ssh "$HOST" "cd $REMOTE_DIR && tar -xzf $NEW_TGZ \
    && find . -path ./.venv -prune -o -name '*.py' -exec chmod a-x {} + \
    && chmod +x run_cron.sh run_dev.sh 2>/dev/null; \
    chmod +x tools/*.sh tools/*.py 2>/dev/null; \
    chown -R $REMOTE_USER:$REMOTE_USER . && echo unpacked" || rollback

echo "[deploy] 校验配置与可导入性"
ssh "$HOST" "cd $REMOTE_DIR && sudo -u $REMOTE_USER .venv/bin/python -" \
    <"$SMOKE" || rollback

echo "[deploy] 完成: $STAMP"
echo "[deploy] 提醒: 登录态(runtime/storage.json)不走这个脚本, 首次部署请单独传(见 docs/deployment.md)"