# douyin-spark · 抖音自动续火花

一台 Linux 机器 + 一条定时任务，就能每天自动把该续的聊天火花续掉；跑完自己留账，失败自己喊人。

不要服务器、不要常开电脑：低配小板、旧笔记本、便宜 VPS 都够用（作者最初就是拿一块 1.5G 内存的 ARM 小板长期跑的，所以对弱机很友好）。

> **免责声明**：本项目为个人自动化实践，与抖音官方无关。自动化操作可能触发平台风控，请自行评估风险并遵守平台协议；由此产生的任何后果由使用者自负。

`Python 3.9+` · `Playwright (Chromium)` · `MIT` · `614 单元测试`

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
git clone https://github.com/Fish-dsh/douyin-spark.git
cd douyin-spark

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt      # 只跑主链路(够用)
# 想连视觉兜底一起装(多一个 opencv; 1.5G 内存的机器装它有点重):
#   .venv/bin/pip install -r requirements-vision.txt
.venv/bin/playwright install chromium          # 首次需要下载浏览器
# 若报缺系统库: sudo .venv/bin/playwright install-deps chromium

cp config.example.json config.json             # 好友名单无需配置，扫描自动发现
cp .env.example .env && chmod 600 .env         # 只有用密码登录才需要填；见下方说明

# 生成登录态（二选一）——先确认这台机器有 X 显示能弹浏览器，见下面第一条
.venv/bin/python login.py --manual             # 弹出浏览器，人工扫码/登录后保存
.venv/bin/python login.py --password           # 用 .env 里的账号密码登录

.venv/bin/python spark.py --dry-run            # 演练：只走定位链路，不真发
.venv/bin/python spark.py                      # 真发（窗口内）
```

几句必须知道的话：

- **浏览器必须是"有头"模式**（`headless: false` + Xvfb `:99`），否则抖音会拦、弱机还会渲染挂死。这条不是文档里的建议，而是**启动硬门禁**：`config.json` 里缺 `headless`、写成 `true`、或者 `display` 不是 `:N` 形式，`spark.py` 一律以退出码 5 拒绝启动（桌面临时调试要跑 headless 时设 `SPARK_ALLOW_HEADLESS=1`）。部署前先看 [部署文档](docs/deployment.md)。
- **`.env` 不是必需的**：只有走 `login.py --password`（密码登录）才要往里填账号密码。用 `--manual` 扫码登录的话，`.env` 留空就行。**`.env` / `config.json` / `runtime/` 都不要提交**，已经在 `.gitignore` 里。
- **登录态在 `runtime/storage.json`**。它单独传、别进仓库 —— 把它跟代码一起同步过去这件事做漏了，部署机会去扫码登录而无人值守下没人做，当晚就是静默零发送。细节见部署文档「三类东西分开走」。
- **登录用的是你这台机器当前的 X 显示，跟 `config.json` 的 `display` 没关系**。那条 `:99` 只给 `spark.py` 用（`src/session.py` 把它注入 `DISPLAY`）；`login.py` 读的是当前 shell 的 `DISPLAY`。所以登录前先确认：
  - **有桌面的机器**：直接在桌面会话里跑，`echo $DISPLAY` 应该输出 `:0` 之类。**不要加 `sudo`** —— sudo 会把 `DISPLAY` 丢掉，现象和「没装 X」一模一样。
  - **没有桌面的机器**（服务器 / VPS / 板子）：先起一个虚拟显示，再把它指给登录命令：
    ```bash
    sudo apt install -y xvfb
    Xvfb :99 -screen 0 800x600x24 &                # 部署机上它是 xvfb.service 常驻的
    # 等 socket 真出现再登录: Xvfb 异步起, 命令返回不等于显示就绪
    for i in $(seq 1 20); do [ -S /tmp/.X11-unix/X99 ] && break; sleep 0.5; done
    ls /tmp/.X11-unix/                             # 应该看到 X99
    .venv/bin/python login.py --manual --display :99
    ```
    想在自己电脑上看这个窗口，再加 `ssh -X` 转发。
  - 两者都没有（`DISPLAY` 为空）时 `login.py` 会直接停手并打印上面这两条路，不会抛 Playwright 的 `Missing X server or $DISPLAY` 栈。

## 常用命令

| 想干什么 | 命令 |
| -------- | ---- |
| 演练（不真发） | `.venv/bin/python spark.py --dry-run` |
| 立刻补发（无视时间窗） | `.venv/bin/python spark.py --force` |
| 登录态失效后重新登录 | `.venv/bin/python login.py --manual` |
| 今晚到底成没成 | `.venv/bin/python -m src.health_check` |
| 这周有人断了吗 | `.venv/bin/python -m src.weekly_report` |
| 本地开发（不碰正式账本） | `./run_dev.sh --dry-run`（自动补 `config.json` 占位） |
| 提交前自检 | `./tools/verify.sh`（语法 + JS 语法 + lint + 单测；`--fast` 只做前三项） |

## 配置速查

`config.json`（模板见 `config.example.json`），最常改的就这几项：

| 字段 | 默认 | 说明 |
| ---- | ---- | ---- |
| `send_window` | `23:30`–`00:30` | 允许自动开始的时间窗（本地时），只约束开始时刻 |
| `accounts` | 1 个 `main` | 多账号：每项含 `name` / `storage_file` / `state_file`，可加 `account` / `password` |
| `headless` | `false` | **必须为 false**（抖音会拦无头；弱机无头渲染容易挂死） |
| `display` | `:99` | Xvfb 虚拟显示编号 |
| `perf_profile` | `low` | `low`=低配机 / `mid`=普通笔记本 / `high`=高性能机（不确定就留 `low`） |
| `notify_cmd` | `""` | 失败告警命令，`HEALTH_FAIL_MSG` 环境变量带原因 |

全部字段、环境变量、多账号与登录凭据规则见 [配置文档](docs/configuration.md)。

## 部署（Linux + cron）

```bash
# 1. 放代码（路径可在 run_cron.sh 改）
#    rsync 不读 .gitignore, 排除项得自己写全 —— 少写一个就是把口令/登录态传上服务器
sudo rsync -a --exclude runtime --exclude .venv --exclude .git \
    --exclude config.json --exclude .env --exclude '.env.*' \
    --exclude storage.json --exclude state.json --exclude '__pycache__' \
    ./ /opt/douyin-spark/
