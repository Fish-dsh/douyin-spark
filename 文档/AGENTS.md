# AGENTS.md · douyin-spark

抖音自动续火花：每天 00:00（cron 调 `run_cron.sh`）用 playwright + chromium 给「有火花且需续」好友发"续火花"。**表情面板关闭=发送成功**（`src/sender.py` 的 `send_sticker` 判定，勿改）。**置顶好友不受状态检测影响、一律强制续**（防误判断火花）。

生产跑在 OrangePi Zero 3（1.5G 内存，占用须 ≤1G）；本机 WSL 是调试环境。**本仓库是 git repo**（2026-08-15 起），破坏性操作仍先备份。

## 关键文档

| 文件 | 用途 |
|---|---|
| `docs/operations.md` | config.json、登录管理、FAQ |
| `docs/deployment.md` | 板子部署、cron、内存优化、板子操作约定 |
| `docs/self-heal.md` | 自愈机制（运行时自愈 + 本地恢复策略表 + 告警 + 保底重跑） |
| `WORKLOG.md` | 会话时间线、事故复盘（跨会话先读） |
| `.pi/maintainer.md` | pi 修复 agent 硬性约束（含 `XX` 后缀铁律） |

## 命令（一律用 `.venv/bin/python`）

```bash
# 测试/校验
.venv/bin/python -m unittest discover -s tests -v   # 全量单测（数量以末行 `Ran N tests` 为准，别信文档里写死的数字）
ruff check .                                        # lint（配置见 ruff.toml，豁免项有注释）
.venv/bin/python -m py_compile <file>               # 编译校验
.venv/bin/python tools/wlog.py check                # 记档对账（按天）：有状态类改动未记 WORKLOG 则退出 1
.venv/bin/python tools/wlog.py check --strict --after 008676d  # 逐 commit 对账，只认守卫装好之后
.venv/bin/python tools/wlog.py new --date YYYY-MM-DD --subject "会话 Nb（补记：…）" --text-file /tmp/n.txt  # 整天漏档补记
.venv/bin/python src/health_check.py --quiet       # 健康检查：当日日志+state 校验，失败退出 1

# 运行
.venv/bin/python spark.py --dry-run   # 演练：定位链路、不真正发送
.venv/bin/python spark.py --force     # 强制：忽略发送窗口+今日幂等
.venv/bin/python spark.py --no-jitter # 跳过 ±5min 抖动
.venv/bin/python spark.py --verify-send  # 发送链路门禁：窗口内对全部待续好友逐一真实发送并确认（人工/多账号前验证用）
.venv/bin/python login.py --password  # 用 .env 账号密码登录(自动重登入口, 可带 --storage)
```

> 运行 `spark.py` 会真实启动浏览器访问抖音。所有运行时生成物在 `runtime/`。**时间口径=机器本地时**：窗口/state 日期/健康检查均按本地时判定（板子时区 Asia/Shanghai，cron 00:00 本地命中窗口 23:30-00:30；曾误用 UTC 已改回，勿再引入）。

## spark.py 退出码（勿改语义）

| 码 | 含义 |
|---|---|
| 0 | 成功 / 无待续好友 / 窗口外 |
| 1 | 一般异常 |
| 2 | 未登录（提示走 `login.py --password`） |
| 3 | 自愈成功但本进程重试仍失败 → `run_cron.sh` 冷却 180s 冷启动重跑 ≤3 次 |
| 4 | 未获得 PID 互斥锁（另一实例持锁）→ 本次跳过；**不得当成功**（2026-10-01 新增：原先裸 return 得 rc=0，锁被挂死实例长期持有时 cron 天天"成功"、静默断火）。`run_cron.sh` 对 4 不重试；子进程被 `timeout` 杀掉是 124（2026-10-04 前是 45m——两账号串行实测需 ~60 分钟，45 分钟在**无故障的正常夜里**都不够，已放宽并据此把健康检查 cron 推迟到 01:45）。**整晚有硬截止 `_HARD_STOP_MIN=95`（01:35）**：距它不足 20 分钟就不再起新一轮，每轮 timeout 按剩余时间收窄——00:00 起的第一轮仍是完整 90m，被收窄的只是"已经烧过一轮"的晚上。没有这条的话，rc=3 的重跑会跑到 01:53，而 health_check 有一条"主任务还在跑就不判定、直接退 0"的豁免 → **那一晚谁都没判过**（2026-10-04 评审 P1） |

