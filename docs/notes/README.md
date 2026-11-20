# 项目笔记（Project Notes）

这几份是 douyin-spark 的实际研发记录，不是使用说明。文档里的 IP、手机号、好友昵称等已经全部替换成占位符（`BOARD_IP` / `ACCOUNT_B` / `FRIEND_A` 之类），可以直接读。

| 文件 | 讲什么 |
|---|---|
| [worklog.md](worklog.md) | 会话工作日志（会话 1–58，最新一条在「会话时间线」顶部）。每次动手的目标、踩的坑、怎么定位、最后的判断依据。想知道"为什么代码长成这样"看这里。 |
| [board-gpu.md](board-gpu.md) | 那块 1.5G 内存 ARM 小板开 GPU 的完整实测：能开成什么样、开完之后 headless 能不能替掉 Xvfb（结论：不能）。含每一步的原始测量值。 |
| [board-perf.md](board-perf.md) | 板端性能：三代耗时、6 分半怎么拆的、发现页交接优化、还能试什么、别重走哪些路。 |
| [architecture-review.md](architecture-review.md) | 架构体检（2026-11-19）：27 个模块的依赖形状与行数、测试比例、巨物清单、建议的拆分顺序，以及已经拆掉的那几个。 |
| [daily-handover.md](daily-handover.md) | 每天自动刷新的交接单：昨晚发了几个、有没有失败、今天要不要管。作者本人只读这一页。 |

## 读的顺序

1. [README.md](../../README.md) —— 这项目是干什么的、怎么跑起来。
2. [docs/architecture.md](../architecture.md) —— 代码怎么分层。
3. [worklog.md](worklog.md) —— 逐个会话的具体决策与事故复盘。
4. [architecture-review.md](architecture-review.md) —— 想动 src/ 结构之前看：谁依赖谁、哪几个函数该先拆。
5. [board-perf.md](board-perf.md) —— 想改性能相关代码之前先看：哪些等待是保命的、哪些路已经试死了。

## 关于 worklog 里的占位符

| 占位符 | 指代 |
|---|---|
| `BOARD_IP` | 跑生产的板子内网地址 |
| `ROUTER_IP` | 路由器管理地址 |
| `ACCOUNT_B` / `ACCOUNT_C` / `TEST_PHONE` | 登录用的手机号 |
| `FRIEND_A` … `FRIEND_M` | 好友昵称 |
| `BOARD_SUBNET` | 板子所在网段 |
| `DEVUSER` / `DEVUSER_OLD` | 本机与旧环境的用户名 |

脱敏是脚本一次性替换的，语义没动，所以那些"某人一直发不出去""某人的面板打不开"的因果链仍然是完整的。