#    拷完把属主交回自己, 否则下一步建不了 .venv
sudo chown -R "$USER:$USER" /opt/douyin-spark
cd /opt/douyin-spark && python3 -m venv .venv && .venv/bin/pip install -r requirements-vision.txt
sudo .venv/bin/playwright install-deps chromium                          # 2. 系统库（要 root）
.venv/bin/playwright install chromium                                    #    浏览器本体（当前用户，别用 sudo）
sudo apt install -y xvfb                                                 # 3. Xvfb 常驻 :99
sudo cp deploy/xvfb.service /etc/systemd/system/
sudo systemctl enable --now xvfb                                         #    开机自启并立刻拉起，systemctl status xvfb 应 active
# 4. 登录态（runtime/storage.json）单独传，不要指望第 1 步带上它：
#      scp runtime/storage.json REMOTE:/opt/douyin-spark/runtime/ && ssh REMOTE chmod 600 ...
#    config.json 的账号密码在部署机上自己填，别把本机那份 scp 过去。
# 5. crontab（-e）：
#   0 0 * * *  /opt/douyin-spark/run_cron.sh
#  45 1 * * *  /opt/douyin-spark/.venv/bin/python /opt/douyin-spark/src/health_check.py --quiet --notify-only-on-change
# 注意：cron 不读 PATH，命令一律写绝对路径；run_cron.sh 必须 chmod +x。
```

代码更新有两条路：`SPARK_HOST=USER@HOST ./tools/deploy.sh`（自动备份 + 传完跑冒烟 + 失败回滚），或者手动 `git bundle` / `rsync`。两条路都**不传** `config.json` 与 `runtime/`。

细节（三类东西怎么分开同步、低配机内存优化、Python 版本对齐）见 [部署文档](docs/deployment.md)。

## 已知限制

- **页面改版会让选择器失效**：四条识别策略里三条锚定混淆类名，改版一次同时打掉，靠结构与视觉兜底救场。
- **滑块/行为验证码不处理**：只伪装自动化特征，绝不碰验证码，撞到就退避等下一晚。
- **不做协议逆向**：火花天数走 WebSocket 二进制 protobuf，硬啃是军备竞赛，本项目天花板是"把 UI 操作做稳"。
- **只在 Linux 上验证过**：`run_cron.sh` / Xvfb 那套是 Linux 专有。

## 文档

README 只放主干，细节都在 `docs/`：

| 读者 | 文档 | 内容 |
| ---- | ---- | ---- |
| 人 | [docs/architecture.md](docs/architecture.md) | 一个夜晚的完整链路、每处设计为什么这么定、各模块职责 |
| 人 | [docs/configuration.md](docs/configuration.md) | 全部配置字段、环境变量、登录凭据从哪里读 |
| 人 | [docs/deployment.md](docs/deployment.md) | 部署到 Linux 机器、cron、低配机内存优化 |
| 人 | [docs/operations.md](docs/operations.md) | 退出码、每日巡检、登录管理、时区与 FAQ |
| 人 | [docs/self-heal.md](docs/self-heal.md) | 自愈机制：错误分类、恢复策略表、告警（含与视觉兜底的区别） |
| 人 | [docs/development.md](docs/development.md) | 跑测试、lint、本地开发、提 PR 前自检 |
| Agent | [AGENTS.md](AGENTS.md) | 仓库地图、命令清单、改动约定（给 AI/自动化代理看的） |
| 人 | [docs/notes/](docs/notes/README.md) | **实测笔记**：为什么这个项目必须有头跑 —— headless 在 ARM 小板上的完整失败实测（含原始参数与截图数据） |

## 许可证

[MIT](LICENSE)。欢迎 fork 自用；如果你把它跑在更差的硬件上还成功了，那挺厉害的。
