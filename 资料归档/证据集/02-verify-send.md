# `--verify-send` 发送门禁

## 结论

**核心防御层，不可删除。** 自愈后唯一能验证「发送链路」完好的手段。删除会导致 AI 修复后的发送路径回归无感知——能发现好友 ≠ 能发送消息。

---

## 1. 调用链

```
self_heal.py: _verify_send()
  → subprocess: spark.py --verify-send --no-jitter
    → scheduler.py: run() → process_account() → _send_phase()
      → if verify_send: return _verify_send_phase(p2, cfg, account, state, pending)
        → 对每个 pending 好友: process_friend(dry_run=False) → 真实发送
        → 成功后写入 state["sent"]（防重发）
```

| 文件 | 行号 | 说明 |
|------|------|------|
| `src/scheduler.py:298-337` | `_verify_send_phase()` | 核心实现：逐好友真实发送 |
| `src/scheduler.py:553-555` | `process_account` 中的分支 | `if verify_send: return` 跳过正常发送 |
| `src/scheduler.py:650-651` | `run()` 中一次性退出 | 只对第一个账号执行后 sys.exit |
| `src/self_heal.py:1380-1427` | `_verify_send()` | 自愈包装器：调 subprocess + 解读结果 |
| `src/self_heal.py:1543-1556` | 自愈门禁判定 | 只在 `error_type == "process_friend"` 时触发 |

## 2. 五态返回语义

| 返回值 | 条件 | 自愈行为 |
|--------|------|----------|
| `"ok"` | exit=0 且日志含「发送成功」 | 修复通过，保留 |
| `"login"` | exit=2 且含「未登录」 | 降级：跳过门禁，信任 dry-run |
| `"inconclusive"` | 非零 exit + OOM 关键词 | 保留修复，不回滚，下次 cron 重试 |
| `"noevidence"` | exit=0 但无「发送成功」 | **回滚**（防选择器退化白过门禁） |
| `"fail"` | 其他非零 exit | **回滚** |

**关键设计**：exit=0 不够，必须有正向日志证据（`_log_evidence`）。这防止了「选择器退化导致发现 0 好友 → verify-send 无事发生 → exit=0 → 门禁误通过」的场景。

## 3. 防重发机制

verify-send 真实发送后写入 `state["sent"]`，主发送循环检查：

| 文件 | 行号 | 代码 |
|------|------|------|
| `src/scheduler.py:353-354` | 发送循环顶部 | `if fname in load_state(account).get("sent", []): skip` |
| `src/scheduler.py:418` | 重试循环内 | `自愈门禁 verify-send 已发送, 跳过重发` |

## 4. WORKLOG 历史

| 会话 | 日期 | 事件 |
|------|------|------|
| 11 | 2026-08-15 | 创建 verify-send：对第一个待发好友真实发送 |
| 11b | 2026-08-15 | 修复 verify-send 与主流程的双发残留 |
| 16 | 2026-09-06 | 升级为全量待发送 + 正向证据审查 + `noevidence` 状态 |
| 17 | 2026-09-24 | 修复多账号 `_log_evidence` 分支 |
| 28 | 2026-09-30 | 纳入 480s 总预算 |
| 29 | 2026-09-30 | 修复超时日志畸变 |
| 31 | 2026-10-01 | 提取为独立函数 `_verify_send_phase` |

## 5. 测试覆盖

| 测试文件 | 类/方法 | 覆盖 |
|----------|---------|------|
| `tests/test_scheduler.py:389-534` | `TestRunVerifySend` (4 项) | 全量发送、失败退出、部分成功、防重发 |
| `tests/test_self_heal.py:243-286` | `TestVerifySend` (5 项) | 五态返回、重试次数、OOM 判定 |
| `tests/test_self_heal.py:362-470` | `TestSelfHealVerifySendGate` (2 项) | 回滚/跳过（discover 不触发） |
| `tests/test_self_heal.py:289-329` | `TestLogEvidence` (2 项) | 正向证据正则匹配 |

**总计 13 项测试**，覆盖完整。

## 6. 删除后果

1. **发送路径回归无感知**：自愈后只能验证发现链路（dry-run），发送链路（click → 输入 → 确认）无法验证
2. **`noevidence` 安全网消失**：选择器退化导致发现 0 好友时，门禁无法识别
3. **防重发机制断裂**：自愈已发送的好友会被主流程重发
4. **三层验证退化为两层**：`py_compile`（语法）→ `dry-run`（发现）→ ~~`verify-send`（发送）~~

## 7. 判断

**保留，不可简化。** 理由：
1. 是「能发现 ≠ 能发送」这一核心洞察的工程落地
2. 正向证据要求（不只是 exit=0）是防白过门禁的关键设计
3. 测试覆盖完善（13 项）
4. 只在自愈路径触发，不影响正常运行效率
