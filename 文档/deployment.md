# 部署到 OrangePi Zero 3

生产推荐：**在本机生成 runtime/storage.json 再传给板子**，板子全程无头。

## 方式 A：本机 `login.py --password` → 传到板子

1. 本机把账号密码填入 `.env`，跑 `python login.py --password`（有头会话）生成 `runtime/storage.json`。
2. 把项目同步到板子（**本机 = 唯一正源，板子单向镜像**，完整流程见 `docs/board-sync-checklist.md` 第 1 节）：
   ```bash
   git bundle create /tmp/spark-mirror.bundle --all && scp /tmp/spark-mirror.bundle BOARD:/tmp/
   ssh BOARD 'git -C /opt/douyin-spark fetch -q /tmp/spark-mirror.bundle "refs/heads/main:refs/remotes/localsrc/main" && git -C /opt/douyin-spark merge --ff-only refs/remotes/localsrc/main && chown -R BOARD:BOARD /opt/douyin-spark'
   ```

账号需重新登录时，见 `docs/operations.md`「登录管理」。

## 手动部署步骤

> 一键部署脚本 `deploy_BOARD.sh` 已弃用并移除。当前按以下手动步骤部署：
> 2026-08-15 起有逐项可勾选的同步清单：`docs/board-sync-checklist.md`（代码同步/时区/健康检查 cron/真机验证）。

1. 依赖：`python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/playwright install chromium`
2. 准备 `config.json`（`engine: chromium`、自动发现、`send_window` 对齐 cron）。
3. **git 兜底**：项目已是 git 仓库。scp 整目录会带上 `.git`；若只传代码文件，板子上执行 `git init && git add -A && git commit -m "chore: board import"`。
4. 板子上配 cron：`crontab -e` 写入 `0 0 * * * /opt/douyin-spark/run_cron.sh`，并确保脚本可执行：`chmod +x /opt/douyin-spark/run_cron.sh && chown -R BOARD:BOARD /opt/douyin-spark`（**scp/覆盖更新脚本会丢 x 位，cron 调用将 Permission denied 且无日志，务必校验**）。
5. **健康检查 cron**（堵静默失败，**晚于主任务最晚可能的收尾时刻**）：`45 1 * * * /opt/douyin-spark/.venv/bin/python /opt/douyin-spark/src/health_check.py --quiet --notify-only-on-change`（如需告警，先在 `config.json` 配 `notify_cmd`，见 `docs/operations.md`）。原为 `15 1`，2026-10-04 随 `timeout 45m → 90m` 推迟到 **01:45**。**"最晚 01:30 收尾"这个前提现在由 `run_cron.sh` 的硬截止 `_HARD_STOP_MIN=95`（01:35）保证**：距 01:35 不足 20 分钟不再起新一轮，每轮 timeout 也按剩余时间收窄（00:00 起的第一轮仍是完整 90m）。否则 rc=3 的重跑能跑到 01:53，而 health_check 见"主任务还在跑"会**不判定地退 0**——那一晚等于没人检查过。
6. 建议配 512M swap 或 zram，保证 chromium 内存余量。

## cron 行为

cron 每天 00:00 调用包装脚本 `run_cron.sh`：

```cron
0 0 * * * /opt/douyin-spark/run_cron.sh
```

```bash
# 脚本内容不在本文档复制(此前已与实际 run_cron.sh 漂移)，正源就是仓库根那份文件。
# 它负责: cd 失败告警 / export DISPLAY=${SPARK_DISPLAY:-:99}(Xvfb 有头) /
#         export SPARK_PERF_PROFILE=low(生产档位焊死, 见"性能档位") /
#         export SPARK_DEADLINE_EPOCH(给 spark.py 算补扫余量的硬截止时刻) /
# timeout 90m 外圈兜底(防进程挂死长驻持 PID 锁; 45m → 90m 见下) / 退出码分流。
```

退出码语义（`run_cron.sh` 分流，`spark.py` 侧勿改）：

| 码 | 含义 | 包装脚本动作 |
|---|---|---|
| 0 | 成功 / 无待续好友 / 窗口外 | 退出 0 |
| 1 | 一般异常 | 冷却 60s 重试 1 次；距硬截止 01:35 不足 20 分钟不再重试（`run_cron.sh` 的 `_left < 20` 判据，不再看墙上时钟） |
| 2 | 未登录 | 直接退出，不重试（提示走 `login.py`） |
| 3 | 本进程内重试仍失败 | 冷却 180s 冷启动重跑，≤3 次 |
| 4 | 未获得 PID 互斥锁（另一实例持锁） | 记录并退出 4，**不计成功**（2026-10-01 新增；此前裸 return 得 rc=0，锁被挂死实例长持时 cron 天天"成功"、静默断火） |
| 124 | 被 `timeout 90m` 杀掉的挂死进程 | 直接退出，显式可见（run_cron 对 124 不重试） |
| 130 | 人工 Ctrl+C | 不重跑 |

