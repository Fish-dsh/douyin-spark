# 部署到 Linux 机器

推荐：**在有屏幕的机器上登录一次生成 `runtime/storage.json`，再单独传给部署机**；部署机全程无人值守，靠 Xvfb 虚拟显示提供有头环境。

## 三类东西分开走，别混在一起同步

| 东西 | 是什么 | 怎么传 |
| ---- | ------ | ------ |
| 代码 | 仓库里的一切 | `git` / `rsync`，公网可下载 |
| 登录态 | `runtime/storage.json`（cookie + localStorage） | **单独、加密、定向**：`scp`/`rsync` 到目标机，`chmod 600` |
| 账号密码 | `config.json` 的 `accounts` | **不要跨机复制**，部署机自己填，`chmod 600` |
| 账本 | `runtime/state.json`、`spark.log`、`.heal*` | 部署机自己生成，不复制 |

**`runtime/` 一定不会被同步过去**：它整目录在 `.gitignore` 里，`git bundle` 只打包 Git 对象，`README` 的 `rsync` 也带 `--exclude runtime`。这不只是"少传了文件"—— 少了 `storage.json`，部署机首次运行会去扫码登录，而无人值守环境下那一步没人做，结果是**当晚静默零发送、直到第二天 01:45 巡检才知道**。

## 方式 A：本地登录一次 → 只把登录态传过去

1. **本机**（有屏幕的那台）把账号密码填入 `.env`（`cp .env.example .env && chmod 600 .env`，格式见文件内注释），跑 `python login.py --password`（有头会话）生成 `runtime/storage.json`。
2. **代码**同步到部署机（`REMOTE` 换成你自己的 `user@host`）：
   ```bash
   git bundle create /tmp/spark-mirror.bundle --all && scp /tmp/spark-mirror.bundle REMOTE:/tmp/
   ssh REMOTE 'git -C /opt/douyin-spark fetch -q /tmp/spark-mirror.bundle "refs/heads/main:refs/remotes/localsrc/main" && git -C /opt/douyin-spark merge --ff-only refs/remotes/localsrc/main && chown -R USER:GROUP /opt/douyin-spark'
   ```
   bundle 里**没有** `runtime/`（gitignored），这是对的 —— 登录态不该进版本历史。
3. **登录态**单独传，别跟代码搅在一起：
   ```bash
   ssh REMOTE 'mkdir -p /opt/douyin-spark/runtime && chmod 700 /opt/douyin-spark/runtime'
   scp runtime/storage.json REMOTE:/opt/douyin-spark/runtime/
   ssh REMOTE 'chmod 600 /opt/douyin-spark/runtime/storage.json && chown USER:GROUP /opt/douyin-spark/runtime/storage.json'
   ```
4. **账号密码**在部署机的 `config.json` 里自己填（`accounts[].account/password`，`chmod 600 config.json`）。别把本机的 `config.json` scp 过去 —— 那样两台机器密码各改一次就漂了，而失效的那台只会在巡检时才暴露。
5. 传完**先验证一次**再挂 cron：`ssh REMOTE 'cd /opt/douyin-spark && .venv/bin/python spark.py --dry-run'`，日志里该出现 `[login] 复用已保存登录态`，而不是跳登录页。

> 无桌面 VPS 上 `login.py --manual` 起不来（没有 X 显示，浏览器窗口无处可去）。两条路：一是在本机/笔记本上登录一次，按上面第 3 步把 `storage.json` 传过去；二是在 VPS 上装 Xvfb 后跑 `login.py --manual`，用 `ssh -X` 把窗口转发到你本地屏幕扫码。

账号需重新登录时，见 `docs/operations.md`「登录管理」。

## 方式 B：`tools/deploy.sh` 一键推代码

先把远端参数说清楚（脚本不含任何写死的机器名）：

```bash
SPARK_HOST=USER@HOST ./tools/deploy.sh
```

| 环境变量 | 含义 |
| -------- | ---- |
| `SPARK_HOST` | 远端 ssh 目标，支持 `~/.ssh/config` 别名。**必填**，不填脚本直接退出 |
| `SPARK_REMOTE_DIR` | 远端部署目录，默认 `/opt/douyin-spark` |
| `SPARK_REMOTE_USER` | 远端跑服务的用户（决定 `chown` 和跑冒烟的身份），默认取本地 `whoami` |

它做的事，按顺序：打包（**排除** `.venv` / `runtime` / `__pycache__` / `.git` / `runtime-dev` / `*.log` / `config.json`）→ ssh 把远端当前代码与 `config.json` 备份到 `/tmp/spark-prev-<时间戳>.tgz` 和 `/tmp/spark-config-<时间戳>.json` → 上传解包并 `chown` → 远端跑冒烟（`load_config()` + `preflight()` + 打印账号名与 `notify_cmd` 是否配了 + `import src.scheduler`）→ 任一步失败自动回滚代码与配置。

