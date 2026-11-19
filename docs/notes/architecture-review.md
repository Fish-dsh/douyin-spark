# 架构速览与体检（2026-11-19）

一句话：**分层是干净的（27 个模块、零循环依赖、依赖方向单向），问题不在"乱"，在"巨物"——一个 1300 行的编排者，和几个 100~241 行的单函数（会话 58 已拆掉其中最长的两个）。**

## 依赖形状

```
spark.py / run_cron.sh
   └── src.scheduler          编排者: out=15, in=2
        ├── src.discovery     发现: 扫列表 → 判火花 → pending
        │    └── src.spark_status  (4 条识别策略 + CV 兜底)
        ├── src.sender        发送: 表情/文字/确认/兜底
        │    ├── src.vision       视觉兜底(HTTP)
        │    └── src.vision_cv    红↑按钮定位
        ├── src.session       浏览器生命周期(启动/关闭/拦截/内存)
        │    └── src.login        登录态恢复与登录
        ├── src.self_heal     坏了让 LLM 改代码 + 验证 + 回滚
        ├── src.recovery      失败分类
        └── src.streak_history 断签检测

底座(被全面依赖, 自身近乎无依赖): logutil(in=18) / src.config(in=14) / src.perf(in=10)
独立 CLI(不被 import, 由 cron 或人手调): src.health_check / src.weekly_report /
   tools.task_watchdog / tools.strat_stats / tools.observe_net
```

**零循环依赖**不是巧合——`src/__init__.py` 刻意留空就是为了防这个（文件里写着当年 login → src.config → scheduler → session → login 那个环）。

## 数据

| 指标 | 值 |
|---|---|
| src 行数 / tests 行数 | 8311 / 8065（1.03:1，这个比例很健康） |
| 模块数 / 循环依赖 | 27 / 0 |
| 测试 | 539 passed + 1 skipped |
| 无专属测试文件的模块 | debug / recovery / stealth（runtime_lock 由 test_scheduler 的 TestPidLockExemption 覆盖） |

## 巨物清单

| 位置 | 行数 | 一条函数里塞了什么 |
|---|---|---|
| `src/scheduler.py:772 _send_phase` | **241** | 页复用状态机 + 异常矩阵（中断/掉登录/渲染挂起/超时）+ 逐人重试 |
| `src/scheduler.py:371 process_friend` | 262 → **206** | 已拆出 `_await_chat_panel`(41，等面板挂载 + 合成点击抢救) / `_verify_friend_identity`(40，核对 + 一次复检)；剩下的是直线编排 |
| `src/sender.py send_sticker` | 345 → **40** | 已拆成 `_open_sticker_panel`(36) / `_pick_sticker`(15) / `_await_sticker_result`(74)，判据、文案、事件顺序不动 |
| 其余 >100 行 | | confirm_send 170、send_message 158、synthetic_click_row 130、process_account 123、run 121、cv_send_fallback 117 |

剩下的两处加起来 447 行，占 src 的 5.4%，仍然是「改一行要先读两百行」的地方。

## 建议顺序（按性价比）

1. ~~**拆 `send_sticker`**~~ **已做（会话 58）**：三个私有函数 + 四个内联 JS 提到模块级 `_JS_*`，对外签名一字不改，纯读性收益。
2. **拆 `process_friend`**（中风险）：**已拆两段**（会话 58，`_await_chat_panel` / `_verify_friend_identity`）。剩下的「打开目标」段缠着 `_mark`/`_timing` 闭包、`dump_page` 与 `RenderHangDetected` 的抛出点，再拆要连计时器一起改，收益递减、暂缓。
3. **拆 `_send_phase`**（中风险）：把「页复用状态机」与「单人重试」分开，前者现在和异常处理缠在一起。
4. 小重复（`_as_int` 两份、`classify` 两份）顺手合并即可，别为它单开一轮。
5. **不做**：把 scheduler 拆成多个文件。它现在是编排者，拆开只会让 15 条依赖变成 3 个文件互相 import，收益为零。

## 已做

**会话 57 · 删 import shim**（净减 53 行）

```
13 处 try: from . import X / except ImportError: import X   → 删除
```

那是给「把 src/ 塞进 sys.path 后 import 裸模块名」准备的退路，而实际入口只有 `python spark.py` 和 `python -m src.<x>`，两条都是包内相对导入，except 分支永远不触发。21 个模块 import 冒烟全通过。

**会话 58 · 拆 `send_sticker`**（345 → 40 行）

按注释里本来就有的边界切：找按钮 + 等面板 = `_open_sticker_panel`，找表情项 = `_pick_sticker`，点击后的三级判据 = `_await_sticker_result`；四个内联 JS 提到模块级（`_JS_FIND_EMOJI_BTN` / `_JS_FIND_STICKER` / `_JS_PANEL_VISIBLE` / `_JS_PANEL_CLOSED`），与文件里其他常量同风格。

顺带修了一个断链：`send_sticker` 收 `parent` 却从没传给 `events.emit`，sticker 层的事件一直缺 `parent_attempt_id`（文档里承诺的是「上一层失败的 id 作为下一层 parent」，实际只有 scheduler 自己打的那条带）。现在四个出口统一经由 `_emit_sticker` 带上完整溯源四件套。

**会话 58 · 拆 `process_friend` 的两个步骤**（262 → 206 行）

只动不碰计时的两段：等会话面板挂载 + 合成点击抢救 → `_await_chat_panel`，身份核对 + 一次复检 → `_verify_friend_identity`。两者的 `return "fail", False` 上提到调用点，`_timing()` 仍由 `process_friend` 在原位置调用，所以 `[timing] process_friend(...)` 的段数与顺序一字不变。

没动「打开目标」那段：它缠着 `_mark`/`_timing` 闭包、`dump_page` 和 `RenderHangDetected` 的抛出点，拆出来得连计时器一起重构——那是换语义，不是改读性。
