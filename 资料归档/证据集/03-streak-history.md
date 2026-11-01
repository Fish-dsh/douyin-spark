# `streak_history.py` 火花历史

## 结论

**生产中调用，是静默发现失败的唯一安全网。** 但曾有 bug 导致自愈从未真正触发（会话 29 修复）。覆盖门禁（P5）是较新的、更活跃的路径。

---

## 1. 两个生产角色

### 角色 A: P5 覆盖门禁（发现前 → 发送前）

```
scheduler.py: process_account() → _coverage_gate()
  → streak_history.check_recent_coverage(account, today_seen)
    → 近 3 天已成功续好友 是否出现在今日发现名单？
    → 缺失 → 触发 self_heal("discover", ...) → 成功则冷重启（rc=3）
```

| 文件 | 行号 | 说明 |
|------|------|------|
| `src/streak_history.py:84` | `check_recent_coverage()` | 计算缺失好友 |
| `src/streak_history.py:41` | `coverage_heal_on_cooldown()` | 24h 冷却检查 |
| `src/scheduler.py:261-295` | `_coverage_gate()` | 门禁主体 |
| `src/scheduler.py:549` | 调用点 | 发现后、发送前 |

### 角色 B: 跑后记录 + 间隙检测（发送后）

```
scheduler.py: process_account() 末尾
  → streak_history.save_daily_record(...)    # 持久化今日结果
  → streak_history.trigger_if_gap_detected(...)  # 对比昨日发送 vs 今日发现
```

| 文件 | 行号 | 说明 |
|------|------|------|
| `src/streak_history.py:124` | `save_daily_record()` | 持久化（保留 30 天） |
| `src/streak_history.py:235` | `trigger_if_gap_detected()` | 间隙检测 + 触发自愈 |
| `src/scheduler.py:572-578` | 调用点 | process_account 末尾 |

## 2. 数据存储

- `runtime/streak_history.json`（默认账号）
- `runtime/streak_history_{account}.json`（多账号）
- `runtime/.coverage_heal_{account}`（冷却标记）

## 3. WORKLOG 历史

| 会话 | 日期 | 事件 |
|------|------|------|
| 28 | 2026-09-30 | 引入 P5 覆盖门禁。背景：9/30 00:00 cron 双账号跳过，10 好友漏续 |
| 29 | 2026-09-30 | 发现 `src/self_heal.py` 路径 bug → 覆盖门禁和 streak_gap 自愈**从未真正成功过**；修复 `trigger_if_gap_detected` 的 TypeError |

**关键事实**：从引入到 bug 修复，P5 门禁和 streak_gap 触发的自愈**全部静默失败并回滚**。这意味着这段时间内的覆盖检测只做了「记录」，没有实际触发自愈。

## 4. 事故背景

2026-09-29 事故：好友「抵不过你眉眼带笑」列表文本中有 [续火花] 标记但 class 策略未检测到。这是覆盖门禁要防的核心场景——**选择器对抖音页面改版的静默退化**。

## 5. 测试覆盖

| 测试文件 | 类/方法 | 覆盖 |
|----------|---------|------|
| `tests/test_streak_history.py` (95 行) | `TestCoverage` (7 项) | 近 3 天已续、排除今日、缺失检测、冷却生命周期、按账号隔离 |
| `tests/test_scheduler.py:817-891` | `TestCoverageCheckHook` (5 项) | 门禁集成：无缺失、自愈成功返回 3、冷却仅警告、自愈失败继续 |
| `tests/test_scheduler.py` 多处 | `trigger_if_gap_detected` mock | 4 个测试类在 setUp 中 mock 防触发自愈 |

**覆盖缺口**：`check_streak_history()` 和 `get_previous_record()` 无直接单测；`save_daily_record()` 无直接单测。

## 6. 删除后果

1. **P5 覆盖门禁消失**：近 3 天已续好友从今日名单消失时，无告警、无自愈
2. **日度历史不再持久化**：`runtime/streak_history_*.json` 停止写入
3. **streak_gap 自愈触发消失**：跑后间隙检测不再运行
4. **`_coverage_gate()` 函数需删除**（scheduler.py:261-295）及其调用点（549, 572-578）
5. **12 项测试变死代码**

## 7. 判断

**保留。** 理由：
1. 解决的是「选择器静默退化」问题——抖音改版时 class 名变了，但没人注意到
2. 24h 冷却防自愈风暴
3. 代码量适中（283 行），维护成本可控
4. 曾有 bug 不等于设计有问题——bug 已修复