- **`timeout 90m`（原 45m，2026-10-04 重排）**：两账号串行发送实测需 **~60 分钟**（逐好友 1.3~3.5 分钟），再叠加 ±5 分钟抖动，45 分钟在**无故障的正常夜里**都不够——10-04 就是发到第 4/8 个被 timeout 杀掉（rc=124，且 run_cron 对 124 不重试）→ 后 5 个好友漏发。90m 留足余量。发送窗口只约束**开始时刻**，跑到凌晨一点不违规；健康检查 cron 因此定在 01:45。
- **timeout 从第 2 轮起按剩余分钟收窄**（`run_cron.sh:51-55`）：第 1 轮固定 90m，之后每轮 `_tmo` 取「距 01:35 的剩余分钟 - 5」，日志「按硬截止收窄 timeout」。被收窄的只会是已经烧过一轮的晚上。
- **性能档位**：`run_cron.sh` 里 `export SPARK_PERF_PROFILE=low` 把生产档位焊死，不依赖 `config.json` 是否写了 `perf_profile`。**不要**在板端配 `mid` / `high` —— 板子的瓶颈是抖音侧渲染不是本机 CPU，高档位只是把超时改短、更快失败；`list_budget_ms`（150s）和 `login_state_deadline_ms`（45s）已三档统一，改档位省不到这两项。排查时先看 `runtime/spark.log` 里的 `[perf] perf_profile=...` 那一行；若出现 `[perf][warn]`，说明环境变量和 `config.json` 不一致（等待按环境变量、浏览器资源按 `config.json`）。
- **硬截止 `_HARD_STOP_MIN=95`（01:35，2026-10-04 评审补上）**：90m 的额度只给"从 00:00 起的第一轮"。rc=3 冷启动重跑若发生在 00:16，一轮 90m 就到 01:46，**越过 01:45 的检查点** → health_check 撞上"主任务还在跑"直接退 0 → 整晚没人判定。所以：距 01:35 不足 20 分钟不再起新一轮（日志「不再起第 N 轮」），起得了的轮次 timeout 收窄到剩余分钟数（日志「按硬截止收窄 timeout」）。**被收窄的只是已经烧过一轮的晚上**，正常一晚不受影响——这条与"45m 砍死正常夜"是相反方向的教训，两者一起才把时间预算钉住。
- `spark.py` 自带 ±5 分钟抖动，无需在 cron 里额外 sleep。
- 窗口（`send_window`）只约束**开始时刻**：启动时一次性判定是否在窗口内，发送循环不强制截止，实际可能发到窗口结束后一小段时间（允许晚发）。
- **陷阱**：不要在 crontab 命令里写 `$(( RANDOM % 600 ))`。Debian cron 会把未转义的 `%` 当作换行符（须转义为 `\%`），导致整条命令语法错误、静默失败。shell 逻辑放进 wrapper 脚本即可绕开。
- `> /dev/null 2>>runtime/spark.log`：`logutil` 已自行写 runtime/spark.log，stdout 再重定向进去会造成每行日志双写；stderr 保留以便捕获崩溃堆栈。
- `spark.py` 自己判断窗口与已发送，非窗口期或今日已发完就静默退出。

## 内存优化（1.5G 板子）

- **必须 headed（有头）+ Xvfb，不能用 headless**：抖音对 headless chromium 会弹登录拦截页，且板子（1.5G、无 GPU）headless 下渲染挂死 → 发现策略全未命中 → 2026-09-30 事故漏发 10 人。生产配置 = `config.json` 里 `"headless": false, "display": ":99"`（`config.example.json` 已同步），Xvfb 由 `xvfb.service` 常驻 `:99`；`health_check.py` 的 `check_headed_config` 会断言这两键，写成 headless 直接 FAIL。启动参数见 `src/session.py` 的 `LAUNCH_ARGS`（禁 GPU、静音、`--disable-dev-shm-usage`；有头分支会**移除** `--disable-software-rasterizer` 并补 SwiftShader 参数，`launch_browser()` 实现）。
- 发送时专门注入 JS 拦截图片/视频资源、降低渲染负载。
- 系统层建议 512M swap，进一步建议开启 zram 压缩内存以延长 eMMC 寿命。
- 默认共享单个浏览器实例（`browser_reuse: true`），每好友仅新建 context/page，处理完按 `browser_recycle_mem_mb`（默认 300MB）查 `MemAvailable`，低于阈值才回收实例；内存紧张异常时可设 `browser_reuse: false` 回到每好友独立实例（见 `docs/operations.md` 配置说明），开启前须板子真机验证 `[mem:*]` 记录。

## 开发约定（与板子操作相关）

- **本机优先调试**：本机 WSL 已安装 python3（3.14.4）并重建 `.venv`（playwright + ruff，版本已锁在 `requirements.txt`），代码编译、`--dry-run`、单测一律在本机 `.venv/bin/python` 上做，跑通后再同步板子。
- **注意版本差异**：板子生产 venv 为 Python 3.11，本机为 3.14；涉及版本相关行为差异的改动，仍需在板子真机复验一次。
- **操作板子走 ssh 免密直连**：`ssh BOARD "<命令>"`（`~/.ssh/config` 已配 Host BOARD = root@192.0.2.10，ControlPersist 复用），传文件用 `scp`。历史方案（ssh-mcp / `ssh_mcp.py` / `sshpass`）已弃用。