## 核心模块

- `spark.py`：精简入口，仅转调 `src/scheduler.run()`（`src/scheduler.py` 才是主流程）。两阶段（发现→发送）、窗口校验（`send_window` 23:30–00:30 跨天，只约束开始时刻）、±5min jitter、逐好友发送（默认复用同一浏览器实例省冷启动：`browser_reuse` 默认 true，每处理完一个好友按 `browser_recycle_mem_mb`(默认 300MB) 查 `MemAvailable`，低于阈值才回收实例、下个好友冷启动重建；故障时丢弃重建；设 `browser_reuse:false` 回到每好友独立实例）；**失败好友全部结束后换新会话补扫一轮**；`--verify-send` 对全部待续好友逐一真实发送，全过才门禁通过。`_HEALABLE_ERRORS` = 瞬时故障，先免费重启浏览器重试；**发送阶段渲染挂死（`wait_chat_list` 超时且 `page_render_state`=blank）抛 `RenderHangDetected` → 重开会话重试一次**（与发现阶段 `_discover_with_hang_retry` 同口径；2026-10-01 事故：好友在 150s 空页上硬撑，必然「未找到表情按钮」→静默失败）；**发送静默失败也走本地恢复判定**（`_heal_after_send_failure`）——`confirm_send` 返回 fail 而非抛异常，落在 except 白名单之外的盲区；判定为「救不回」的好友进 `skipped_names` 不补扫、继续下一个，判定可重试的才补扫。
- `src/spark_status.py`：`discover_friends_with_spark` 三策略降级（CSS class / 文字特征 / 图标扫描）。全部未命中抛 `SparkDiscoveryError` → 触发自愈。**抖音改版需改这里的选择器**。（2026-10-01 从根迁入 `src/`）
- `src/` 分包：`discovery.py`=发现编排（调 spark_status，置顶 `isPinned` 强制续、`active` 跳过、`need` 待发）；`sender.py`=点击/发送/确认（`click_friend` 三策略 + reload 重试）；`session.py`=会话生命周期+登录保障+内存控制（未登录 `sys.exit(2)`）；`config.py`=配置/state/窗口判定/.env；`finder.py`=选择器工具；`debug.py`=截图 dump（`runtime/heal_screenshots/`）；`login.py`=登录逻辑（根 `login.py` 仅 CLI 壳）；`self_heal.py`=本地恢复策略门面（分类 + 告警，2026-10-05 从 AI 引擎降级，从根迁入 2026-10-01）；`recovery.py`=纯函数分类/策略表（2026-10-05 新增）；`spark_status.py`=火花状态发现（从根迁入，2026-10-01）；`health_check.py`=健康检查（从根迁入，2026-10-01）。
- `login.py`：**根文件为 CLI 薄壳**（`python login.py --password/--manual`，re-export 兼容 `import login`）；**登录逻辑实现在 `src/login.py`**（登录态存取 `runtime/storage.json`、`auto_login` 密码重登、失败退避写 `runtime/login_state.json`）。2026-09-30 下沉以消除「根 `login` ↔ `src.session`」双向交叉依赖，依赖方向改为「入口(根) → 包(src) → 根叶模块(logutil/self_heal/spark_status)」。
- `src/self_heal.py` + `src/recovery.py`（**2026-10-05 降级**）：自愈**不再是 AI 改源码**，改为「错误分类 → 本地恢复策略表 → 告警」。理由：真实故障形态（选择器失效 / 渲染挂死 / 限流 / OOM / 登录过期）**没有一个是源码 bug**，旧 AI 链被板子审计证实**从未成功过一次**（0 条"修复完成"提交、`.heal_cache` 全 `failed`），却每次最多烧 20 分钟预算（2026-10-04 事故：烧穿 timeout → 5 人漏发）。`recovery.classify(error_type, error_msg)` → 类别 ∈ `{render_hang, login, resource, transient, selector, unknown}`；`recovery.policy()` → 动作 ∈ `{重开会话, 告警, 重开会话, 重试, reload, 告警}`；判定顺序即优先级，越"具体/危险"越先判。全部是**纯函数、零依赖**（无 playwright / 网络 / 文件系统）。门面 `self_heal(type, msg, extra, screenshot_path) -> bool`（**对外签名未变**，True=建议调用方重试）只做分类 + 告警，**零 API 调用、毫秒返回**；具体动作由调用方执行（只有它手里有页面与会话句柄）。`login`/`unknown` 两类本地无手段，经 `_alert` 复用 `health_check.notify_cmd` **确定性告警**，同签名每自然日至多一次（账本 `runtime/.heal_alerted`，防告警疲劳）。**开关**：env `SPARK_SELF_HEAL=1/0` > `config.json` 的 `self_heal` > 默认**开**。**skip-and-continue**：`_send_phase` 按 `should_retry` 给失败好友分流——判定"救不回"（unknown/login）的进 `skipped_names`、**不补扫**、继续下一个，并入当日 `failed` 记录。**`selector` 类（抖音改版）本地修不好**，靠 reload 重试扛瞬时抖动 + 告警等**人工**更新 `src/spark_status.py` 选择器（AI 猜新版结构风险高于收益，是刻意取舍）。旧 pi/opencode/GLM/AGNES 引擎、预算账本、备份回滚、验证门禁代码已删；历史设计见 `docs/decisions/2026-08-09-self-heal-pi-design.md` 与 `docs/self-heal.md`。
- `src/health_check.py`（2026-10-01 从根迁入 `src/`）：健康检查（state 日期=今天 + spark.log 当日**逐账号**收尾）；失败执行 `config.json` 的 `notify_cmd`（摘要走环境变量 `HEALTH_FAIL_MSG`）、退出码 1。**刻意不写 spark.log**（防自证循环）。**逐账号口径（2026-10-04 事故）**：老口径只统计"出现过 `完成:` 行"的账号，账号 A 正常收尾就足以让整轮判健康 → 账号 B 被 timeout 杀在半路（5 人漏发）仍无告警；现按 `config.accounts` 逐个点名，缺收尾行／被跳过（登录态失效）都判失败。**运行中不判定**：`runtime/.spark.pid` 指向活的 `spark.py` 进程时直接退出 0 且**不改** `.health_failed`（判定过早=假告警=告警疲劳；陈旧锁/PID 复用不算运行中）。**cmdline 判据必须写 `spark.py` 而不是裸 `spark`**：项目路径本身就是 `/opt/douyin-spark`，一个残留的自愈子代理或任何带该路径的进程都会命中裸词 → "还在跑"→ 退 0 → 整晚没人判定，后果与静默断火相同（2026-10-04 评审）。**这条豁免的前提是 `run_cron.sh` 的硬截止**（见上面的 `_HARD_STOP_MIN`），否则"还在跑"会变成一条长期免判定通道。
- `logutil.py`：`log()` 统一日志（控制台 + `runtime/spark.log` 持久句柄），勿各模块另写。**src 包一律 `from logutil import log` 绝对导入根实现**（9/27 曾出现 `src/logutil.py` 整份副本、后又收敛为转发垫片 → 日志分裂到 `src/runtime/`、health_check 成功夜恒误报；2026-09-30 统一导入风格后 `src/logutil.py`、`src/self_heal.py` 两块垫片**已删**，勿再放副本/垫片）。**单测入口日志隔离（2026-10-04 事故）**：`python -m unittest` / `python tests/test_x.py` 下 `LOG_FILE` 自动改写 `runtime/spark.test.log`（`SPARK_LOG_FILE` 可显式覆盖）——单测跑的是真实调度代码，写进生产 `spark.log` 就会注入伪造的 `==== 账号 X 完成:` 行顶掉真实告警（板子上实测一晚 42 条）。子进程（`python -c` 等探测不到的入口）须显式传 `SPARK_LOG_FILE`。
- `run_cron.sh`：cron 入口。**stdout 重定向到 `> /dev/null`**（logutil 已写 runtime/spark.log，勿双写）。