为什么冒烟里要跑 `preflight()`：2026-11-02 那次事故就是手写 `tar` 把本地一个 19 字节的 `config.json` 桩打进去，覆盖掉部署机真正的配置 —— `accounts` 空着，整晚发 0 个好友却返回 `rc=0`，巡检判健康、告警通道也没配，静默断火一整晚。现在 `accounts` 空 / 条目缺 `name` 会抛 `ConfigError`，退出码 5。

> `deploy.sh` **只传代码**。登录态（`runtime/storage.json`）和 `config.json` 的账号密码各机独立，见上面方式 A 的第 3、4 步。

## 手动部署步骤

> 不想用脚本就按下面手动走。部署完逐项核对：代码同步（`git status` 应为干净）／时区（`timedatectl` 应为 Asia/Shanghai）／健康检查 cron（01:45）／当晚真机跑一次看日志收尾。

1. 依赖（**分两步，只有第一步要 sudo**）：
   ```bash
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   sudo .venv/bin/playwright install-deps chromium      # 系统库 apt 安装, 需要 root
   .venv/bin/playwright install chromium                # 浏览器本体, 装进当前用户家目录
   ```
   `install --with-deps chromium` 一步也能装完，但它会把「下载浏览器」也塞进 root 的 `~/.cache/ms-playwright`，之后普通用户跑 cron 反而找不到浏览器；上面两步分开就没有这个坑。
2. 准备 `config.json`（自动发现、`send_window` 对齐 cron；`headless` 必须 `false`）。`engine` 字段已废弃，代码不读，别写。
3. **git 兜底**：项目已是 git 仓库。scp 整目录会带上 `.git`；若只传代码文件，部署机上执行 `git init && git add -A && git commit -m "chore: import"`。
4. 部署机上配 cron：`crontab -e` 写入 `0 0 * * * /opt/douyin-spark/run_cron.sh`，并确保脚本可执行：`chmod +x /opt/douyin-spark/run_cron.sh && chown -R USER:GROUP /opt/douyin-spark`（**scp/覆盖更新脚本会丢 x 位，cron 调用将 Permission denied 且无日志，务必校验**）。
5. **健康检查 cron**（堵静默失败，**晚于主任务最晚可能的收尾时刻**）：`45 1 * * * /opt/douyin-spark/.venv/bin/python /opt/douyin-spark/src/health_check.py --quiet --notify-only-on-change`（如需告警，先在 `config.json` 配 `notify_cmd`，见 `docs/operations.md`）。原为 `15 1`，2026-10-04 随 `timeout 45m → 90m` 推迟到 **01:45**。**"最晚 01:30 收尾"这个前提现在由 `run_cron.sh` 的硬截止 `_HARD_STOP_MIN=95`（01:35）保证**：距 01:35 不足 20 分钟不再起新一轮，每轮 timeout 也按剩余时间收窄（00:00 起的第一轮仍是完整 90m）。否则 rc=3 的重跑能跑到 01:53，而 health_check 见"主任务还在跑"会**不判定地退 0**——那一晚等于没人检查过。
6. 建议配 512M swap 或 zram，保证 chromium 内存余量。
7. **Xvfb 常驻服务**（有头模式的前提，漏了它 `spark.py` 起来就没有 DISPLAY）：
   ```bash
   sudo cp deploy/xvfb.service /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now xvfb
   systemctl is-active xvfb      # 必须输出 active
   ```
   unit 里是 `Xvfb :99 -screen 0 800x600x24 -nolisten tcp`，与 `config.json` 的 `display: ":99"` 对应；改端口要两边一起改，`run_cron.sh` 的 `SPARK_DISPLAY` 也要跟着改。

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

退出码语义（`run_cron.sh` 分流，`spark.py` 侧勿改）：见 [operations.md 的「退出码」一节](operations.md)。

