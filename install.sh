#!/bin/bash
# 一条命令把「代码」变成「能跑」: 解释器 / 依赖 / 浏览器 / 虚拟显示 / 配置。
#
# 为什么要有它(2026-11-22): 部署原本是 docs/deployment.md 里的 8 步散文 —— 人读得懂,
# Agent 读不懂, 而它本来就是给 Agent 接管的。安装要么全做完, 要么说清楚卡在哪一步、
# 该执行什么命令, 不要留半套。
#
# 幂等: 每步先看有没有, 有就 [skip]; 重复跑不会弄坏已有安装。
# 只检查: ./install.sh --check   只报告缺什么(不缺则退出码 0), 不动任何文件。
set -uo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT" || exit 1

CHECK=0
[ "${1:-}" = "--check" ] && CHECK=1

missing=0
ok()   { printf '[ok]   %s\n' "$*"; }
skip() { printf '[skip] %s\n' "$*"; }
run()  { printf '[run]  %s\n' "$*"; }
warn() { printf '[warn] %s\n' "$*"; }
bad()  { printf '[--]   %s\n' "$*"; missing=$((missing + 1)); }
die()  { printf '[fail] %s\n' "$1" >&2; shift; for l in "$@"; do printf '       %s\n' "$l" >&2; done; exit 1; }

echo "== 1/6 解释器 =="
PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  die "找不到 $PY" \
      "装一个 3.11+: apt-get install -y python3 python3-venv" \
      "或指定路径: PYTHON=/usr/bin/python3.12 ./install.sh"
fi
ver="$("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null)"
if "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  ok "$PY $ver"
else
  bad "$PY $ver 太旧, 需要 3.11+"
fi

echo "== 2/6 虚拟环境 =="
if [ -x .venv/bin/python ]; then
  skip ".venv 已存在($(.venv/bin/python -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null))"
elif [ "$CHECK" = 1 ]; then
  bad "没有 .venv"
else
  run "$PY -m venv .venv"
  "$PY" -m venv .venv || die "建 venv 失败" \
      "Debian/Ubuntu 缺 venv 模块: apt-get install -y python3-venv"
  ok ".venv 建好了"
fi

echo "== 3/6 Python 依赖 =="
if [ ! -x .venv/bin/python ]; then
  bad "跳过依赖安装(没有 .venv)"
elif [ "$CHECK" = 1 ]; then
  if .venv/bin/python -c 'import playwright, cv2' 2>/dev/null; then
    ok "playwright + opencv 已装"
  else
    bad "依赖没装全: .venv/bin/pip install -r requirements.txt"
  fi
else
  run ".venv/bin/pip install -q -r requirements.txt"
  .venv/bin/pip install -q -r requirements.txt \
    || die "pip install 失败" "看上面的报错; 装不上多半是网络或 wheel 不匹配(板子 ARM 上 opencv 要 aarch64 轮子)"
  ok "依赖就绪"
fi

echo "== 4/6 浏览器内核 =="
# 不只认 $HOME: 板上 cron 以 orangepi 跑(HOME=/home/orangepi), 而 root ssh 进来时
# HOME=/root —— 只看当前 HOME 会假报"没装", 安装者就会重复下一个几百 MB 的浏览器。
browsers_hit=""
for d in "${PLAYWRIGHT_BROWSERS_PATH:-$HOME/.cache/ms-playwright}" /home/*/.cache/ms-playwright; do
  [ -d "$d" ] || continue
  hit="$(ls -d "$d"/chromium-* 2>/dev/null | head -1)"
  [ -n "$hit" ] && { browsers_hit="$(basename "$hit") ($d)"; break; }
done
if [ -n "$browsers_hit" ]; then
  skip "chromium 已下载($browsers_hit)"
elif [ "$CHECK" = 1 ]; then
  bad "chromium 没下载: .venv/bin/playwright install chromium"
elif [ -x .venv/bin/playwright ]; then
  run ".venv/bin/playwright install chromium"
  .venv/bin/playwright install chromium || die "下载 chromium 失败" "多半是网络; 国内可设 PLAYWRIGHT_DOWNLOAD_HOST 镜像"
  ok "chromium 就绪"
else
  bad "没有 playwright 命令(依赖那步没成)"
fi

echo "== 5/6 虚拟显示(Xvfb) =="
# 抖音对 headless 会弹登录拦截页, 弱机上 headless 直接渲染挂死 —— 生产必须
# headless:false + Xvfb(2026-09-30 事故)。所以没有 Xvfb 就等于装不上。
if command -v Xvfb >/dev/null 2>&1; then
  if pgrep -f "Xvfb :99" >/dev/null 2>&1 || pgrep -f "Xvfb.*:99" >/dev/null 2>&1; then
    ok "Xvfb :99 正在跑"
  else
    warn "Xvfb 装了但 :99 没在跑 —— 起来: sudo Xvfb :99 -screen 0 1280x800x24 &(生产用 systemd unit, 见 docs/deployment.md)"
  fi
else
  bad "没有 Xvfb: apt-get install -y xvfb, 再起 :99"
fi

echo "== 6/6 配置 =="
if [ -f config.json ]; then
  skip "config.json 已存在(不覆盖)"
elif [ "$CHECK" = 1 ]; then
  bad "没有 config.json: cp config.example.json config.json 然后填 accounts"
else
  cp config.example.json config.json
  warn "config.json 是示例, 必须改: accounts 里的 name / storage_file, 以及 notify_cmd"
fi
if [ -f .env ]; then
  skip ".env 已存在"
else
  cp .env.example .env 2>/dev/null && warn ".env 是模板, 密码登录需要填账号密码(仅 login.py 用)"
fi

tz="$(date +%z 2>/dev/null)"
if [ "$tz" != "+0800" ]; then
  warn "时区是 $tz 不是 +0800: 发送窗口按本地时判定, 时区不对会整天不触发(timedatectl set-timezone Asia/Shanghai)"
fi

echo
if [ "$missing" -gt 0 ]; then
  printf '== 还缺 %d 项, 按上面 [--] 那几行处理后重跑本脚本 ==\n' "$missing"
  [ "$CHECK" = 1 ] && exit 1
fi

cat <<EOF
== 装好了 ==
下一步(顺序不能反):
  1) python login.py --manual     # 有屏幕的机器上扫码一次, 生成 runtime/storage.json
  2) crontab -e                   # 加一行: 0 0 * * * $ROOT/run_cron.sh
  3) python spark.py --status     # 第二天早上用它看"成了吗", 不用翻日志
EOF
