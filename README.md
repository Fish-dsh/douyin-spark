# douyin-spark · 抖音自动续火花

一台机器 + 一条定时任务（Linux cron / Windows 计划任务都行），就能每天自动把该续的聊天火花续掉；跑完自己留账，失败自己喊人。

不要服务器、不要常开电脑：低配小板、旧笔记本、便宜 VPS 都够用（作者最初就是拿一块 1.5G 内存的 ARM 小板长期跑的，所以对弱机很友好）。

> **免责声明**：本项目为个人自动化实践，与抖音官方无关。自动化操作可能触发平台风控，请自行评估风险并遵守平台协议；由此产生的任何后果由使用者自负。

`Python 3.11+` · `Playwright (Chromium)` · `MIT` · 全量单元测试（`pytest -q` 末行数字为准，别信写死的数字）

---

## 它能做什么

- **无人值守**：cron 拉起，跑完自己退出；有 PID 锁，两条实例不会打架。
- **自动发现**：不维护好友名单。扫描聊天列表，只处理「有火花且今天没动静」的人；置顶好友无条件强制发。
- **表情优先、文字兜底**：先发内置"续火花"表情，面板打不开才退回文字"续火花"。
- **按天幂等**：`runtime/state.json` 记住今天给谁发过，同一天不重复骚扰。
- **登录态自愈**：优先复用 cookie/localStorage，失效时按配置用账号密码重登，失败退避 1 小时。
- **失败可见**：语义化退出码 + 每日巡检 + 可选告警命令；宁可吵一次，不要静默断火。

## 跑多快

参考机是一块 1.5G 内存的 ARM 小板（Orange Pi Zero3，单核大约只有普通笔记本的十分之一），生产配置 2 个账号、每晚 8 个待续好友：

| | 实测 |
| --- | --- |
| 一夜总耗时 | **6 分 30 秒**——其中约 3 分钟花在「每个账号开一次浏览器、加载一次整页」，剩下 8 个人共 3 分半 |
| 人均 | **27 秒**（会话 51 之前是 138 秒，砍掉每人一次冗余整页重载换来的） |
| 内存峰值 | Chromium 约 **1.28G**，1.5G 的板子压线跑，建议配 512M swap |

真正的瓶颈是抖音页面自己的渲染，不是本机 CPU：换更强的机器收益有限，换更弱的机器也跑得动——因为它大部分时间在等，不在算。

想动性能相关代码之前先看 [板端性能笔记](docs/notes/board-perf.md)：哪几个等待是保命的、哪几条路已经试死、收益天花板在哪。

## 快速开始

Linux：

```bash
git clone <this-repo> douyin-spark && cd douyin-spark

./install.sh                                   # 解释器/依赖/浏览器/Xvfb/配置, 幂等, 缺什么直接说

# 生成登录态（三选一）
.venv/bin/python tools/import_browser_cookies.py --account main   # 复用日常 Edge 里已登好的抖音
.venv/bin/python login.py --manual             # 弹出浏览器，人工扫码/登录后保存
.venv/bin/python login.py --password           # 用 .env 里的账号密码登录

.venv/bin/python spark.py --dry-run            # 演练：只走定位链路，不真发
.venv/bin/python spark.py                      # 真发（窗口内）
```

Windows（PowerShell，命令把 `/` 换成 `\`、`.venv/bin/` 换成 `.venv\Scripts\`）：

```powershell
git clone <this-repo> douyin-spark; cd douyin-spark

py -3 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\playwright install chromium

copy config.example.json config.json

