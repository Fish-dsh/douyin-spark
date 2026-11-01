# 配置与运维

## 配置说明（config.json）

| 字段 | 类型 | 说明 |
| ---- | ---- | ---- |
| `send_window` | object | `{ "start": "23:30", "end": "00:30" }` 允许发送时间段，跨天（如 `21:00–09:00`）也会正确处理；cron 在 00:00 运行必须保证该时刻在窗口内 |
| `auto_login` | bool | `true` 且该账号在 `accounts` 里配了 `account`/`password` 时，登录态失效自动密码重登。**凭据来源是 config.json，不是 `.env`**——多账号下回退 `.env` 会跨账号误登（会话 28 已在 `tests/test_scheduler.py` 焊死此断言）；`.env` 只服务根 `login.py --password` 人工登录入口 |
| `block_resources` | bool | `true` 时屏蔽图片/视频等非关键资源，降低内存占用（默认 `true`） |
| `browser_reuse` | bool | `true` 时所有待续好友共享单个浏览器实例，每好友仅新建 context/page，省冷启动；`false` 时每好友独立浏览器（默认 `true`；若内存累积严重可设 `false`，通过 `[mem:*]` 日志观察） |
| `headless` / `display` | bool / string | **生产必须 `"headless": false, "display": ":99"`**（chromium 跑在 Xvfb 虚拟显示上）。headless 会被抖音拦截且板子上渲染挂死（2026-09-30 漏发 10 人），`health_check.check_headed_config` 会断言这两键 |
| `self_heal` | bool | 本地恢复/告警总开关（错误分类 → 策略表 → 告警，**零 API、不改源码**）。优先级 env `SPARK_SELF_HEAL=1/0` > 本字段 > 默认**开** |
| `engine` | string | **已废弃，代码不读取**（曾从示例配置移除）；浏览器引擎固定 chromium |
| `friends` | array | **已废弃**，脚本固定自动发现，忽略此字段 |
| `notify_cmd` | string | **可选**。健康检查（`health_check.py`）失败时执行的 shell 命令（如 webhook curl）。失败摘要通过环境变量 `HEALTH_FAIL_MSG` 传入；留空则不执行 |

好友列表不手动配置，脚本自动扫描聊天列表中「火花需续」的好友并发送固定内容"续火花"（表情优先，文字兜底）。**置顶好友（聊天列表顶部带置顶标记）不受状态检测限制，无论检测为已续/未续一律强制发送**，避免置顶好友（如重要联系人）因状态误判而断火花。

## 性能档位与传参（2026-11-01）

档位同时决定**浏览器资源上限**（`src/session.py:build_launch_args`）和**全流程等待/超时/轮询/预算**（`src/perf.py`），两处共用 `config.json` 的 `perf_profile` 字段。

| 档位 | 目标机器 | 浏览器资源 | 单好友固定等待 |
|---|---|---|---|
| `low`（缺省/生产） | 板子 4 核 / 1.5G | 单渲染进程、256MB JS 堆、禁 GPU | 3.35s |
| `mid` | 普通笔记本 4-8 核 / 8G 无独显 | 2 渲染进程、1GB JS 堆 | 2.13s |
| `high` | 桌面 18 核 / 15.7G | 放开 GPU、4 渲染进程、2GB JS 堆 | 1.31s |

- **生产不配 `perf_profile`**：`run_cron.sh` 里 `export SPARK_PERF_PROFILE=low` 焊死；缺字段、写错档位名都会被 `src/config.py` 静默回落 `low` 并打一行警告。
- **本机开发走 `./run_dev.sh`**：它设 `SPARK_CONFIG=config.dev.json`（`extends: config.json` + `perf_profile: high` + `headless: false` + `browser_reuse: true` + `display: null`）与 `SPARK_RUNTIME_DIR=runtime-dev`，state/storage/日志/截图全隔离。**不要**在板子上跑 `run_dev.sh` —— 幂等记录落在 `runtime-dev/`，「今天已发送」读不到，同一天会重复续火花。
- 单个值临时覆盖：`SPARK_PERF_<KNOB大写>`，如 `SPARK_PERF_PANEL_OPEN_MS=9000`；越界或非法值回落默认，不抛异常。
- `SPARK_PERF_PROFILE` 环境变量只改「等待/超时」，**不改浏览器资源**（后者只读 `config.json`）。两者不一致时启动日志打 `[perf][warn]`。要整体试档请改 `config.json` 的 `perf_profile`。
- 查看当前档位：`python src/perf.py` 打印三档完整对照表，`python src/perf.py high` 打印该档摘要；运行中看 `runtime/spark.log` 的 `[perf] perf_profile=...` 行。