## 必知约定与坑

- **多账户**：`config.json` 的 `accounts` 字段为账号列表，每个账号含 `name`/`account`/`password`/`storage_file`/`state_file`。`storage_file` 和 `state_file` 可自定义，否则按 `{name}` 派生。账号凭据从 config.json 的 account dict 读取；**`auto_login` 不回退 `.env`**（多账号下回退即跨账号误登，2026-09-24 事故，2026-10 移除；仅人工 `--password` 登录命令在无参时才读 `.env`）。`auto_login` 将登录态保存到对应 `storage_file`。**调度放行判据 `has_login_state`**：配置了凭据或 storage 含未过期 douyin.com cookie 之一即处理，两者皆无则跳过该账号。**新账号需先获取有效登录态**（`python login.py --password --storage runtime/storage_{name}.json`）。
- **配置**：`config.json` 的 `friends` 字段已废弃，好友固定自动发现（`engine` 固定 `chromium`）；不写死好友昵称/手机号等隐私，凭据只放 `.env`（须 `chmod 600`）。
- **`.pi/maintainer.md` 铁律**：含字面量 `XX` 后缀的选择器（如 `conversationConversationItemwrapperXX`）一律无条件删除 `XX` 恢复原类名，是唯一正确的最小修复，禁止借机新增策略/JS/兜底逻辑。pi 只 py_compile 自证，权威验证由外层跑。
- **状态文件**（`runtime/`，gitignored，勿改勿复述）：`storage.json`=登录态、`state.json`=幂等（date+sent 昵称）、`state.json` 日期不匹配则重置当天空列表；`login_state.json`=自动重登退避；`.health_failed`=健康检查失败标记；`.heal_alerted`=本地恢复告警去重账本（只留今日条目，同错误签名每自然日至多告警一次，防告警疲劳）。
- **时间口径**：全部按**机器本地时**判定（板子 Asia/Shanghai，cron 00:00 = 北京 00:00 命中窗口 23:30-00:30）；2026-08-15 曾误用 UTC（cron 北京 00:00=UTC 16:00 永远不在 UTC 窗口→静默不发送）已改回本地时，勿再引入时区；改时机配置过「四方一致」。
- **cron 陷阱**：`run_cron.sh` 的 shell 逻辑承载了 cron 对 `%` 的解析规避；部署后必须 `chmod +x` 且属主 `BOARD:BOARD`（scp/覆盖会丢 x 位→cron 静默 Permission denied，无日志）。
- **启动时机**：`send_window` 必须包住 cron 00:00 时刻；改运行时机配置要过「四方一致」（配置×代码×cron×文档）。
- **板子操作**：`ssh BOARD "<命令>"` + `scp`——本机（Ubuntu WSL）`~/.ssh/config` 已配免密直连（Host BOARD，root@192.0.2.10，ControlPersist 复用）。需以 BOARD 身份写文件用 `runuser` 或事后 `chown`（root 无需 sudo）。板子 Python 3.11 vs 本机 3.14，版本相关改动需板子真机复验。**同步（本机 = 唯一正源，板子单向镜像；2026-10-06 更正流程）**：本机 commit 后 `git bundle create /tmp/spark-mirror.bundle main && scp` 到板子 → 板子 `git -C /opt/douyin-spark fetch -q /tmp/spark-mirror.bundle "refs/heads/main:refs/remotes/localsrc/main" && git checkout refs/remotes/localsrc/main -- . && python3 -m py_compile src/*.py && git add -A && git commit -m "chore(board): 部署快照 …(与本机 <sha> md5 一致)"` → `chown -R BOARD:BOARD /opt/douyin-spark`（cron 以 BOARD 跑，root 属主会挡住自愈改文件）。**注意 `merge --ff-only` 已不可用**：板子有自己的部署快照提交链，与 main 必然分叉（会话 37 起惯例）；同步语义=「工作树对齐正源+板端提交快照」, 校验靠逐文件 md5, 部署完把「进行到一半」的状态写进 WORKLOG 挂起事项（曾有半部署事故: 文件拷了一半收工, 下个会话只能挨个 md5 考古）。小改单文件也可直接 `scp <file> BOARD:/opt/douyin-spark/` 后同样 chown。板子离线时（ssh 超时）等板子上线后再同步。
- **记档纪律（会话 32 立，机制而非自觉）**：动了**状态类文件**（`src/*.py`、根级 `.py`、`requirements.txt`、`config*.json`、`run_cron.sh`、`logrotate.conf`、`ruff.toml`）就必须在**同一轮**记 WORKLOG——`git commit` 后立刻 `.venv/bin/python tools/wlog.py add --session N --text "…"`。该工具做锚点唯一性校验 + 原子写，**禁止手搓 `text.replace()` 追加**（曾把相邻历史条目的尾句整段覆盖）。整天漏档（`check` 报某天零条目）用 `tools/wlog.py new --date 该天 --subject "会话 Nb（补记：…）" --text-file …` 补一条、自动落到该日位置，条目里写清「按 diff 补记」并把当天 sha 列进去——补记走工具，正源永不手改。收尾前 `tools/wlog.py check` 须退出 0（1=有欠账，2=被拒/未配置）；`--strict` 逐 commit 判，须配 `--after <守卫装好那笔提交>` 用（基线之前的旧账不是它的 claim），且同提交自带 WORKLOG 改动不算欠账（理想姿势）。动机查不真就 `tools/wlog.py stub` 留一行待补欠债，**不许编动机**。归因前先查 `%an/%cn` 与会话记录，别把改动推到别的 harness 头上。跨项目版本已做成 user-scoped skill `ai-project-memory-audit`（自带通用 `scripts/wlog.py`）——换项目用它审计+加固；**不用 git 钩子**（`.git/hooks` 不进仓库、跟着人走不了，且钩子会挡住无人值守的自愈提交）。
- **挂起事项（跨 Harness 存档点，2026-10-06 立）**：WORKLOG 顶部「## 挂起事项」小节只放进行中/待观察的一两行短句（`- [ ] YYYY-MM-DD · 事项 → 判据/动作 (owner)`）。**开工先读**（避免重复考古别人做了一半的事），**收工前更新**（新挂起加一行、完成删行并把成果沉淀进时间线）。时间线只追加不改写，挂起事项允许增删——这是两者分工：存档点 vs 历史。

