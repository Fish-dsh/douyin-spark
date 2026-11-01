# douyin-spark 组件存废证据集

> 生成日期: 2026-10-01
> 目的: 对项目中「不确定是否必要」的组件逐一收集代码证据、WORKLOG 记录、测试覆盖、生产日志，
> 供决策者判断保留/删除/简化。每条证据附文件路径和行号，可直接跳转验证。

## 目录

| # | 组件 | 文件 | 结论摘要 |
|---|------|------|----------|
| 1 | 密码登录 (`password_login` / `auto_login`) | [01-password-login.md](01-password-login.md) | **活跃使用**，但生产环境频繁失败；用户 2026-10-01 实测确认可用 |
| 2 | `--verify-send` 发送门禁 | [02-verify-send.md](02-verify-send.md) | **核心防御层**：自愈后唯一能验证发送链路的手段，删除会导致发送路径回归无感知 |
| 3 | `streak_history.py` 火花历史 | [03-streak-history.md](03-streak-history.md) | **生产中调用**，但自愈触发路径曾有 bug（会话 29 修复）；覆盖门禁是静默发现失败的唯一安全网 |
| 4 | 多 AI 引擎 (GLM/DeepSeek/AGNES) | [04-multi-engine.md](04-multi-engine.md) | **生产仅用 GLM**，其余为降级链；opencode 已清除（Zen 永久关闭） |
| 5 | 多发现策略 (class/text/icon) | [05-discovery-strategies.md](05-discovery-strategies.md) | **三策略并行执行、结果合并**；class 为主力，text/icon 为抖音改版的兜底，零测试覆盖 |
| 6 | 浏览器复用 (`browser_reuse`) | [06-browser-reuse.md](06-browser-reuse.md) | **性能优化**：1.5G 内存板子上省冷启动时间；有内存回收安全阀；文档矛盾已修复 |

## 如何使用

1. 每个文件包含：代码调用链、WORKLOG 历史记录、测试覆盖分析、删除后果评估
2. 所有证据附 `文件:行号`，可用编辑器直接跳转
3. 结论摘要中的判断基于证据，但最终决策需结合运维经验