## 时间口径（重要）

**脚本内部所有时间判定一律按「机器本地时」**（不用 UTC）：

- `send_window` 起止（`config.json`）按**本地时**解析与判定（板子时区 Asia/Shanghai，窗口 23:30–00:30 即北京 23:30–00:30，cron `0 0 * * *` 的 00:00 北京时刻命中）；
- `state.json` 的 `date` 用**本地日期**（`datetime.now()`）；
- `health_check.py` 的「今天」也用本地时。

> 历史教训（2026-08-15）：重构时曾误用 UTC——cron 北京 00:00 = UTC 16:00，永远不在 UTC 窗口 23:30–00:30 内，会像历史事故一样静默不发送（退出码 0 无日志异常）。核对板子后已改回本地时，**勿再引入时区**。改运行时机配置必须过「四方一致」：`config.json` × `src/config.py` × crontab × 本文档。

## 健康检查（堵静默失败）

无人值守的静默失败（cron 脚本丢执行权限、板子断网、登录失效、调度未收尾）不会主动报错，靠 `health_check.py` 定时巡检兜底：

```bash
.venv/bin/python src/health_check.py                    # 完整检查
.venv/bin/python src/health_check.py --quiet            # 健康时静默（cron 用）
.venv/bin/python src/health_check.py --notify-only-on-change  # 状态翻转才提醒
```

检查项：① `runtime/state.json` 的 `date` 等于今天（本地时）；② `runtime/spark.log` 含今天的日期，且 **`config.accounts` 里每个账号**都有当日收尾（`==== 账号 X 完成:`、`今日…没有需要续火花的好友, 账号 X 结束` 或明确的 `【账号 X … 跳过】`；跳过=该账号今日没续火花，同样判失败）。任一不满足即失败：打印诊断到 stderr、执行 `notify_cmd`（若配置）、退出码 1。**2026-10-04 修正**：老口径只统计"出现过完成行"的账号，于是账号 A 正常收尾就足以让整轮判健康——账号 B 被 `timeout` 杀在半路、5 人漏发也毫无告警。**任务还在跑时不判定**：`runtime/.spark.pid` 指向活的 `spark.py` 时直接退出 0 且不改 `.health_failed`（判定过早=假告警；陈旧锁/PID 复用不算运行中）。

cron 建议（板子，晚于 00:00 主任务）：

```cron
15 1 * * * /opt/douyin-spark/.venv/bin/python /opt/douyin-spark/src/health_check.py --quiet --notify-only-on-change
```

`--notify-only-on-change` 用 `runtime/.health_failed` 标记避免连续失败刷屏；恢复健康自动清除。**注意**：本脚本刻意不写 `spark.log`，避免自身的日志满足日志检查（自证循环）。

## 登录管理

- **storage.json 复用**：`login.py` 密码重登或自动重登成功后，把 cookie 与 localStorage 写入 `runtime/storage.json`。`spark.py` 每次启动先恢复该状态；`is_logged_in()` 检测登录弹层、已登录路径、用户信息元素与会话 cookie 综合判定。
- **密码登录（自动重登）**：`config.json` 开 `auto_login` 且 `.env` 填好 `DOUYIN_ACCOUNT` / `DOUYIN_PASSWORD`，登录态失效时脚本自动走账号密码重登（`login.py --password` 的按钮流程）。出现滑块/行为验证或超时即失败。
- **有效期与退避**：登录态自然有效期约数月；失效被 `auto_login` 自动补救。每次密码登录失败会记录 `last_fail` 到 `runtime/login_state.json`，1 小时内不再次重试，避免高频登录取证风控。

