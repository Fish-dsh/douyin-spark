# douyin-spark · 抖音自动续火花

一个跑在 OrangePi Zero 3 上的无人值守脚本：每天自动给**有火花且需续**（以及**置顶好友**）的抖音好友发一条"续火花"，维持火苗不断。

> **免责声明**：本项目仅供个人学习与自动化实践使用。请遵守《抖音用户协议》，并谨慎控制发送频率。使用本脚本产生的任何账号异常、风控、封禁或其他后果，均由使用者自行承担，作者概不负责。

## 工作原理

1. **定时触发**：cron 每天 00:00（**板子本地时**，Asia/Shanghai）调用 `run_cron.sh` 启动 `spark.py`（脚本内部自带 ±5 分钟抖动），非窗口期直接退出。
2. **窗口校验**：`send_window` 配置允许发送的起止时间（HH:MM，按板子本地时判定），只有窗口内才继续。
3. **登录态复用**：优先以 `runtime/storage.json` 中的 cookie/localStorage 恢复登录；若失效且 `config.json` 的该账号配了 `account`/`password`（`auto_login: true`），用账号密码自动重登，失败则记录退避时间避免频繁触发风控。密码来源是 **config.json 的 accounts 字段，不是 `.env`**——`.env` 只服务根 `login.py --password` 这一条人工登录入口（会话 28 已把这条链焊在 `tests/test_scheduler.py::TestRunBrowserReuse` 的断言里）。
4. **自动发现好友**：扫描聊天列表，处理「火花需续」(need) 的好友；已续火花(active) 的好友自动跳过；**置顶好友不受状态检测影响、一律强制发送**（保证置顶好友火花不断）。不再支持 config 指定好友列表。
5. **固定发送"续火花"**：优先发送抖音内置"续火花"表情，表情发送失败时以文字"续火花"兜底。
6. **幂等**：`runtime/state.json` 记录每天已发好友，同一天不重复发送。
7. **健康检查**：`health_check.py` 每天巡检（state 日期=今天 + spark.log 当日有收尾记录），失败执行 `config.json` 的 `notify_cmd` 告警并退出码 1，堵住无人值守的静默失败（详见 `docs/operations.md`）。

## 目录结构

```
douyin-spark/
├── spark.py               # CLI 入口（转调 src/scheduler.run）
├── login.py               # CLI 壳（登录逻辑实现在 src/login.py）
├── run_cron.sh            # cron 入口包装脚本（承载 shell 逻辑，规避 cron 对 % 的解析）
├── ruff.toml              # ruff 配置（豁免 BLE001/S110/S112 并注释原因）
├── config.example.json    # 配置模板，复制为 config.json 后修改
├── requirements.txt       # 依赖：playwright + ruff（版本已锁）
├── logrotate.conf         # logrotate 配置模板（部署时手动复制到 /etc/logrotate.d/）
├── src/                   # 核心逻辑包
│   ├── scheduler.py       # 主流程编排（窗口校验、逐账号处理、失败补扫）
│   ├── spark_status.py    # 火花检测（3策略：CSS class / 文字特征 / 图标扫描）
│   ├── sender.py          # 点击/发送/确认（click_friend 三策略 + reload 重试）
│   ├── discovery.py       # 发现编排（置顶强制续、active 跳过、need 待发）
│   ├── session.py         # 会话生命周期+登录保障+内存控制
│   ├── finder.py          # 选择器工具（wait_chat_list 等）
│   ├── config.py          # 配置/state/窗口判定/.env
│   ├── debug.py           # 截图 dump（runtime/heal_screenshots/）
│   ├── login.py           # 登录逻辑（登录态存取、auto_login 密码重登）
│   ├── self_heal.py       # 自愈门面：错误分类 → 本地恢复策略 → 告警（零 API、不改源码）
│   ├── recovery.py        # 纯函数分类/恢复策略表（自愈的决策表，零依赖）
│   ├── health_check.py    # 健康检查：state 日期+当日日志收尾校验
│   └── streak_history.py  # 火花历史检测（覆盖门禁：近 3 天已续好友应再现今日发现名单）
├── logutil.py             # 日志（控制台 + 持久文件句柄，轮转友好）
├── tests/                 # unittest 全量测试（318 项，含自愈/调度/健康检查/PID 锁）
├── runtime/               # 运行时生成物（gitignored）：spark.log / storage.json / state.json / heal_*
├── .pi/
│   ├── maintainer.md      # pi 的无人值守修复 Agent 提示词
│   └── models.json        # glm provider 的 canonical 源（pi 实际读 ~/.pi/agent/models.json，部署时拷贝过去）
├── docs/
│   ├── deployment.md      # 部署到 OrangePi + cron + 内存优化
│   ├── operations.md      # 配置说明 + 登录管理 + 运维/FAQ
│   ├── self-heal.md       # 自愈机制详解
│   └── decisions/         # 架构决策记录（ADR）
├── README.md              # 本文档：快速开始 + 入口
├── AGENTS.md              # AI agent 操作手册（命令、约定、坑）
└── WORKLOG.md             # 项目工作日志（会话/事故/决策时间线）
```

## 本机快速开始（Linux x86）

先在本机把链路跑通，再部署到板子。

```bash
cd douyin-spark

# 1. 创建虚拟环境并安装依赖
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. 安装 chromium（playwright 依赖）
.venv/bin/playwright install chromium

# 3. 生成配置文件
cp config.example.json config.json   # 编辑配置（好友自动发现，无需填列表）

# 4. 填写登录凭据（账号密码用于登录）
cp .env.example .env 2>/dev/null     # 存在则编辑；否则手动创建
chmod 600 .env                       # 权限收紧，保护账号密码

# 5. 密码登录（用 .env 账号密码生成登录态）
.venv/bin/python login.py --password # 成功 → 生成 runtime/storage.json

# 6. 演练验证（不真正发送，仅测定位链路）
.venv/bin/python spark.py --dry-run

# 7. 手动真实跑一次
.venv/bin/python spark.py
```

## 文档导航

| 文档 | 内容 |
| ---- | ---- |
| [AGENTS.md](AGENTS.md) | AI agent 操作手册（命令、约定、坑、破坏性操作前检查） |
| [docs/deployment.md](docs/deployment.md) | 部署到 OrangePi、cron 配置、内存优化、ssh 免密直连操作约定 |
| [docs/operations.md](docs/operations.md) | config.json 配置、登录管理、常见问题、日常维护 |
| [docs/self-heal.md](docs/self-heal.md) | 自愈机制（运行时自愈 + 本地恢复策略表 + 告警 + 保底重跑） |
| [docs/decisions/2026-08-09-self-heal-pi-design.md](docs/decisions/2026-08-09-self-heal-pi-design.md) | 自愈重写架构决策（ADR） |
| [WORKLOG.md](WORKLOG.md) | 项目工作日志：会话时间线、事故复盘、关键决策 |
