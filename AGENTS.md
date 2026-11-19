# AGENTS.md · 给 AI / 自动化代理的仓库说明

这个文件写给**在你仓库里干活的代理**（也适合新接手的人）。人看的文档在 `README.md` 与 `docs/`。

## 项目是什么

抖音「聊天火花」自动续期工具：cron 每天 00:00 拉起，扫聊天列表，把「有火花但今天没互动」的人用**内置"续火花"表情**续掉，成功判据是**表情面板关闭**，失败降级发文字"续火花"。

## 硬约束（改代码前先记住）

1. **`headless` 必须为 `false`**，跑在 Xvfb `:99` 上。改成无头 = 抖音拦 + 弱机渲染挂死，`src/health_check.py::check_headed_config` 会断言，测试也会失败。配置里**漏写这个键也按 `false`（有头）跑**（`src/session.py` 的 `DEFAULT_HEADLESS`），因为「漏键 = 静默无头」只会在第二天巡检时才发现。
2. **不要新增 LLM / 外部 API 依赖到发送主链路**。自愈曾经由 LLM 改源码实现，实盘从未成功过一次，已经降级成「错误分类 → 本地策略表 → 告警」。
3. **不碰验证码**。`src/stealth.py` 只伪装自动化特征（`navigator.webdriver` 之类），撞到滑块就退避等下一晚。
4. **不做协议逆向**。火花天数走 WebSocket 二进制 protobuf，`src/net_sniff.py` 只是诊断金丝雀，不要把它变成主策略。
5. **时间预算是三联的**：`run_cron.sh` 的 `timeout 90m` ↔ 硬截止 `_HARD_STOP_MIN=95`（01:35）↔ 巡检 cron 01:45。改一个必须回头看另外两个，否则会出现"任务跑到巡检之后，整晚没人判定"。这三份副本都是**文本常量、没有 Python 代码引用**，由 `tests/test_budget_consistency.py` 断言 `run_cron.sh` 与两份文档的原文守着——改了数字却没改测试，测试会立刻失败。
6. **退出码是外部契约**：0 完成/无待续/窗口外、1 一般异常、2 未登录、3 需冷启动重跑、4 未拿到 PID 锁（**不得当成功**）、124 超时被杀、130 人工中断。`run_cron.sh` 按它分流，别在 `spark.py` 里随手改。
7. **判进程存活只能用 `src/runtime_lock.pid_alive()`**。`os.kill(pid, 0)` 在 POSIX 上只是探活，在 **Windows 上会 `TerminateProcess` 掉目标进程**（sig 参数被忽略）——2026-11-19 就是这么把 pytest 自己杀掉的，同一行代码在生产上的含义是「第二个实例杀掉第一个实例」而不是「拒绝启动」。PID 互斥、陈旧锁接管、`SPARK_SKIP_PID_LOCK` 豁免全在 `src/runtime_lock.py` 里，不要往别处复制。
8. **平台相关的系统调用要写进模块并留单测**。仓库在 Windows 上开发、在 Linux 上运行，遇到「只有某个平台才有 / 语义相反」的 API（`/proc`、`OpenProcess`、`os.kill`），做法是抽象到一个模块里、用 `sys.platform` 分流，并在 `tests/` 里直接打那个模块——不要在业务函数里加 `if win32` 分支。

## 仓库地图

```
douyin-spark/
├── spark.py / login.py / logutil.py   # 入口与日志
├── run_cron.sh / run_dev.sh           # 定时入口 / 开发入口
├── config.example.json / config.dev.json / .env.example
├── src/                               # 全部业务逻辑（见 docs/architecture.md 的模块表）
├── tools/                             # 运维脚本，不参与发送主链路
├── tests/                             # unittest 全量测试
├── docs/                              # architecture / configuration / deployment / operations / self-heal / development
└── runtime/                           # 运行时生成物（gitignored）
```

模块职责、调用链、每个设计为什么这么定：`docs/architecture.md`。需要改某块逻辑前先读对应文件**开头的注释**，那里写了踩坑原因。

## 常用命令

```bash
python -m compileall -q src tests tools spark.py login.py logutil.py   # 语法自检（先跑这个）
.venv/bin/python -m unittest discover -s tests -v                     # 全量测试
.venv/bin/ruff check .                                                # 静态检查
.venv/bin/python spark.py --dry-run                                   # 演练：只走定位链路，不真发
./run_dev.sh --dry-run                                                # 本地开发（runtime-dev/，不碰正式账本）
.venv/bin/python -m src.health_check                                  # 巡检：今晚成没成
.venv/bin/python -m src.weekly_report                                 # 周报（只读日志）
```

## 改动约定

- 改完先 `compileall`，再跑 `unittest`；涉及 DOM 选择的改动，说明清楚用哪条策略兜底。
- 新增等待/超时不要写字面量，加到 `src/perf.py` 的 `_SPECS`，由 `perf_profile` 统管（可用 `SPARK_PERF_<KNOB>` 临时覆盖）。
- 新增配置字段要同时改：`config.example.json`、`src/config.py`、`docs/configuration.md`；改运行时机还要改 `docs/operations.md` 的四方一致那节。
- 日志用中文、面向"明天早上看日志的人"；机器读的内容一律进 `src/events.py` 的 JSONL，不要混进日志。
- 不要把 `config.json`、`.env`、`runtime/`、截图、dump 提交进仓库。
- 提交信息用一句话说清"改了什么、为什么"；一次提交只做一件事。

## 文档该放哪

| 想写的内容 | 放哪 |
| ---------- | ---- |
| 链路、模块职责、设计理由 | `docs/architecture.md` |
| 配置字段、环境变量、凭据来源 | `docs/configuration.md` |
| 部署、cron、低配机调优 | `docs/deployment.md` |
| 退出码、巡检、登录管理、FAQ | `docs/operations.md` |
| 自愈策略与告警 | `docs/self-heal.md` |
| 测试、lint、提交前自检 | `docs/development.md` |
| 会话时间线、挂起事项、每个决定为什么这么定 | `docs/notes/worklog.md` |
| 给代理的约定（本文） | `AGENTS.md` |

README 只放主干和导航，别把细节堆回去。