## 维护环境（pi）

项目现由 **pi** 单环境维护；WORKLOG 历史条目里的 deepseek harness / ssh-mcp 均已弃用（见 WORKLOG「已弃用」）。

| 操作 | 做法 |
|---|---|
| 板子执行命令 / 传文件 | `ssh BOARD "<命令>"`、`scp`（`~/.ssh/config` 免密直连；同步方式见上「板子操作」） |
| 代码审查（完成前五问④） | 派 subagent 审查代理（给完整自包含 prompt），如会话 11b |
| 测试 / lint / 运行 | `.venv/bin/python ...`、`.venv/bin/ruff check .`（ruff 版本见 `requirements.txt`，已锁） |
| 日志记录 | **同轮**追加时间线条目：`.venv/bin/python tools/wlog.py add --session N --text "…"`（整天漏档 `new --date/--subject`，对账 `check`，补欠债 `stub`）；勿手搓字符串替换 |
| 自愈 | `src/self_heal.py` + `src/recovery.py` 本地策略表：错误分类 → 确定性恢复动作 → 告警（**零 API 调用、不改源码**）。`SPARK_SELF_HEAL`/`self_heal` 可覆盖开关。自动链已不再调用 pi/LLM（2026-10-05 降级） |

原则：**约定单一定义（板子免密直连、审查过门禁、时间口径北京时等），落地按环境选择工具**；WORKLOG 历史条目记录的是当时环境，操作提示以本表为准。

