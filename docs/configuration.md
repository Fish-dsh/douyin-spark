# 配置

配置文件是 `config.json`（模板见 `config.example.json`）；开发用 `config.dev.json`（`extends` 生产配置，只覆盖桌面机差异）。

## 字段表

| 字段 | 默认 | 说明 |
| ---- | ---- | ---- |
| `send_window` | `23:30`–`00:30` | 允许自动开始的时间窗（**本地时**），只约束开始时刻 |
| `accounts` | 1 个 `main` | 多账号：每项含 `name` / `storage_file` / `state_file`，可加 `account` / `password`。**不能为空**：`src/config.py::preflight()` 会拦下空列表、非对象条目、缺 `name` 的条目，退出码 5（见 [operations.md](operations.md) 的退出码一节） |
| `auto_login` | `true` | 登录态失效时用 `accounts` 里的账号密码重登（**凭据来自 config.json，不是 .env**） |
| `headless` | `false` | **必须为 false**。抖音会拦无头；弱机无头渲染容易挂死 |
| `display` | `:99` | Xvfb 虚拟显示编号 |
| `perf_profile` | `low` | `low`=低配机 / `mid`=普通笔记本 / `high`=高性能机（决定浏览器参数与全套等待上限） |
| `browser_reuse` | `true` | 多好友共享一个浏览器实例 |
| `browser_recycle_mem_mb` | `300` | 复用模式下，`MemAvailable` 低于该值就回收浏览器实例 |
| `block_resources` | `true` | 拦截图片/字体/媒体，省带宽与内存 |
| `self_heal` | `true` | 错误分类 → 本地策略恢复（可用 `SPARK_SELF_HEAL=0` 关掉） |
| `vision_fallback` | `true` | 视觉兜底：四条 DOM 策略全失效时，用多模态模型粗定位 + DOM 现场取证找回会话行。`vision_provider` 指定主用来源（默认 `agnes`，备 `glm`）。**没配任何模型 key 时自动空转**（日志一行「未配置视觉模型 key…跳过」），不会影响主流程。可用 `SPARK_VISION=0` 关掉 |
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
| `SPARK_SKIP_PID_LOCK` | 跳过 PID 锁（只给测试用，**别在线上开**） |

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
