# 自愈功能重写设计（pi + opencode zen 修复引擎）

> 日期：2026-08-09 · 状态：已实施

## 背景与动机

原 `self_heal.py` 调用 `opencode run --agent maintainer` 让 AI agent 改代码，过重：
板子（OrangePi Zero 3，1.5G）上 opencode 占用资源大、启动慢、180s 超时。
用户要求替换为轻量工具 **pi**（极简 TS agent，核心仅 read/write/edit/bash），
接 opencode zen 网关的免费模型 `deepseek-v4-flash-free`（key 填 `public` 即可，零成本）。
板子需完全无人值守，pi 对代码修改**全权负责**（含业务逻辑），风险用备份回滚 + 验证门禁控制。

## 架构

```
spark.py 捕获故障
   └─ self_heal(error_type, error_msg, extra)  ← 接口不变，spark.py:1082 调用点不动
       1. 冷却检查（签名级 + 每自然日次数上限）
       2. 收集故障 bundle（日志尾部 + 报错 + 相关代码，秘密脱敏）
       3. 备份受影响 .py 到 heal_backups/<时间戳>/
       4. subprocess 调 pi 无头修复：
          pi -p -a --provider zen --model deepseek-v4-flash-free \
             --append-system-prompt <项目/.pi/maintainer.md> <bundle>
       5. 验证门禁（本地执行，不信任 AI 自证）：
          a. py_compile 全量 .py
          b. spark.py --dry-run 真实执行（退出码 0 且无 traceback）
             ※ 若因「未登录」失败（登录态问题非代码问题）→ 降级为仅 py_compile
       6. 通过 → 返回 True（spark.py 重试真发送）
          失败 → 回滚 heal_backups/ 备份 + 记录冷却 + 返回 False
```

## 组件

- `self_heal.py`：重写。保留 `_error_signature`/冷却骨架；`_run_opencode` → `_run_pi`；
  新增备份/回滚、验证门禁、bundle 脱敏。接口 `self_heal(error_type, error_msg, extra="") -> bool`。
- `.pi/maintainer.md`：pi 的 maintainer 系统提示（约束 + 修复流程 + 输出格式）。
- `~/.pi/agent/models.json`（全局）/ `.pi/models.json`（项目）：注册 zen provider。
- 删除 `.opencode/agent/maintainer.md`（opencode 方案废弃）。
- README/WORKLOG 更新。

## 实施结果

- 2026-08-09 重写完成：self_heal.py 全量通过 17/17 单测；后续演进见 WORKLOG 会话 7/8/8b/8c（冷却移前、瞬时故障先重启浏览器、OOM 三态门禁）。
- 2026-08-11 增加视觉诊断：`_dump_page` 截图 → mimo 多模态判断选择器失效根因并入 bundle；失败降级纯文本，不阻塞自愈。

## 关键决策

- 模型：`deepseek-v4-flash-free`（免费，key=public）。无需 API key。
- 验证：py_compile + dry-run 双门禁；dry-run 由 self_heal 本地执行；登录态失效导致的
  dry-run 失败不算代码修复失败。
- 冷却：签名级 `.heal_cooldown`（沿用）+ 同签名每自然日最多 1 次 AI 修复。
- 脱敏：bundle 永不包含 .env / storage.json / state.json 内容；日志尾部过滤疑似
  cookie/token/手机号行。
- 部署：板子装 nodejs + pi（全局 models.json 写 zen）；本机同。自愈不常驻。

## 测试

- 单测（unittest）：冷却、脱敏、报告解析、备份/回滚、pi 调用命令构造。
- 本机演练：故意破坏 spark_status.py 一个选择器 → 触发 self_heal → pi 修复 →
  py_compile + dry-run 通过。
- 板子部署后同样演练一次。