## 破坏性操作前必备份

```bash
cp -r ~/projects/douyin-spark /tmp/spark-config-backup-$(date +%s)
ls -dt /tmp/spark-config-backup-* | tail -n +6 | xargs rm -rf
```

## 完成前五问（声称完成/请求部署前必过）

1. **配置/时机**：四方一致（配置×代码×cron×文档）？时机命中？
2. **逆场景**：什么场景会绕过验证或门禁（如网络 TimeoutError 不进重试链 / 验证子进程触发同签名递归自愈 / 先重改后重试顺序错）？
3. **文档**：需同步？是否与实现矛盾？
4. **审查门槛**：>30 行 / 配置联动 / 关键路径 → 派 subagent 审查代理（给完整自包含 prompt），重点查「配置-代码联动」「逆场景绕过」盲区。
5. **记档**：本轮动过状态类文件吗？已在同一轮落 WORKLOG 动机+结论？`tools/wlog.py check` 退出 0？（漏记的根因永远是「留到以后再写」）

## 本机（WSL）不做生产监控

板子才是生产：本机无 cron，`runtime/state.json` 日期不会自动跟当天 → 在本机手动跑 `src/health_check.py` 会判 FAIL 并**真实执行 notify_cmd 推送告警**。健康检查只在板子跑；本机调试用 `--dry-run`/单测，勿在本机跑 `src/health_check.py`。