.venv\Scripts\python tools\import_browser_cookies.py --account main   # 或 login.py --manual 扫码
.venv\Scripts\python spark.py --dry-run
.venv\Scripts\python spark.py
```

浏览器必须是"有头"模式（`headless: false`），否则抖音会拦截——这条是硬约束。Linux 上靠 Xvfb 提供显示号（`display: ":99"`），Windows 直接用桌面会话，`display` 留空即可。部署前先看 [部署文档](docs/deployment.md)。

## 常用命令

| 想干什么 | 命令 |
| -------- | ---- |
| 昨晚到底成没成（一句话） | `python spark.py --status` |
| 演练（不真发） | `python spark.py --dry-run` |
| 立刻补发（无视时间窗） | `python spark.py --force` |
| 登录态失效后重新登录 | `python login.py --manual` |
| 复用浏览器里已登好的抖音 | `python tools/import_browser_cookies.py --account main` |
| 配置/收尾/有头/显示四项巡检 | `python -m src.health_check` |
| 这周有人断了吗 | `python -m src.weekly_report` |
| 提交前自检 | `python tools/verify.py` |
| 本地开发（不碰正式账本） | `./run_dev.sh --dry-run` |

想省掉扫码：`import_browser_cookies.py` 会让你日常在用的 Edge/Chrome 临时加载一个只申请 `cookies` 权限的小扩展，把已登录会话导成 `runtime/storage_<账号>.json`——主链路照旧读这个文件，发送代码一行没改。要求那个浏览器先**完全退出**（后台残留进程会让新窗口把参数移交给老进程，扩展不加载）。

## 配置速查

`config.json`（模板见 `config.example.json`），最常改的就这几项：

| 字段 | 默认 | 说明 |
| ---- | ---- | ---- |
| `send_window` | `23:30`–`00:30` | 允许自动开始的时间窗（本地时），只约束开始时刻 |
| `accounts` | **必填**（无默认） | 多账号：每项含 `name` / `storage_file` / `state_file`，可加 `account` / `password`；缺这一项整轮什么都不做 |
| `headless` | `false` | **必须为 false**（抖音会拦无头；弱机无头渲染容易挂死）。漏写这个键也按 `false`（有头）跑 |
| `display` | `:99` | Xvfb 虚拟显示编号 |
| `perf_profile` | `low` | `low`=低配机 / `mid`=普通笔记本 / `high`=高性能机（不确定就留 `low`） |
| `notify_cmd` | `""` | 失败告警命令，`HEALTH_FAIL_MSG` 环境变量带原因 |

全部字段、环境变量、多账号与登录凭据规则见 [配置文档](docs/configuration.md)。

## 部署

Linux + cron：

```bash
sudo rsync -a --exclude runtime --exclude .venv ./ /opt/douyin-spark/   # 1. 放代码（路径可在 run_cron.sh 改）
sudo apt install -y xvfb                                                 # 2. 虚拟显示（有头浏览器的前提）
cd /opt/douyin-spark && ./install.sh                                     # 3. 依赖 + 浏览器 + 配置, 幂等可重跑
# 4. crontab：
#   0 0 * * *  cd /opt/douyin-spark && ./run_cron.sh
#  45 1 * * *  cd /opt/douyin-spark && .venv/bin/python -m src.health_check
```

Windows + 计划任务（`run_cron.ps1` 与 `run_cron.sh` 判据一一对应：同样的 90 分钟首轮、01:35 硬截止、退出码分流与重试矩阵）：

```powershell
schtasks /Create /TN douyin-spark /SC DAILY /ST 00:00 /TR ^
  "powershell -NoProfile -ExecutionPolicy Bypass -File C:\douyin-spark\run_cron.ps1"
schtasks /Create /TN douyin-spark-check /SC DAILY /ST 01:45 /TR ^
  "powershell -NoProfile -ExecutionPolicy Bypass -Command ^"cd C:\douyin-spark; .venv\Scripts\python -m src.health_check --quiet --notify-only-on-change^""