## 常见问题

| 问题 | 说明 |
| ---- | ---- |
| 找不到好友 | 未命中完整匹配时自动回退到模糊文本 / `title` / `data-name` 属性兜底；仍失败则该好友标记 fail 跳过，不中断其他好友 |
| 火花状态误判 | 火花检测基于图标 class（灰=需续，正常=已续），若抖音改版导致误判，需更新 `spark_status.py` 的选择器；**置顶好友已强制续，不受该误判影响** |
| 滑块验证 | 出现在密码自动登录时；脚本记录失败时间退避，下一次窗口重试 |
| 内存不足 | 检查 swap/zram；确认禁 GPU 与 `block_resources: true`（**不要改成 headless**——有头跑在 Xvfb 上是为了绕开抖音拦截，改 headless 会渲染挂死漏发）；缩小 viewport；默认已开 `browser_reuse: true` 共享浏览器，若仍不足可观察 `runtime/spark.log` 的 `[mem:*]` 记录判断是否需要降低 `browser_recycle_mem_mb` 阈值或设 `browser_reuse: false` |
| 连续出现 `rc=4` | `run_cron.sh` 记的「未获得 PID 锁」：正常只该在两个实例重叠时偶发；**连续多天**说明有进程挂死没释放锁 → 查 `runtime/.spark.pid` 里的 PID 是否存活（`ps -p`），必要时手工 kill 并删该文件（外圈已有 `timeout 90m` 兜底，长持锁会被杀成 rc=124） |
| `rc=124` | 进程跑满 timeout 仍没收尾——通常浏览器真挂死或逐好友发送累计超时；同日 01:45 健康检查会逐账号报「无收尾记录」。**日志里若出现「按硬截止收窄 timeout」或「不再起第 N 轮」**，说明这一晚已经烧过至少一轮，`run_cron.sh` 的正是不让它越过 01:35 硬截止 |
| 手工跑验证子进程被锁挡住 | 主任务持锁期间手工跑 `spark.py --dry-run/--verify-send` 会以 rc=4 跳过 → 需要 `SPARK_SKIP_PID_LOCK=1 .venv/bin/python spark.py --dry-run --no-jitter` |
| 登录失效当天 | 自动重登失败当日不再重试（`runtime/login_state.json` 退避），记录 `last_fail`，次日窗口再试 |

## 维护

- **查看日志**：`tail -f runtime/spark.log` 实时观察；每轮结束会打印「成功 / 失败」统计（跳过项不再单独计数，见日志中「已发送/已续/非待续」的逐条跳过行）。日志由 logrotate 按天轮转（模板 `logrotate.conf`，`rotate 40` + gzip），不会撑爆磁盘：一晚实测 ~100KB 原文 / ~12KB gzip，40 份上限 ≈ 0.5~2MB。
- **月度回看**：`python tools/strat_stats.py --all --days 30` 一次读尽 `runtime/spark.log*`（含 `.gz`）；只读默认入口时它只看当前那份，历史轮转不会被统计。
- **手动强制发送**：`python spark.py --force` 忽略发送窗口与今日幂等限制，立即尝试给所有需续好友发送（用于补发或测试）。
- **发送链路验证**：`python spark.py --verify-send` 窗口内对**全部待续好友**（含置顶强制续）逐一真实发送并确认（会写 `state.json` 防重发，通过要求日志有「发送成功」正向证据）。
- **重新登录**：登录态彻底失效时，重跑 `python login.py --password`（用 `.env` 账号密码）。

## 开发约定

- **代码不写死好友昵称/手机号等隐私**，凭据只放 `.env`（chmod 600）与 `config.json`。
- **发送内容固定**"续火花"，表情优先、文字兜底；「表情面板关闭」视为发送成功，不再用消息数二次确认。
- 项目历史与决策记录见 `WORKLOG.md` 与 `docs/decisions/`。
