# 板子同步核对清单（2026-08-15 修复批次）

目的：把本机已就绪的修复（git 兜底 / `health_check` / `--verify-send` / 时区约定 / ruff 配置）
同步到板子 `/opt/douyin-spark/`，并完成部署验证。逐项勾选，全部 ✅ 才算同步完成。

## 0. 前置：确认能 fast-forward（2026-11-01 补，板端实测会失败）

板子上的仓库是**快照式**仓库：它有自己的 commit（如 `73889aa5 部署快照: ...`），本机没有那个对象，
`refs/remotes/localsrc/main` 也可能落后本机几十个提交。所以第 1 节的 `merge --ff-only` **不保证成功**。

- [ ] 先判：`git -C /opt/douyin-spark merge-base --is-ancestor refs/remotes/localsrc/main HEAD && echo OK`
      输出 `OK` 才能用 `merge --ff-only`；否则（板端实测就是 NOT_FAST_FORWARD）走下面这条：
      `cd /opt/douyin-spark && tar czf /root/spark-before-sync-$(date +%F-%H%M).tgz --exclude=runtime --exclude=.venv .`
      `git -C /opt/douyin-spark fetch -q /tmp/spark-mirror.bundle "refs/heads/main:refs/remotes/localsrc/main"`
      `git -C /opt/douyin-spark reset --hard refs/remotes/localsrc/main`
      （`reset --hard` 会丢板端本地未推送的提交——所以先打 tar。`config.json` / `runtime/` 都在 .gitignore 里，不受影响）
- [ ] **不要以 root 跑 git**：板端 `.git/index` 曾属 `root:root`，BOARD 的任何 git 写操作（含 `self_heal` 自动 commit）都会因 index 只读失败。修：
      `chown -R BOARD:BOARD /opt/douyin-spark/.git /opt/douyin-spark/runtime`
      之后一律 `sudo -u BOARD git -C /opt/douyin-spark ...`

## 1. 代码同步

- [ ] 本机（唯一正源 `~/projects/douyin-spark`）先 `git commit`，再整仓同步板子（单向镜像，带 `.git`）：
      `git bundle create /tmp/spark-mirror.bundle --all && scp /tmp/spark-mirror.bundle BOARD:/tmp/`
      板子上：`git -C /opt/douyin-spark fetch -q /tmp/spark-mirror.bundle "refs/heads/main:refs/remotes/localsrc/main" && git -C /opt/douyin-spark merge --ff-only refs/remotes/localsrc/main`
      （单个文件小改可直接 `scp <file> BOARD:/opt/douyin-spark/<相对路径>`）
- [ ] **scp 多文件必须逐个写目标路径**：`scp a.py b.py BOARD:/opt/douyin-spark/` 会把两个文件都丢进**仓库根**，
      造成 `/opt/douyin-spark/perf.py` 这类误拷贝 → ruff EXE002「file is executable but no shebang」+ pytest collection error。
- [ ] 属主/权限（scp 会丢 x 位，这是 8/10 事故根因）：
      `chown -R BOARD:BOARD /opt/douyin-spark && chmod +x /opt/douyin-spark/run_cron.sh && test -x /opt/douyin-spark/run_cron.sh`
- [ ] 若只传了代码文件：板子上 `cd /opt/douyin-spark && git init -b main && git add -A && git commit -m "chore: board import"`
      （整目录 scp 自带 `.git` 则跳过；`self_heal` 自愈自动 commit 对非 git 仓库静默降级）
- [ ] 确认 `.gitignore` 生效：`git status --short` 无 `.env` / `runtime/` / `config.json`
- [ ] 板子 Python 3.11 编译校验：`.venv/bin/python -m py_compile spark.py login.py src/*.py`
- [ ] 板子单测：`.venv/bin/python -m pytest -q`（板端 venv 默认没装 pytest，先 `.venv/bin/pip install pytest`；或用 `-m unittest discover -s tests`）
- [ ] 板端 ruff 门禁（本机 x86 的 ruff 不报 EXE002，只有板端才暴露）：`.venv/bin/ruff check .`
- [ ] **可执行位反向校验**：git 索引里 `src/perf.py` / `src/events.py` / `tests/test_events.py` / `tests/test_task_watchdog.py` / `tests/test_weekly_report.py` 记录为 100755 但没有 shebang，同步后板端 ruff 会报 EXE002。检查：
      `find src tests -name '*.py' -perm -u+x -print` 期望输出为空；非空则 `chmod -R a-x src/*.py tests/*.py`