**小改动（≤30 行、行为零变化）可逐项声明「不涉及①/②/③/④」替代；但⑤记档不适用此豁免——动了状态类文件就必须同轮记档。涉及五项任一必须显式给结论。**

## 本机开发 / 板端生产分离（2026-11-01）

生产只有一条入口：cron → `run_cron.sh` → `config.json`。本机开发/调试走 `./run_dev.sh`，它导出两个**只改指针**的环境变量后再跑 `spark.py`（不设这两个变量时，CONFIG_FILE/RUNTIME_DIR/STATE_FILE/LOG_FILE 与旧版逐字节相同；浏览器参数与全流程等待/超时/预算另有 low/mid/high 三档，见下）：

| 变量 | 生产（不设） | 本机开发（`run_dev.sh` 设） |
|---|---|---|
| `SPARK_CONFIG` | `config.json` | `config.dev.json`（`extends: config.json` + `perf_profile: high` + `headless: false` + `browser_reuse: true` + `display: null`） |
| `SPARK_RUNTIME_DIR` | `runtime/` | `runtime-dev/`（state/storage/日志/截图全隔离，不污染生产监控面） |


- `perf_profile`：**三档**，缺省 `low`，生产配置不用改。档位同时决定**浏览器资源上限**（`src/session.py`）和**全流程等待/超时/轮询/预算**（`src/perf.py`），两处共用同一个字段，不会各说各话。

  | 档位 | 目标机器 | 浏览器资源 | 单好友固定等待（`perf.friend_sleep_ms`） |
  |---|---|---|---|
  | `low` | 板子 4 核 / 1.5G | 单渲染进程、256MB JS 堆、禁 GPU | 3.35s（= 原生产字面量，零行为变化） |
  | `mid` | 普通笔记本 4-8 核 / 8G，无独显 | 2 渲染进程、1GB JS 堆、GPU 开关沿用板端口径 | 2.13s（low 的 64%） |
  | `high` | 桌面 18 核 / 15.7G | 放开 GPU、4 渲染进程、2GB JS 堆 | 1.31s（low 的 39%） |

  「节奏」只算**无条件真睡**（`_SLEEP_KEYS`），不含 `wait_for_selector` / `click(timeout=)` 这类「元素一出现就返回」的超时天花板。一晚 9 好友×2 账号纯固定等待：low 145s / mid 97s / high 65s。实测同一条 sleep 序列 3 好友×1 账号墙钟：48.4s / 35.6s / 24.8s。

  **三档统一、不随档位缩的两项**（慢在抖音侧、代价不可逆，收窄只会更快失败）：`list_budget_ms=150000`（板端实测列表最慢 75s 才填充，短于它必然扫半张列表→静默漏发；`_BOUNDS` 下限 120000）、`login_state_deadline_ms=45000`（判错未登录会写 `last_fail` → 1 小时退避 → 当晚 0 发送）。`pwd_field_budget_ms` 三档同为 15000。

  51 个 knob 全部集中在 `src/perf.py`（`_SPECS` 三档表 + `_BOUNDS` 区间校验）。`low` 列**逐项锁死**为 2026-11-01 前的生产字面量，并由 `tests/test_perf.py::TestLowLocksProductionLiterals` 守护——改 `low` 任何一个数字都会让测试红。
  `python src/perf.py` 打印三档完整对照表；`python src/perf.py high` 打印该档摘要。启动日志里有 `[perf] perf_profile=...`，排查时先看这一行。
  临时覆盖：`SPARK_PERF_PROFILE`，或 `SPARK_PERF_<KNOB大写>`（如 `SPARK_PERF_PANEL_OPEN_MS=9000`），越界/非法值回落默认，不抛异常。