```

细节（同步方式、低配机内存优化、Python 版本对齐）见 [部署文档](docs/deployment.md)。

## 已知限制

- **页面改版会让选择器失效**：扫聊天列表靠四条策略（CSS 类名 → 文本特征 → 火焰图标 → 结构兜底），其中只有一条锚定混淆类名；改版通常先打掉前两条，靠后两条与视觉兜底救场。
- **滑块/行为验证码不处理**：只伪装自动化特征，绝不碰验证码，撞到就退避等下一晚。
- **不做协议逆向**：火花天数走 WebSocket 二进制 protobuf，硬啃是军备竞赛，本项目天花板是"把 UI 操作做稳"。
- **Windows 只是能用，不是验证过的生产环境**：2026-11-20 在本机 Windows 上跑过 `spark.py --dry-run` 演练（浏览器真的起来、打开抖音聊天页、判未登录、按退出码 2 收场），但整套链路的生产验证仍在 Linux 板子上做。Windows 上没有 Xvfb，有头模式直接用桌面会话（`display` 留空）。
- **没装 playwright 浏览器时**：`playwright install chromium` 只装 playwright 自带那份；本机已有 Chrome/Edge 的话，设 `PLAYWRIGHT_CHROMIUM_PATH` 指向 `chrome.exe` 也能跑。启动失败会打印期望路径与两条修法，不会只甩一段英文栈。

## 代码地图

想改哪里先看这里（完整链路见 [docs/architecture.md](docs/architecture.md)）：

```
install.sh           一条命令装好：解释器/依赖/浏览器/Xvfb/配置（幂等，--check 只检查）
spark.py             入口：参数 → (--status 走只读查询) → 时间窗校验 → src.scheduler.run()
run_cron.sh          cron 包装（Linux）：超时兜底、硬截止、退出码转重试
run_cron.ps1         同一套判据的 Windows 版（计划任务入口）
src/scheduler.py     唯一的编排者：发现 → 逐个好友 → 表情 → 文字 → 视觉兜底
src/runtime_lock.py  PID 锁：跨进程互斥，拿不到就退 4
src/discovery.py     扫聊天列表，挑出「有火花且今天没动静」的人
src/spark_status.py  火花天数识别：CSS 类名 → 文本特征 → 火焰图标 → 结构兜底
src/sender.py        发送与确认：表情面板、文字、几何点击、新增气泡判定
src/self_heal.py     错误分类 → 恢复动作 → 告警
src/status.py        一句话状态：最近一夜发了几个、失败在哪、下一步查什么
src/health_check.py  次日巡检（CLI，也是告警出口）
tools/               自检入口 verify.py + 诊断脚本：策略直方图、任务看门狗、协议嗅探金丝雀
tests/               全量 `pytest -q`，不开浏览器、不碰真账号（耗时看末行）
```

依赖是单向的：`logutil` / `src.config` / `src.perf` 是底座，`src.scheduler` 只往下调，兄弟模块之间互不 import（23 个模块，零循环依赖）。

## 文档

README 只放主干，细节都在 `docs/`：

| 读者 | 文档 | 内容 |
| ---- | ---- | ---- |
| 人 | [docs/architecture.md](docs/architecture.md) | 一个夜晚的完整链路、每处设计为什么这么定、各模块职责 |
| 人 | [docs/configuration.md](docs/configuration.md) | 全部配置字段、环境变量、登录凭据从哪里读 |
| 人 | [docs/deployment.md](docs/deployment.md) | 部署到 Linux 机器、cron、低配机内存优化 |
| 人 | [docs/operations.md](docs/operations.md) | 退出码、每日巡检、登录管理、时区与 FAQ |
| 人 | [docs/self-heal.md](docs/self-heal.md) | 自愈机制：错误分类、恢复策略表、告警 |
| 人 | [docs/development.md](docs/development.md) | 跑测试、lint、本地开发、提 PR 前自检 |
| Agent | [AGENTS.md](AGENTS.md) | 仓库地图、命令清单、改动约定（给 AI/自动化代理看的） |
| 人 | [docs/notes/](docs/notes/README.md) | **研发实况笔记**：会话工作日志、小板开 GPU 的实测报告、每日交接单（已脱敏） |

## 参与

[![](https://img.shields.io/badge/powered_by-dsh-4D6BFE?style=flat-square&logo=deepseek&logoColor=white)](https://github.com/deepseek-ai/deepseek-harness)

## 许可证

[MIT](LICENSE)。欢迎 fork 自用；如果你把它跑在更差的硬件上还成功了，那挺厉害的。