- [ ] 本次同步新增文件：`src/perf.py`、`tests/test_perf.py`；改动：`src/session.py`、`src/config.py`、`src/spark_status.py`、`src/debug.py`、`src/login.py`、`src/scheduler.py`、`src/sender.py`、`src/finder.py`、`tests/conftest.py`、`run_cron.sh`
- [ ] 生产档位确认：`grep -c perf_profile config.json` 期望 **0**（生产靠 `run_cron.sh` 的 `export SPARK_PERF_PROFILE=low` 焊死；写了 `mid`/`high` 反而会更快超时）
- [ ] 清残留（生产目录不该有测试缓存与备份）：
      `rm -rf .pytest_cache __pycache__ src/__pycache__ tests/__pycache__ .ruff_cache runtime/*.test* board-backups`
      `ls config.json.bak-* src/*.bak-* 2>/dev/null` 逐个确认后可删

## 2. 时间口径核对（必查；北京时间口径，不是 UTC）

- [ ] 时间口径 = **机器本地时 = 北京时间**：窗口/state 日期/健康检查全部按北京时判定（2026-08-15 已废弃误引入的 UTC 口径）
- [ ] `timedatectl`：Time zone 应为 **Asia/Shanghai (CST, +0800)**；`date` 显示北京时间
- [ ] 逻辑自洽：cron `0 0 * * *` = 北京 00:00，落在窗口 23:30–00:30（北京时）内
- [ ] **不要**把系统时区改成 UTC（曾误用 UTC 导致 cron 北京 00:00=UTC 16:00 永远不在窗口 → 静默不发送）

## 3. config.json（板子）

- [ ] `send_window` 保持 `23:30`–`00:30`（北京时间口径，与 crontab 00:00 北京命中）
- [ ] 可选：新增 `"notify_cmd": "<webhook/curl 命令>"`——健康检查失败时执行，失败摘要经环境变量 `HEALTH_FAIL_MSG` 传入
- [ ] `config.json` 不入 git（已 gitignore，属机器本地配置）

## 4. cron

- [ ] 主任务保留：`0 0 * * * /opt/douyin-spark/run_cron.sh`
- [ ] 新增健康检查（**晚于主任务最晚可能的收尾**，堵静默失败）：
      `45 1 * * * /opt/douyin-spark/.venv/bin/python /opt/douyin-spark/src/health_check.py --quiet --notify-only-on-change`
      （01:15 → **01:45**：`run_cron.sh` 有硬截止 `_HARD_STOP_MIN=95`（01:35）——距它不足 20 分钟不再起新一轮，每轮 timeout 按剩余时间收窄，所以 01:45 一定判得到这一晚。**没有硬截止时这句话是假的**：rc=3 的重跑能从 00:23 跑到 01:53，而 health_check 见"主任务还在跑"会直接退 0，那一晚谁都没判过）
- [ ] `crontab -l` 复核两条；**不要在 crontab 里写 `%`**（Debian cron 把未转义 `%` 当换行，静默失败）

## 5. 板子真机验证

- [ ] dry-run：`cd /opt/douyin-spark && .venv/bin/python spark.py --dry-run --no-jitter`
      （窗口内：发现+定位全链路；窗口外：仅发现，退出 0 即链路可用）
- [ ] 健康检查手动跑一次：`.venv/bin/python src/health_check.py`
      → 主任务当日未跑时应 FAIL（退出码 1、stderr 有诊断），确认不是误报
- [ ] 今晚 00:00（北京时间）后复核：
      - `tail -5 runtime/spark.log` 有 `==== 完成:` 收尾，**且 `config.accounts` 里每个账号各有一条**（缺谁谁就是没跑完）
      - `cat runtime/state.json` 的 `date` == 今天（北京时间）
      - `.venv/bin/python src/health_check.py --quiet` 转 OK（退出码 0）
- [ ] 自愈链路抽查（可选）：`ls runtime/heal_backups/ | tail` 与 `git -C /opt/douyin-spark log --oneline | head`
      确认自愈自动 commit 生效

## 6. 四方一致复核（AGENTS.md「完成前四问」①）

- [ ] `config.json` send_window × `src/config.py` × crontab × `docs/operations.md` 一致
- [ ] 板子 Python 3.11 与版本相关改动（若有）已真机复验
