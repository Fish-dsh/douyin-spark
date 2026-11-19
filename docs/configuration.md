# 配置

配置文件是 `config.json`（模板见 `config.example.json`）；开发用 `config.dev.json`（`extends` 生产配置，只覆盖桌面机差异）。

## 字段表

| 字段 | 默认 | 说明 |
| ---- | ---- | ---- |
| `send_window` | `23:30`–`00:30` | 允许自动开始的时间窗（**本地时**），只约束开始时刻 |
| `accounts` | **必填，无默认** | 多账号：每项含 `name` / `storage_file` / `state_file`，可加 `account` / `password`。缺这一项时整轮什么都不做（`src/config.py` 只告警不补 `main`） |
| `auto_login` | `true` | 登录态失效时用 `accounts` 里的账号密码重登（**凭据来自 config.json，不是 .env**） |
| `headless` | `false` | **必须为 false**。抖音会拦无头；弱机无头渲染容易挂死。漏写这个键时 `src/session.py::DEFAULT_HEADLESS` 也按 `false`（有头）处理——2026-11-19 审计把旧默认 `True` 改掉了，因为「漏写键 = 静默无头」只会在第二天巡检里才被发现 |
| `display` | `:99` | Xvfb 虚拟显示编号 |
| `perf_profile` | `low` | `low`=低配机 / `mid`=普通笔记本 / `high`=高性能机（决定浏览器参数与全套等待上限） |
| `browser_reuse` | `true` | 多好友共享一个浏览器实例 |
| `browser_recycle_mem_mb` | `300` | 复用模式下，`MemAvailable` 低于该值就回收浏览器实例 |
| `block_resources` | `true` | 只拦 `media`（视频/音频）。图片与字体**故意放行**：一张图都不加载是极强的机器人特征，且实测对常驻内存（RSS）没有帮助（`src/session.py` 的 `_BLOCKED_TYPES` 注释） |
| `self_heal` | `true` | 错误分类 → 本地策略恢复（可用 `SPARK_SELF_HEAL=0` 关掉） |
| `vision_fallback` | `true` | 视觉兜底（`vision_provider` 指定多模态来源；无 key 时自动跳过） |
| `vision_provider` | `agnes` | 视觉兜底用哪家模型（`src/vision.py` 读；`SPARK_VISION` 可临时覆盖） |
| `extends` | — | 继承另一份配置，**只支持一层**（`config.dev.json` 靠它 extends 生产配置）；相对路径按本配置文件所在目录解析 |
| `notify_cmd` | `""` | 失败告警命令，`HEALTH_FAIL_MSG` 环境变量带原因 |

## 环境变量

| 变量 | 作用 |
| ---- | ---- |
| `SPARK_CONFIG` | 指定配置文件路径（只改路径指针） |
| `SPARK_RUNTIME_DIR` | 指定运行时目录（日志/登录态/账本都落这里） |
| `SPARK_PERF_PROFILE` | 覆盖 `perf_profile`（`run_cron.sh` 用它把档位钉死） |
| `SPARK_PERF_<KNOB>` | 临时覆盖单个等待/超时参数，排查用 |
| `SPARK_DEADLINE_EPOCH` | 硬截止时刻，由 `run_cron.sh` 注入，给补扫算余量 |
| `SPARK_SELF_HEAL` | `0` 关闭自愈 |
| `SPARK_SKIP_PID_LOCK` | 跳过 PID 锁（主任务持锁时跑验证子进程专用；**别在线上开**） |
| `SPARK_LOG_FILE` | 指定日志文件（`logutil.py`）；不设时落 `runtime/spark.log` |
| `SPARK_DISPLAY` | `run_cron.sh` 用它在启动前注入 `DISPLAY`（默认 `:99`）；必须早于浏览器启动，事后改 `os.environ` 对已拉起的驱动无效 |
| `SPARK_VISION` | 覆盖 `vision_provider` |

调试开关（默认行为即生产行为，改它们只是在排查单点时用）：`SPARK_PANEL_WAIT_MS`（表情面板等待）、`SPARK_SEND_GAP`（好友间停顿，如 `"3,15"`）、`SPARK_REUSE_PAGE` / `SPARK_PAGE_REUSE_MAX`（页面复用开关与上限）、`SPARK_SKIP_RELOAD`（跳过会话列表刷新）、`SPARK_PERF_SEND_GAP_MS`（拟人化停顿，归 `perf.py` 管）。

## 性能档位

`perf_profile` 决定浏览器启动参数与一整组等待/超时上限，集中在 `src/perf.py`：

| 档位 | 适合 | 感觉 |
| ---- | ---- | ---- |
| `low` | 低配机（4 核 / 1.5G 之类） | 最保守，动作慢但不翻车 |
| `mid` | 普通笔记本 | 折中 |
| `high` | 高性能机 | 节奏快、超时短 |

不确定就用 `low`。`list_budget_ms`（列表预算）和 `login_state_deadline_ms`（登录等待）三档一致，换档位省不到这两项。跑起来后看日志里的 `[perf] perf_profile=...` 一行确认生效；出现 `[perf][warn]` 说明环境变量和 `config.json` 不一致（等待按环境变量、浏览器资源按 `config.json`）。

## 登录凭据从哪来

- **无人值守（cron）**：只读 `config.json` 的 `accounts[].account` / `accounts[].password`。
- **`.env`**：只服务 `login.py --password` 这条人工入口，**不会**被自动重登读取（多账号时回退 `.env` 会跨账号误登，`tests/test_scheduler.py::TestRunBrowserReuse` 守着这条）。
- **登录态文件**：`runtime/storage.json`（Playwright storageState），失效时按 `auto_login` 重登，失败写 `runtime/login_state.json` 并退避 1 小时，避免高频登录招风控。

## 配置与其它地方的一致性

改运行时机（`send_window`、cron 时刻）必须四方一致：`config.json` × `src/config.py` × crontab × 文档。历史上曾误用 UTC 导致窗口永远不命中、静默不发送（退出码 0、日志无异常），**勿再引入时区换算**。

两个已知的不一致（2026-11-19 审计记录，暂不改代码，改前先想清楚）：

1. **巡检不认 `SPARK_CONFIG` / `SPARK_RUNTIME_DIR`**：`src/health_check.py` 自己按 `Path(__file__).parent.parent / "runtime"` 定位状态与日志。也就是说开发目录（`runtime-dev/`）跑出来的东西，巡检看不到；上板后两者同路径，所以生产无感。
2. **不是所有键都由 `src/config.py` 读**：`headless` / `display` / `auto_login` / `notify_cmd` / `vision_*` 由各自的消费方直接读 `cfg`，`config.py` 只负责补默认值与归一（`perf_profile`、`accounts[].*_file`）。加字段时两种路径都有人用，别假设「config.py 里搜不到 = 没人读」。