- **`timeout 90m`（原 45m，2026-10-04 重排）**：两账号串行发送实测需 **~60 分钟**（逐好友 1.3~3.5 分钟），再叠加 ±5 分钟抖动，45 分钟在**无故障的正常夜里**都不够——10-04 就是发到第 4/8 个被 timeout 杀掉（rc=124，且 run_cron 对 124 不重试）→ 后 5 个好友漏发。90m 留足余量。发送窗口只约束**开始时刻**，跑到凌晨一点不违规；健康检查 cron 因此定在 01:45。
- **timeout 从第 2 轮起按剩余分钟收窄**（`run_cron.sh:51-55`）：第 1 轮固定 90m，之后每轮 `_tmo` 取「距 01:35 的剩余分钟 - 5」，日志「按硬截止收窄 timeout」。被收窄的只会是已经烧过一轮的晚上。
- **性能档位**：`run_cron.sh` 里 `export SPARK_PERF_PROFILE=low` 把生产档位焊死，不依赖 `config.json` 是否写了 `perf_profile`。**不要**在低配机上配 `mid` / `high` —— 瓶颈在抖音侧渲染而不是本机 CPU，高档位只是把超时改短、更快失败；`list_budget_ms`（150s）和 `login_state_deadline_ms`（45s）已三档统一，改档位省不到这两项。排查时先看 `runtime/spark.log` 里的 `[perf] perf_profile=...` 那一行；若出现 `[perf][warn]`，说明环境变量和 `config.json` 不一致（等待按环境变量、浏览器资源按 `config.json`）。
- **硬截止 `_HARD_STOP_MIN=95`（01:35，2026-10-04 评审补上）**：90m 的额度只给"从 00:00 起的第一轮"。rc=3 冷启动重跑若发生在 00:16，一轮 90m 就到 01:46，**越过 01:45 的检查点** → health_check 撞上"主任务还在跑"直接退 0 → 整晚没人判定。所以：距 01:35 不足 20 分钟不再起新一轮（日志「不再起第 N 轮」），起得了的轮次 timeout 收窄到剩余分钟数（日志「按硬截止收窄 timeout」）。**被收窄的只是已经烧过一轮的晚上**，正常一晚不受影响——这条与"45m 砍死正常夜"是相反方向的教训，两者一起才把时间预算钉住。
- `spark.py` 自带 ±5 分钟抖动，无需在 cron 里额外 sleep。
- 窗口（`send_window`）只约束**开始时刻**：启动时一次性判定是否在窗口内，发送循环不强制截止，实际可能发到窗口结束后一小段时间（允许晚发）。
- **陷阱**：不要在 crontab 命令里写 `$(( RANDOM % 600 ))`。Debian cron 会把未转义的 `%` 当作换行符（须转义为 `\%`），导致整条命令语法错误、静默失败。shell 逻辑放进 wrapper 脚本即可绕开。
- `> /dev/null 2>>runtime/spark.log`：`logutil` 已自行写 runtime/spark.log，stdout 再重定向进去会造成每行日志双写；stderr 保留以便捕获崩溃堆栈。
- `spark.py` 自己判断窗口与已发送，非窗口期或今日已发完就静默退出。

## 内存优化（1.5G 级低配机）

- **必须 headed（有头）+ Xvfb，不能用 headless**：抖音对 headless chromium 会弹登录拦截页，且弱机（1.5G、无 GPU）headless 下渲染挂死 → 发现策略全未命中 → 2026-09-30 事故漏发 10 人。生产配置 = `config.json` 里 `"headless": false, "display": ":99"`（`config.example.json` 已同步），Xvfb 由 `xvfb.service` 常驻 `:99`（unit 文件就在仓库 `deploy/xvfb.service`，装法见下面第 7 步）；`health_check.py` 的 `check_headed_config` 会断言这两键，写成 headless 直接 FAIL。启动参数见 `src/session.py` 的 `LAUNCH_ARGS`（禁 GPU、静音、`--disable-dev-shm-usage`；有头分支会**移除** `--disable-software-rasterizer` 并补 SwiftShader 参数，`launch_browser()` 实现）。
- 发送时专门注入 JS 拦截图片/视频资源、降低渲染负载。
- 系统层建议 512M swap，进一步建议开启 zram 压缩内存以延长 eMMC 寿命。
- 默认共享单个浏览器实例（`browser_reuse: true`），每好友仅新建 context/page，处理完按 `browser_recycle_mem_mb`（默认 300MB）查 `MemAvailable`，低于阈值才回收实例；内存紧张异常时可设 `browser_reuse: false` 回到每好友独立实例（见 `docs/operations.md` 配置说明），开启前须真机验证 `[mem:*]` 记录。

## 开发与调试约定

- **本地先跑通**：编译、`--dry-run`、单测都在本地虚拟环境里做（`python -m venv .venv && .venv/bin/pip install -r requirements.txt`），跑通后再同步到部署机。
- **版本要对齐**：本地与部署机 Python 版本可能不同，涉及版本相关行为的改动，以部署机真机复验一次为准。
- **同步方式自选，但代码和登录态分两趟**：代码走 `git bundle` / `rsync` / `git pull` 都行；`runtime/storage.json` 单独 `scp` 并 `chmod 600`，**账本**（`state.json` / `spark.log`）留在部署机自己生成，别反向覆盖回去。脚本的可执行位要保（`rsync` 加 `-a`，或传完 `chmod +x`）。