- 三档都带**防后台节流三连**（注意：这是相对 10 月版的**唯一**参数变化，low 档板端同样生效，上板前应视为「行为有变」而非零改动；`--disable-renderer-backgrounding` 理论上会抬高 RSS，板端 1.5G 需实测）：`--disable-background-timer-throttling` / `--disable-renderer-backgrounding` / `--disable-backgrounding-occluded-windows`。板端 headed 跑在 Xvfb 上，窗口极易被判「被遮挡」，chromium 随即降频 —— 与「表情面板打开慢 → 按旧布局取坐标 → 点到空白处」的现象吻合。
- `extends` 只支持一层，只为「开发配置继承生产配置」而存在，不要扩散成配置框架。
- `runtime-dev/` 下没有 `storage*.json` → 首次跑需扫码登录一次；`state*.json` 也独立，不与生产幂等记录互相污染。
- 本机 18 核 / 板子 4 核 1.5G：**不要**用板子的低配参数或耗时标准衡量本机（2026-11-01 实测：同页面 60s 级等待主要来自抖音侧慢路径，不是 CPU 不够）。纯 CPU 基准（300 万次整数乘模）：WSL 0.31s vs 板子 3.26s，**板子单核慢约 10.5 倍**——这就是三档存在的理由。

## 常见故障速查

| 现象 | 排查方向 |
|---|---|
| 找不到好友 | `click_friend`（src/sender.py）三策略 + reload 重试仍失败 → 入 failure 列表，末尾补扫仍败则跳过 |
| 火花状态误判 | `spark_status.py` 三策略选择器失效 → 触发自愈或手动改选择器 |
| 滑块验证 | 密码自动登录触发 → 记录 `last_fail` 退避 1 小时，次日再试 |
| 内存不足 | 检查 swap/zram；确认 chromium headless + 禁 GPU；缩小 viewport |
| 登录失效当天 | `auto_login` 失败记录 `login_state.json`，当日不再重试 |
| cron 无日志 | `test -x run_cron.sh` 校验可执行；`chown -R BOARD:BOARD` |
| 板子断网 | YT8531 PHY 缺陷 → 固定 100M (`nmcli 802-3-ethernet.speed 100`)；无线强制 5G |

## 环境差异

| 项 | 本机 (WSL) | 板子 |
|---|---|---|
| Python | 3.14 | 3.11 |
| 内存 | 充足 | 1.5G（≤1G 占用） |
| 操作方式 | 直接运行 | ssh 插件/`ssh BOARD` 免密 / scp 回退 |
| 真机验证 | .venv/bin/python | 需板子真机复验版本相关改动 |
| chromium 档位 + 等待节奏 | `perf_profile: high`（`./run_dev.sh`，18 核 / 15.7G） | `perf_profile: low`（缺省，4 核 / 1.5G） |