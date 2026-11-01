# 密码登录 (`password_login` / `auto_login`)

## 结论

**活跃使用，但生产环境频繁失败。** 代码路径完整接入生产调用链，用户 2026-10-01 实测确认可用。WORKLOG 会话 15/17 的「死 tab / 反自动化拦截」结论已被推翻。

---

## 1. 调用链

```
spark.py → scheduler.run() → process_account() → open_session()
  → is_logged_in(page) == False → _ensure_login()
    → cfg.get("auto_login") and 有凭据 → auto_login(browser, page, account, password, storage_path)
      → password_login(page, account, password)
        → _show_login_panel() → 切 tab → 填账号 → 填密码 → 点登录 → 等判定
```

| 文件 | 行号 | 说明 |
|------|------|------|
| `src/login.py:438` | `password_login()` 定义 | 完整密码登录流程 |
| `src/login.py:561` | `auto_login()` 定义 | 含风控退避（1h 冷却） |
| `src/login.py:597` | `auto_login` 调用 `password_login` | 唯一调用点 |
| `src/session.py:198` | `_ensure_login` 触发条件 | `cfg.get("auto_login")` + 有凭据 |
| `src/session.py:201` | `auto_login()` 实际调用 | 传入 `storage_path` 防跨账号覆盖 |

## 2. 触发条件

`_ensure_login()` (session.py:186-221) 触发 `auto_login` 需同时满足：
1. `cfg.get("auto_login")` 为 truthy（config.json 中配置）
2. 账号 dict 中有 `account` 和 `password` 字段
3. `is_logged_in(page)` 返回 False（storage.json 过期或无效）

**失败后行为**：退出码 2，进程终止。无二次重试（日志「转密码登录」有误导性，实际直接 exit）。

## 3. 生产日志证据

| 时间 | 日志位置 | 事件 |
|------|----------|------|
| 2026-08-28 | `runtime/spark.log:196` | `账号密码自动登录成功`（测试上下文） |
| 2026-09-05 13:30 | `spark.log:3303` | `未找到账号输入框, 登录失败` |
| 2026-09-05 14:06 | `spark.log:4614-4667` | 切 tab 成功、填手机号成功、填错字段（验证码而非密码）、超时 45s |
| 2026-09-05 14:13 | `spark.log:4689-4692` | 切 tab 成功、填手机号成功、未找到密码输入框 |
| 2026-09-24 13:56 | `spark.log:7846-7852` | `auto_login` 调用失败，但随后 `is_logged_in` 通过（storage.json 仍有效） |

## 4. WORKLOG 历史记录

| 会话 | 日期 | 结论 | 可信度 |
|------|------|------|--------|
| 13 | 2026-08-30 | 密码登录触发但失败（密码框选择器未命中） | 可信（代码 bug，已修） |
| 14 | 2026-09-05 | 扩展选择器至 7 种；headless 被反爬拦截 | 部分可信（反爬可能是临时） |
| 15 | 2026-09-05 | 「密码登录是死 tab」（**已推翻**） | **不可信** — 用户 2026-10-01 实测可用 |
| 17 | 2026-09-24 | 修复 JS 点击；「服务端反自动化静默拒绝」（**已推翻**） | **不可信** — 同上 |
| 18 | 2026-09-24 | 好友F 账号最终通过 `--manual`（短信验证码）获得登录态 | 可信 |

## 5. 测试覆盖

| 测试文件 | 测试方法 | 覆盖范围 |
|----------|----------|----------|
| `tests/test_login.py:12-26` | `TestPasswordInputSelectors` | 选择器元组内容（4 项） |
| `tests/test_login.py:29-68` | `TestAutoLoginStoragePath` | `storage_path` 传递（2 项，**mock password_login**） |
| `tests/test_session.py:166-265` | 6 个 `_ensure_login` 测试 | 分支覆盖（**全部 mock auto_login**） |

**覆盖缺口**：`password_login()` 的浏览器自动化流程（切 tab、填输入、点登录、等判定）**零集成测试**。所有测试都 mock 掉实际登录流程。

### 集成测试可行性评估（2026-10-01）

**结论：完整集成测试不可行，原因如下：**

| 障碍 | 说明 |
|------|------|
| 真实凭据 | 需要抖音账号密码，不能放入 CI |
| 真实浏览器 | `password_login()` 使用 `page.evaluate`/`page.fill`/`page.locator().click` 等，需 Chromium 实例 |
| 验证码/滑块 | 登录流程含人机验证（line 500-502），无法自动化 |
| 抖音 DOM 变化 | 选择器频繁失效正是 self_heal 存在的原因；测试会很快过时 |
| 时序依赖 | 轮询最长 45 秒，`wait_for_timeout` 调用使测试缓慢且脆弱 |

**可改进的方向（非集成测试）：**
- `auto_login` 的 1h 冷却风控逻辑（`login_state.json` 读写 + 时间判定）可纯单元测试
- `save_state`/`restore_state` 的 cookie 序列化往返可用 mock context 测试
- `is_logged_in` 可补充更多 DOM 快照场景（已有 2 项，可扩展）

**建议**：不补集成测试。当前 mock 测试已覆盖分支逻辑；真正的端到端验证只能靠生产运行 + 日志监控。

### 板端实测验证（2026-10-01）

在 OrangePi 上用 Xvfb(:99) + 真实凭据跑了一次完整 `password_login` 流程：

| 步骤 | 结果 |
|------|------|
| 页面加载 `/chat` | ✓ 成功（headless 模式会 404，必须 Xvfb） |
| 点击「登录」入口 | ✓ 成功 |
| 切换「密码登录」tab | ✓ 成功 |
| 填充手机号 | ✓ `input[placeholder*='手机号']` |
| 填充密码 | ✓ `input[type='password']` |
| 点击登录按钮 | ✓ 精确文本「登录」 |
| 服务端响应 | ✗ 静默拒绝（按钮消失但未跳转，无验证码） |

**结论**：选择器和流程逻辑全部正确，失败原因是抖音服务端反自动化拒绝（与生产日志一致）。代码无需修改。

## 6. 删除后果

如果删除密码登录代码：
- `_ensure_login()` 在未登录时只能 `sys.exit(2)`，无人值守场景无法恢复
- `--password` CLI 入口失效
- 依赖 storage.json 永久有效（cookie 有过期风险）
- 板子 cron 无人值守时，一旦 storage.json 过期就彻底停摆

## 7. 判断

**保留。** 理由：
1. 用户实测可用，WORKLOG 的错误结论已修正
2. 是无人值守场景的唯一自动恢复路径
3. 即使当前生产主要依赖 storage.json，cookie 过期时密码登录是兜底
4. 代码量不大（~80 行核心逻辑），维护成本低
