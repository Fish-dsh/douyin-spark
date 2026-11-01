# 多 AI 引擎 (GLM / DeepSeek / AGNES)

## 结论

**生产仅用 GLM（免费），AGNES + DeepSeek 为降级链。** opencode（Zen）已永久关闭，代码已于 2026-10-01 清除。4 天内换过 4 次引擎（外部依赖频繁失效），降级链有实际触发记录。

---

## 1. 引擎定义

| 常量 | 值 | 类型 | 驱动方式 |
|------|-----|------|----------|
| `_ENGINE_GLM` | `"glm"` | 免费 | pi CLI + 智谱 GLM |
| `_ENGINE_AGNES` | `"agnes"` | 免费 | pi CLI + AGNES（并发上限 2） |
| `_ENGINE_PI` | `"pi"` | **付费** | pi CLI + DeepSeek V4.1 Flash |

已删除：`_ENGINE_OPENCODE`（Zen 免费层 2026-09-23 起永久关闭，2026-10-01 清除代码）。

## 2. 降级链

```python
# self_heal.py _run_agent()
primary = _engine()  # 从 config.json 读，默认 glm
if primary == _ENGINE_PI:
    primary = _ENGINE_GLM  # 付费永远不优先
chain = [primary] + [其余免费引擎] + [付费引擎]
# 依次尝试，前一引擎无产出则回落下一引擎
```

当前生产配置：`"self_heal_engine": "glm"` → 降级链 `[glm, agnes, pi]`

## 3. 引擎切换历史（WORKLOG）

| 会话 | 日期 | 切换 | 原因 |
|------|------|------|------|
| 22 | 2026-09-25 | → deepseek（付费） | Zen 免费层永久关闭 |
| 23 | 2026-09-25 | → opencode | 短暂切换 |
| 26 | 2026-09-26 | → glm | 接入智谱 GLM 免费模型 |
| 27 | 2026-09-26 | glm 4.5→4.7 | 4.5 官方下线日已过，4.7 为主力 |

**4 天换 4 次**——外部免费模型端点极不稳定。降级链的设计初衷就是应对这种频繁失效。

## 4. 引擎实测（2026-10-01）

| 引擎 | 冷启动 | 代码修复质量 | 费用 | 结论 |
|------|--------|------------|------|------|
| GLM | ~6s | 正确（`if not f.exists(): return {}` + `state.get("sent", [])`） | 免费 | **主力** |
| DeepSeek | ~3s | 正确+更防御（额外加 JSONDecodeError 处理 + 自测） | 付费 | 保留作灾备 |
| AGNES | 5s-105s | 正确（同 GLM 修复） | 免费 | 保留作灾备 |
| opencode | 未安装 | — | — | **已删除**（Zen 永久关闭） |

## 5. 各引擎 runner

| 引擎 | 函数 | 特殊逻辑 |
|------|------|----------|
| GLM | `_run_glm()` | 指数退避+Full Jitter；429/1305 限流识别；主力→备用模型切换 |
| pi | `_run_pi()` | DeepSeek 付费；`--thinking max`（2026-10-01 校对：现为 `high`，见 `8558656`「付费引擎思考档位 max -> high（平衡成本）」与 `src/self_heal.py:239`；本表是当时的实测快照，未回写） |
| AGNES | `_run_agnes()` | 单线程 |
| AGNES 并行 | `_run_agnes_parallel()` | ThreadPoolExecutor(max_workers=2) |

## 6. 生产实际使用

- **板子 config.json**：`"self_heal_engine": "glm"` + `"self_heal": true`
- **本机 config.json**：无 `self_heal_engine` 键 → 代码默认 `glm`
- **降级链触发记录**：WORKLOG 会话 28 记载 GLM 429 限流导致 35 分钟重试风暴 → 引入 480s 总预算

## 7. 判断

**保留 GLM + AGNES + DeepSeek 降级链。** 理由：
1. 降级链代码量不大（每个 runner ~50-80 行）
2. 外部免费模型频繁失效是已验证的常态
3. AGNES 和 pi(DeepSeek) 作为灾备保留成本低
4. opencode 代码已清除（Zen 永久关闭，无可恢复路径）
