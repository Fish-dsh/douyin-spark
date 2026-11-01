# 多发现策略 (class / text / icon)

## 结论

**三策略并行执行、结果合并。** class 为主力，text/icon 为抖音改版的兜底。**零直接测试覆盖**——JS 选择器片段从未被测试验证。

---

## 1. 三策略定义

| 策略 | JS 常量 | 行号 | 原理 |
|------|---------|------|------|
| CSS class | `_JS_DISCOVER_BY_CLASS` | 27-70 | 查 `[class*="conversationConversationItemwrapper"]`，找 `[class*="commonStreakstreakContainer"]` 判 hasStreak |
| 文字特征 | `_JS_DISCOVER_BY_TEXT` | 72-123 | 同选择器找会话项，检查图标 `src` 含 `flame/fire/streak/spark` 等关键词 |
| 图标扫描 | `_JS_DISCOVER_BY_ICON` | 125-161 | 直接查 `[class*="commonStreakicon"]`，向上遍历找父会话项 |

代码位置：`src/spark_status.py`

## 2. 执行方式

```python
# spark_status.py:221-272 _run_discover()
seen: dict[str, dict] = {}

# 策略 1: CSS class
items = page.evaluate(_JS_DISCOVER_BY_CLASS)
for item in items:
    if item.get("hasStreak") or item.get("isPinned"):
        seen[item["name"]] = item

# 策略 2: 文字特征（补充策略 1 遗漏的）
items = page.evaluate(_JS_DISCOVER_BY_TEXT)
for item in items:
    if ... and item["name"] not in seen:
        seen[item["name"]] = item

# 策略 3: 图标扫描（补充策略 1+2 遗漏的）
items = page.evaluate(_JS_DISCOVER_BY_ICON)
for item in items:
    if ... and item["name"] not in seen:
        seen[item["name"]] = item

# 三策略全失败 → SparkDiscoveryError → 触发自愈
```

**关键设计**：三策略**全部执行**（不是 stop-on-first-success），结果按 name 去重合并。每个策略独立 try/except，一个失败不影响其他。

## 3. 为什么需要三策略

抖音网页版没有稳定的公开 API，选择器依赖页面 DOM 结构。抖音前端更新时：
- class 名可能变（CSS class 策略失效）
- 但图标 URL 中的关键词（flame/fire）通常不变（text/icon 策略兜底）
- 反之亦然

**实际事故**：2026-09-29 好友「抵不过你眉眼带笑」列表文本有 [续火花] 但 class 策略未检测到（WORKLOG 会话 28）。这说明至少有一次 class 策略单独不够用。

## 4. 测试覆盖

**21 项测试**（`tests/test_spark_status.py`，2026-10-01 新增）：

| 测试类 | 覆盖范围 | 测试数 |
|--------|----------|--------|
| `TestClassifyIcon` | gray/grey→need, 其他→active, 空→unknown, 大小写不敏感 | 5 |
| `TestRunDiscover` | class 策略命中、过滤无火花、置顶保留、三策略全空报错、class 异常降级 text、去重 class 优先、text/icon 兜底 | 8 |
| `TestDiscoverFriendsWithSpark` | 状态分类(need/active/pinned)、滚动去重、不可滚动时单 pass、异常传播、通用异常返回空 | 5 |
| `TestJsSelectorConsistency` | text/icon 关键词一致性、class 检查 isStickOnTop、icon 使用 closest 向上遍历 | 3 |

**已有测试**（`tests/test_discovery.py`）：mock `_run_discover`，测试 warn-once 和 render-hang。

**仍未覆盖**：JS 选择器在真实浏览器 DOM 中的执行（需集成测试 + 真实 HTML snapshot）。

## 5. 删除后果

**如果只保留 class 策略：**
- 抖音改版导致 class 名变化时，text/icon 兜底消失
- 历史证据：2026-09-29 事故中 class 策略已有一次单独不够用
- 风险：静默发现失败（部分好友漏续但系统不知道）

**如果删除所有策略（整个 spark_status.py）：**
- 发现功能完全消失
- 项目核心功能（续火花）无法运行

## 6. 判断

**保留三策略，但需补测试。** 理由：
1. 三策略并行执行开销极低（三次 JS evaluate）
2. 抖音改版是已验证的常态（2026-09-29 事故）
3. **最大风险**：JS 选择器片段零测试覆盖，选择器过期时无人知晓
4. 建议：用真实页面 HTML snapshot 写集成测试，至少验证 class 策略的选择器匹配
