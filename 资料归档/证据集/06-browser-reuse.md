# 浏览器复用 (`browser_reuse`)

## 结论

**性能优化，非功能必需。** 在 1.5G 内存板子上省冷启动时间。有内存回收安全阀。**文档矛盾已于 2026-10-01 修复**（统一为代码默认 true）。

---

## 1. 两种模式

| 模式 | 行为 | 优点 | 缺点 |
|------|------|------|------|
| `browser_reuse: false` | 每好友独立 Chromium 实例 | 安全，无内存累积 | 冷启动慢（ARM 板子数秒） |
| `browser_reuse: true` | 共享单浏览器，每好友新建 context | 省冷启动时间 | 内存单调增长 |

## 2. 配置（已统一，默认 `true`）

| 来源 | 值 | 位置 |
|------|-----|------|
| `src/config.py:30` | `True` | 代码默认值 |
| `config.example.json:6` | `true` | 示例配置（已修正） |
| `AGENTS.md:47` | `true` | 架构文档 |
| `docs/operations.md:10` | `true` | 运维文档（已修正） |
| `docs/deployment.md:66` | `true` | 部署文档（已修正） |

## 3. 内存安全阀

```
scheduler.py: _send_phase()
  → 每好友处理后: _recycle_if_low_memory()
    → 读 /proc/meminfo 的 MemAvailable
    → < 300MB（默认阈值）→ 关闭共享浏览器 → 下一好友冷启动
```

| 文件 | 行号 | 说明 |
|------|------|------|
| `src/scheduler.py:81-100` | `_recycle_if_low_memory()` | 内存检查 + 回收 |
| `src/scheduler.py:433-436` | 调用点 | 每好友处理后 |
| `src/config.py:31` | `DEFAULT_BROWSER_RECYCLE_MB = 300` | 阈值 |

## 4. 故障处理

关键故障（TimeoutError, OSError, ConnectionError, BrokenPipeError, "Target closed"）时：

```python
# scheduler.py:75-78 _discard_shared_browser()
close_session(None, fctx)  # 先关 context
# 再关 browser → shared_browser = None → 下一好友冷启动
```

**W1 不变式**（test_scheduler.py:383）：故障后的浏览器**必须丢弃**，绝不复用。

## 5. 会话生命周期

| 文件 | 行号 | 说明 |
|------|------|------|
| `src/session.py:144-169` | `open_session()` | 接受可选 `browser` 参数 |
| `src/session.py:172-183` | `close_session()` | browser=None 时只关 context |
| `src/session.py:15-31` | `LAUNCH_ARGS` | JS 堆限制 256MB + 禁图片/字体 |
| `src/session.py:110-119` | `free_page_memory()` | 每好友后触发 JS GC |

## 6. 测试覆盖

| 测试文件 | 类/方法 | 覆盖 |
|----------|---------|------|
| `tests/test_scheduler.py:226` | `test_reuses_shared_browser` | 复用模式：首好友冷启动，后续复用，context 单独关 |
| `tests/test_scheduler.py:273` | `test_recycles_shared_browser_when_memory_low` | 内存低于阈值时回收 |
| `tests/test_scheduler.py:310` | `test_render_hang_retries_without_ai_heal` | 渲染挂死 → 重开会话 |
| `tests/test_scheduler.py:350` | `test_critical_fail_discards_shared_browser` | W1 不变式：故障后丢弃 |
| `tests/test_scheduler.py:617` | `test_default_mode_launches_fresh` | 不复用模式：每好友独立 |

**5 项测试**，覆盖充分。

## 7. WORKLOG 历史

| 日期 | 事件 |
|------|------|
| 2026-09-30 | 拦截 image/font 资源加载降 RSS（commit bf87bc3） |
| 2026-09-30 | 9/30 事故：板子仅 231MB 可用，Chromium 峰值 1.2GB |
| 2026-10-01 | 引入内存回收机制（commit cb43a9a） |

## 8. 删除后果

**如果删除 browser_reuse，强制每好友独立浏览器：**
- 正面：消除内存累积风险，代码简化（~50 行）
- 负面：每好友冷启动增加数秒延迟（ARM 板子）
- 功能无影响：正确性不变，只是变慢

**如果保留但默认改 false：**
- 需统一文档（AGENTS.md / operations.md / config.example.json）
- 需确认板子 config.json 的实际值

## 9. 判断

**保留，文档已统一。** 理由：
1. 在 1.5G ARM 板子上，冷启动延迟是真实的性能问题
2. 内存回收安全阀已到位（300MB 阈值）
3. 代码量不大（~50 行核心逻辑 + 5 项测试）
4. **文档矛盾已修复**（2026-10-01）：config.example / operations.md / deployment.md 统一为 `true`
