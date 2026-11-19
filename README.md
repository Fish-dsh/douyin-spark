# douyin-spark · 抖音自动续火花

一台 Linux 机器 + 一条定时任务，就能每天自动把该续的聊天火花续掉；跑完自己留账，失败自己喊人。

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

## 快速开始（Linux）

```bash
git clone <this-repo> douyin-spark && cd douyin-spark

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium          # 首次需要下载浏览器

cp config.example.json config.json             # 好友名单无需配置，扫描自动发现

# 生成登录态（二选一）
.venv/bin/python login.py --manual             # 弹出浏览器，人工扫码/登录后保存
.venv/bin/python login.py --password           # 用 .env 里的账号密码登录

.venv/bin/python spark.py --dry-run            # 演练：只走定位链路，不真发
.venv/bin/python spark.py                      # 真发（窗口内）
```

浏览器必须是"有头"模式（`headless: false` + Xvfb `:99`），否则抖音会拦截——这条是硬约束，部署前先看 [部署文档](docs/deployment.md)。

## 常用命令

| 想干什么 | 命令 |
| -------- | ---- |
| 演练（不真发） | `.venv/bin/python spark.py --dry-run` |
| 立刻补发（无视时间窗） | `.venv/bin/python spark.py --force` |
| 登录态失效后重新登录 | `.venv/bin/python login.py --manual` |
| 今晚到底成没成 | `.venv/bin/python -m src.health_check` |
| 这周有人断了吗 | `.venv/bin/python -m src.weekly_report` |
| 本地开发（不碰正式账本） | `./run_dev.sh --dry-run` |

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

## 部署（Linux + cron）

```bash
sudo rsync -a --exclude runtime --exclude .venv ./ /opt/douyin-spark/   # 1. 放代码（路径可在 run_cron.sh 改）
cd /opt/douyin-spark && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/playwright install --with-deps chromium                       # 2. 装依赖
sudo apt install -y xvfb                                                 # 3. Xvfb 常驻 :99
# 4. crontab：
#   0 0 * * *  cd /opt/douyin-spark && ./run_cron.sh
#  45 1 * * *  cd /opt/douyin-spark && .venv/bin/python -m src.health_check
```

细节（同步方式、低配机内存优化、Python 版本对齐）见 [部署文档](docs/deployment.md)。

## 已知限制

- **页面改版会让选择器失效**：扫聊天列表靠四条策略（CSS 类名 → 文本特征 → 火焰图标 → 结构兜底），其中只有一条锚定混淆类名；改版通常先打掉前两条，靠后两条与视觉兜底救场。
- **滑块/行为验证码不处理**：只伪装自动化特征，绝不碰验证码，撞到就退避等下一晚。
- **不做协议逆向**：火花天数走 WebSocket 二进制 protobuf，硬啃是军备竞赛，本项目天花板是"把 UI 操作做稳"。
- **只在 Linux 上验证过**：`run_cron.sh` / Xvfb 那套是 Linux 专有。

## 代码地图

想改哪里先看这里（完整链路见 [docs/architecture.md](docs/architecture.md)）：

```
spark.py             入口：参数 → 时间窗校验 → src.scheduler.run()
run_cron.sh          cron 包装：Xvfb 检查、超时兜底、退出码转告警
src/scheduler.py     唯一的编排者：发现 → 逐个好友 → 表情 → 文字 → 视觉兜底
src/discovery.py     扫聊天列表，挑出「有火花且今天没动静」的人
src/spark_status.py  火花天数识别：CSS 类名 → 文本特征 → 火焰图标 → 结构兜底
src/sender.py        发送与确认：表情面板、文字、几何点击、新增气泡判定
src/self_heal.py     错误分类 → 恢复动作 → 告警
src/health_check.py  次日巡检（CLI，也是告警出口）
tools/               诊断脚本：策略直方图、任务看门狗、协议嗅探金丝雀
tests/               全量 `pytest -q` 约 8 秒，不开浏览器、不碰真账号
```

依赖是单向的：`logutil` / `src.config` / `src.perf` 是底座，`src.scheduler` 只往下调，兄弟模块之间互不 import（27 个模块，零循环依赖）。

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

## 许可证

[MIT](LICENSE)。欢迎 fork 自用；如果你把它跑在更差的硬件上还成功了，那挺厉害的。
