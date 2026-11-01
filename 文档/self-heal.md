# 自愈机制（无人值守）

程序出错时自动恢复，全程无需人工。两层配合：

1. **运行时自愈（进程内，无 AI）**：浏览器崩溃自动重启会话重试、渲染挂死重开会话、页面加载失败 reload（`spark.py` 内部）。
2. **本地恢复策略表（`src/self_heal.py` + `src/recovery.py`）**：把错误**分类**成故障类别，查表得到确定性恢复动作；没有本地手段时**告警**。

## 2026-10-05 降级（重要背景）

此前第 2 层是「AI 改源码」：通过 pi 驱动 GLM/AGNES 在运行时修改 `src/` 代码。板子审计结论是它**从未成功过一次**（git 里只有 5 条 `heal: 修复前基线` + 1 条 `heal: 回滚(验证未通过)`，没有一条"修复完成"；`.heal_cache` 三个签名全 `failed`）。

根因不是引擎不好，而是**工具与问题错配**：真实故障形态（选择器失效 / 渲染挂死 / 限流 / OOM / 登录过期）**没有一个是源码 bug**；而每次触发最多要烧 20 分钟预算，还会把发送窗口挤爆（2026-10-04 事故：一晚烧穿 `timeout` → 5 个好友漏发）。

同类无人值守项目（Playwright 自愈库 / 运维 watchdog）的共识做法是「错误分类 → 确定性恢复动作 → 告警」，故照此降级。**现在零 API 调用、毫秒级返回、完全可预测。**

## 分类 → 策略（`src/recovery.py`，纯函数）

| 故障类别 | 命中特征（示例） | 恢复动作 | 门面返回 |
|---|---|---|---|
| `render_hang` | 渲染挂死 / body 全空 / blank | 重开会话后重试 | True |
| `login` | 未登录 / 登录态 / logged_out | 跳过 + **告警** | False |
| `resource` | out of memory / 内存不足 | 重开会话后重试 | True |
| `transient` | timeout / target closed / econnreset / **发送未确认** | 原样重试 | True |
| `selector` | 未命中 / 选择器 / 失效 / 覆盖缺失 / 改版 | reload 后重试 | True |
| `unknown` | 认不出的一律归此 | 跳过 + **告警** | False |

判定顺序即优先级（`render_hang` → `login` → `resource` → `transient` → `selector` → `unknown`），越"具体/危险"的越先判，避免 OOM 日志里恰好出现"未找到"就被误判为选择器。策略表就是"运维策略"——调它不需要碰业务代码。

## 对外接口（未变）

```python
self_heal(error_type, error_msg, extra="", screenshot_path="") -> bool
# True  = 建议调用方按策略重试；False = 跳过，别浪费发送窗口
```

调用方自己执行动作（只有它手里有页面与会话句柄）：

- `src/discovery.py`：发现阶段选择器失效 → `self_heal` 返回 True → reload 重试一次，仍失败冷开页面再试，再失败抛 `HealRetryFailed`（rc=3，交 `run_cron.sh` 冷启动重跑）。
- `src/scheduler.py` `_send_phase`：瞬时故障先重启浏览器重试一次；静默失败（`发送未确认`）走 `_heal_after_send_failure` 判定；渲染挂死抛 `RenderHangDetected` → 重开会话。
- `src/scheduler.py` `_coverage_gate`：发现覆盖缺失 → 分类为 `selector` → 重试成功则 rc=3 冷启动补发（`coverage_heal_on_cooldown` 有 24h 冷却）。
- `src/streak_history.py`：历史漏发（`streak_gap`）→ 未知类 → 告警。

## skip-and-continue（2026-10-05 一并做的）

`_send_phase` 现在按本地策略给失败好友分流：

- `should_retry` 为真（transient / selector / render_hang / resource）→ 进入**补扫**阶段换新会话重试；
- 为假（unknown / login）→ 记入 `skipped_names`，**不再补扫**，日志 `[skip]` 一行，继续下一个好友。

一个救不回的好友不该继续吃掉后面好友的时间（抄自同类项目的 `continue_on_failure`）。跳过的名单会并入当日 `save_daily_record` 的 `failed`。

## 告警（无人值守的安全网）

`login` / `unknown` 两类本地无手段，通过 `self_heal._alert` **复用 `health_check.notify_cmd` 通道**确定性告警。同一错误签名**每自然日最多告警一次**（账本 `runtime/.heal_alerted`），防告警疲劳；未配置 `notify_cmd` 或执行失败都只记日志，不影响主流程。

无人值守最坏的结果不是没修好，而是**没人知道没修好**——这是付费兜底退出后必须补上的一环。

## 开关

优先级：env `SPARK_SELF_HEAL=1/0` > `config.json` 的 `self_heal` > 默认**开**。关闭时只把错误写进日志、等人工处理；第 1 层运行时自愈与 `run_cron.sh` 重跑不受影响。

## 保底重跑（未变）

自愈后进程内 reload/冷开重试仍失败时，`spark.py` 返回退出码 3，`run_cron.sh` 冷却 180s 后**冷启动重跑**（最多 3 次）——新进程选择器若已人工修好，直接续上火花。整晚有硬截止 `_HARD_STOP_MIN=95`（01:35），超时 90m；详见 `docs/deployment.md`。

## 已知局限（无人值守下接受）

- `selector` 类（抖音改版）本地**修不好**，只能靠 reload 重试扛过瞬时抖动 + 告警等人工更新 `src/spark_status.py` 的选择器。这是刻意的：改选择器需要人判断新版页面结构，AI 猜测的风险高于收益。
- 多账号下 `--verify-send` 只覆盖 `accounts[0]`（现生产单账号，启用多账号前需处理）。

## 设计文档

历史设计（pi 修复架构、验证门禁三态、预算账本）见 `docs/decisions/2026-08-09-self-heal-pi-design.md` 与 `WORKLOG.md`；`.pi/maintainer.md` 为历史 pi 提示词约束（已不参与自动链）。
