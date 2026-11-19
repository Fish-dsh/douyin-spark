"""主调度器：两阶段流程（发现→发送）、抖动、窗口校验、退出码语义。"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

from logutil import _offline_entry, log

from . import events, perf, recovery, streak_history
from .config import (
    CHAT_URL,
    DEFAULT_BROWSER_RECYCLE_MB,
    DEFAULT_BROWSER_REUSE,
    RUNTIME_DIR,
    SEND_STICKER,
    get_account_storage_file,
    in_window,
    load_config,
    load_state,
    write_state,
)
from .debug import dump_page
from .discovery import HealRetryFailed, RenderHangDetected, discover_pending_friends
from .finder import chat_page_ready, page_render_state, wait_chat_list, wait_chat_ui
from .net_sniff import sniff_report
from .self_heal import self_heal
from .sender import (
    click_friend,
    confirm_send,
    cv_send_fallback,
    read_tail,
    send_message,
    send_sticker,
    synthetic_click_row,
    verify_chat_opened_ready,
)
from .session import (
    LoginRequiredError,
    available_mb,
    close_session,
    free_page_memory,
    memory_report,
    open_session,
    save_session_state,
)

_HEALABLE_ERRORS = (TimeoutError, OSError, ConnectionError, BrokenPipeError)

# 拟人化节奏(2026-10-05): 原先好友之间零间隔连发、整轮只在开头抖 0-5 分钟,
# 是"定时批量任务"的强特征。改为好友间随机停顿, 起始抖动放宽到 30 分钟。
# 数值已移入 src/perf.py 的 send_gap_ms(板端 500-1200ms, 桌面档 300-800ms)。
# 2026-11-01: 原 3.0–15.0s(平均 9s/好友, 一晚 9 个好友≈2 分钟)。用户判断
# 这个节奏给得过多: 真人连着点几个「续火花」本来就是一两秒一个, 9s 反而
# 不像人。改为 0.5–1.2s; 需要回退用环境变量 SPARK_SEND_GAP="3,15"。
_START_JITTER_MAX = 1800  # 每轮起始随机抖动上限(秒)

_PID_FILE = None  # type: ignore[assignment]


# 业务心跳(ChatGPT 外部评审 round-2 ④): 健康检查原先只认「进程还活着」, 但
# 「活着但卡死」(如 Playwright 调用挂起而 timeout 未触发)会被豁免分支静音整晚。
# 主流程在每个业务事件上敲 heartbeat.json; 豁免改看「心跳是否新鲜」——
# 进程活着 + 心跳新鲜 = 真在推进, 才不判定。写失败绝不阻断主流程。
# 非生产入口分流(2026-10-07): pytest 有 conftest 的 _redirect_heartbeat 夹具兜底,
# 但板端 venv 无 pytest, 只能 `python -m unittest tests.test_x` 逐模块跑, 夹具不加载
# → 心跳直写生产 heartbeat.json(实测把它覆盖成 verify_send_start/main)。同源判据再兜一层。
def _heartbeat_filename() -> str:
    return "heartbeat.test.json" if _offline_entry() else "heartbeat.json"


_HEARTBEAT_FILE = RUNTIME_DIR / _heartbeat_filename()


# 心跳的"身份"字段(2026-11-01 会话50, 外部评审两轮之后): 只有 stage/账号/时间时,
# 上一轮留下的心跳与这一轮完全无法区分 —— 任何读者都会把旧心跳当成"新任务还活着"。
# task_id 由 run() 启动时生成, 心跳与 events 模块共用同一个值; seq 在同一轮内递增,
# 用来判断"心跳在新, 但一步没往前走"(卡死) 与 "心跳根本没更新"(没跑) 是两回事。
_TASK_ID = ""
_HB_SEQ = 0
_HB_STAGE = ""
_HB_STAGE_TS = 0.0


def _heartbeat(stage: str, account: str = "", friend: str = "") -> None:
    global _HB_SEQ, _HB_STAGE, _HB_STAGE_TS
    if stage != _HB_STAGE:
        _HB_STAGE = stage
        _HB_STAGE_TS = time.time()
    _HB_SEQ += 1
    try:
        tmp = _HEARTBEAT_FILE.with_suffix(".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(
            json.dumps(
                {
                    "ts": time.time(),
                    "iso": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "task_id": _TASK_ID,
                    "seq": _HB_SEQ,
                    "stage": stage,
                    "stage_start_ts": _HB_STAGE_TS,
                    "account": account,
                    "friend": friend,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, _HEARTBEAT_FILE)
    except Exception:
        pass


def _check_clock_sanity() -> None:
    """OrangePi 无 RTC 电池, 启动时可能时间不对; 发警告但不阻断(窗口检查会兜底)。"""
    now = datetime.now()
    if now.year < 2026:
        log(f"[warn] 系统时钟异常({now:%Y-%m-%d %H:%M}), RTC 可能未同步")


_PID_STALE_SEC = 3 * 3600  # PID 文件超过该年龄一律视为陈旧锁(兜住 PID 复用/无权限读取)


def _pid_is_spark(pid: int) -> bool:
    """该 PID 是否确实是 spark.py 主任务(而非被复用的无关进程)。"""
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace")
    except OSError:
        return False
    return "spark.py" in cmdline


def _acquire_pid_lock() -> bool:
    """PID 文件互斥: 防止 cron 重叠导致双实例争抢 512MB 内存。返回 True = 获锁成功。

    **豁免位 `SPARK_SKIP_PID_LOCK=1`**(持锁期间手工跑验证子进程专用): 主任务持锁期间想手工跑
    `spark.py --dry-run/--verify-send` 时, 子进程若照常争锁, 会看见父 PID 存活 → 走
    "另一实例运行中"分支 → 裸 return → **rc=0 且日志无「待发送≥1」** → `_log_evidence`
    判定无证据(2026-10-01 评审发现)。置位后本进程不写 PID 文件, 因此也不会在退出时误删
    父进程的锁。
    """
    global _PID_FILE
    if os.environ.get("SPARK_SKIP_PID_LOCK", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        log("[lock] SPARK_SKIP_PID_LOCK 已置位, 跳过 PID 互斥(不写 PID 文件)")
        return True
    _PID_FILE = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "runtime", ".spark.pid"
    )
    try:
        os.makedirs(os.path.dirname(_PID_FILE), exist_ok=True)
        if os.path.isfile(_PID_FILE):
            stale = False
            try:
                age = time.time() - os.path.getmtime(_PID_FILE)
                if age > _PID_STALE_SEC:
                    stale = True
                    log(
                        f"[warn] PID 文件已存在 {age / 3600:.1f}h"
                        f"(超过 {_PID_STALE_SEC / 3600:.0f}h 上限), 视为陈旧锁并接管"
                    )
            except OSError:
                pass
            if not stale:
                old_pid = None
                try:
                    old_pid = int(Path(_PID_FILE).read_text(encoding="utf-8").strip())
                    os.kill(old_pid, 0)
                except (ValueError, OSError):
                    old_pid = None
                if old_pid is not None:
                    if _pid_is_spark(old_pid):
                        log(f"[warn] 另一实例运行中(PID {old_pid}), 退出")
                        return False
                    log(
                        f"[warn] PID {old_pid} 存活但 cmdline 非 spark.py"
                        "(疑似 PID 复用), 视为陈旧锁并接管"
                    )
        with open(_PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        return True
    except OSError as exc:
        log(f"[warn] PID 锁获取失败(继续执行): {exc}")
        return True


def _release_pid_lock() -> None:
    if _PID_FILE:
        try:
            os.unlink(_PID_FILE)
        except OSError:
            pass


def _fmt_duration(sec: float) -> str:
    """格式化耗时: <60s 显示秒, >=60s 显示分秒。"""
    if sec < 60:
        return f"{sec:.1f}s"
    m, s = divmod(sec, 60)
    return f"{int(m)}m{s:.1f}s"


def _heal_after_send_failure(
    fname: str, ai_healed: bool, page=None, *, parent: str | None = None
) -> bool:
    """发送/确认链路失败(返回 fail 而非抛异常)时补一次本地恢复判定。

    这是 except 分支的盲区: 抖音改版若导致"点了没发出去""发到错的地方"等
    静默失败, 不会抛异常, 于是永远不会走恢复链路, 只会静默地没续上火花。
    这里把它显式送进 `self_heal` 分类(静默失败 → transient → 重试一次)。

    page: 仍在使用的页面对象(必须在关闭前传入)。调用方要保证此处在关页之前,
    否则拿不到截图 —— 而截图是"页面改版"场景下视觉诊断的关键输入。
    """
    if ai_healed:
        return True
    shot = ""
    if page is not None:
        try:
            shot = dump_page(page)
        except Exception as exc:
            log(f"【{fname}】发送失败后截图失败(继续无截图自愈): {exc}")
    log(f"【{fname}】发送未确认(非异常路径), 触发本地恢复判定")
    ret = bool(
        self_heal(
            "process_friend",
            f"发送未确认: {fname}",
            f"friend={fname}",
            screenshot_path=shot,
        )
    )
    # 自愈只做分类, 修没修好要看重跑 —— 所以 verified 恒为 None
    events.emit(
        "heal",
        "classify",
        "ok" if ret else "failed",
        friend=fname,
        attempt=events.attempt_id(),
        parent=parent,
        verified=None,
        extra={"retry": ret, "reason": "send_unconfirmed"},
    )
    return ret


def _worth_local_retry(failure_msg: str) -> bool:
    """本地策略判定: 该失败是否值得进补扫(纯函数, 与 self_heal 共用同一张策略表)。

    抄自同类无人值守项目的 continue_on_failure: 一个本地判定"救不回"的好友
    (未知错误 / 登录类)不该继续吃掉后面好友的时间, 直接跳过。
    """
    return recovery.should_retry(
        recovery.policy(recovery.classify("process_friend", failure_msg))
    )


def _discard_shared_browser(browser, context) -> None:
    """复用模式丢弃故障共享浏览器: 先关 context 再关 browser, 避免 Target closed 噪音。"""
    close_session(None, context)
    close_session(browser, None)


def _recycle_if_low_memory(shared_browser, threshold_mb: float):
    """复用模式下按剩余内存决定是否回收浏览器实例。

    返回 (browser, recycled): MemAvailable 低于阈值时关掉实例并返回 None
    (下个好友冷启动重建); 内存充足或读不到内存信息时原样保留, 继续复用。
    读不到(`/proc/meminfo` 不可用, 如非 Linux)时保守保留, 不误杀复用。
    """
    avail = available_mb()
    if avail is None:
        log("[mem] 读取可用内存失败, 保持复用当前浏览器实例")
        return shared_browser, False
    if avail >= threshold_mb:
        return shared_browser, False
    log(
        f"[mem] 可用内存 {avail:.0f}MB < 阈值 {threshold_mb:.0f}MB, "
        "回收复用浏览器实例(下个好友冷启动重建)"
    )
    close_session(shared_browser, None)
    memory_report("session-recycled")
    return None, True


def _panel_wait_ms() -> int:
    """可信点击后等待会话面板挂载的首段预算(毫秒)。

    2026-11-01: 默认值改由性能档位决定(perf.panel_wait_ms), SPARK_PANEL_WAIT_MS
    仍可覆盖(区间 500-15000ms), 真机微调不必改代码。
    """
    try:
        v = int(os.environ.get("SPARK_PANEL_WAIT_MS", "") or perf.ms("panel_wait_ms"))
    except ValueError:
        return perf.ms("panel_wait_ms")
    return v if 500 <= v <= 15000 else perf.ms("panel_wait_ms")


def _send_gap_range() -> tuple[float, float]:
    """好友间停顿区间; SPARK_SEND_GAP="min,max" 可覆盖(非法值回退默认)。"""
    raw = os.environ.get("SPARK_SEND_GAP", "").strip()
    if raw:
        parts = raw.split(",")
        if len(parts) == 2:
            try:
                lo, hi = float(parts[0]), float(parts[1])
                if 0 <= lo <= hi:
                    return (lo, hi)
            except ValueError:
                pass
    return perf.send_gap_range_sec()


def _page_usable(page) -> bool:
    """跨好友复用的门槛: 页面没关、仍在聊天页且会话列表已就绪。

    任何一个条件不满足都退回「重开会话」——复用只是省掉重复加载, 不允许
    把上一轮的不确定状态带进下一个好友。
    """
    try:
        if page is None or page.is_closed():
            return False
        return chat_page_ready(page)
    except Exception:
        return False


# 复用页面时对身份核对的严格度(见 process_friend 内注释); 默认 False = 原语义
_IDENTITY_STRICT = False


def process_friend(
    page, account: dict, name: str, dry_run: bool, *, parent: str | None = None
) -> tuple[str, bool]:
    # 复用上一个好友的页面时, 身份核对「读不到」不许放行: 右栏可能还是上一位,
    # 半渲染瞬间放行 = 把消息发给不确定的人。由 _send_phase 按本轮是否复用设置。
    strict_identity = _IDENTITY_STRICT
    """处理单个好友; 返回 (状态, 是否已发送)。状态: ok / fail / logged_out"""
    acc_name = account.get("name", "?") if isinstance(account, dict) else "?"
    # a0 是本好友这一轮的根 attempt: 表情→文字→视觉→自愈重跑 全部挂在它下面
    a0 = parent or events.attempt_id()
    log(f"======== 处理好友: {name} (账号 {acc_name}) ========")
    events.emit(
        "task",
        "friend_start",
        "ok",
        friend=name,
        account=acc_name,
        attempt=a0,
    )
    log(f"[debug] process_friend 开始: {name}")
    _heartbeat(f"friend_start:{name}", acc_name, name)
    _t0 = time.monotonic()

    try:
        # 性能(2026-11-01): 旧实现每个好友都整页 goto 一次, 板端实测每次重载
        # + 恢复登录态 + 等列表 ≈ 30s, 9 个好友就是 4-5 分钟纯等待; 而会话列表
        # 靠 WS 实时更新, 同一个账号内根本没有重载的必要。改为: 已在聊天页且列
        # 表就绪就跳过, 否则照旧重载(任何不确定都退回重载, 保守)。
        # 出问题不用改代码: 环境变量 SPARK_SKIP_RELOAD=0 即可恢复旧行为。
        _skip_reload = os.environ.get("SPARK_SKIP_RELOAD", "1") != "0"
        if _skip_reload and chat_page_ready(page):
            log("[perf] 已在聊天页且会话列表就绪, 跳过本次整页重载")
        else:
            page.goto(
                CHAT_URL, wait_until="domcontentloaded", timeout=perf.ms("page_dom_ms")
            )
            page.wait_for_timeout(perf.ms("short_settle_ms"))
        if not wait_chat_ui(page):
            log("[warn] 聊天区域未完全就绪, 继续尝试")
        from .login import is_logged_in

        if not is_logged_in(page, quick=True):
            log("[登录] 会话已失效, 终止后续好友")
            log(
                f"[timing] process_friend({name}) 耗时 {_fmt_duration(time.monotonic() - _t0)}"
            )
            return "logged_out", False
        list_ok = wait_chat_list(page)
    except Exception as exc:
        log(f"[错误] 打开聊天页失败: {exc}")
        dump_page(page)
        log(
            f"[timing] process_friend({name}) 耗时 {_fmt_duration(time.monotonic() - _t0)}"
        )
        return "fail", False

    if not list_ok:
        if page_render_state(page) == "blank":
            # 渲染挂死(body 全空)是瞬时环境问题, 与发现阶段同策: 抛给调用方重开会话
            # 重试, 绝不在这张残缺页面上硬撑 —— 硬撑必然「未找到表情按钮」→文字兜底
            # →确认失败。硬撑只会白烧这一晚的发送窗口(2026-10-01 事故: 好友在 150s 空页上判失败)。
            raise RenderHangDetected(
                f"好友『{name}』会话列表超时且页面 body 全空(渲染挂死)"
            )
        log("[warn] 等待会话列表超时但页面有内容, 按疑似选择器失效继续")

    clicked = click_friend(page, name)
    if clicked is None:
        log(f"[重试] 首次定位『{name}』失败, 重新加载页面重试")
        try:
            page.reload(wait_until="domcontentloaded", timeout=perf.ms("reload_ms"))
            page.wait_for_timeout(perf.ms("post_reload_settle_ms"))
            if not wait_chat_ui(page):
                log("[重试] 聊天区域未就绪")
            clicked = click_friend(page, name)
        except Exception as exc:
            log(f"[重试] 重载失败: {exc}")
    if clicked is None:
        log(f"[失败] 未能定位到好友『{name}』, 跳过该好友")
        log(
            f"[timing] process_friend({name}) 耗时 {_fmt_duration(time.monotonic() - _t0)}"
        )
        return "fail", False

    if dry_run:
        log(f"[dry-run] 『{name}』定位链路 OK, 未真正发送 (如需发送请去掉 --dry-run)")
        log(
            f"[timing] process_friend({name}) 耗时 {_fmt_duration(time.monotonic() - _t0)}"
        )
        return "ok", False

    # 性能(2026-11-01): 首段等待 15s 太长。板端实测(下午探针)可信点击有时根本
    # 不打开面板, 而合成点击 1s 内就能打开 —— 白等 12.5s/好友。改为首段 2.5s
    # (SPARK_PANEL_WAIT_MS 可调), 未挂载立刻合成点击抢救, 第二段仍等 15s。
    # 2026-10-06 好友A事故: 点击列表项同秒即返回, 会话面板(含消息输入框)可能尚未
    # 挂载; 直接发送会误把顶部搜索框 input[type=text] 当输入框, 字打进搜索框假发送。
    # 故先等消息输入框(唯一的 contenteditable)可见再走发送链路。
    # 取证 v1-v6(板子实测): 重燃会话对一切可信点击 90s 不挂载面板, 唯有会话行上的
    # 合成事件序列能打开 → 首等超时后用 synthetic_click_row 抢救一次, 仍不挂载则
    # 快速失败(不盲发: 无面板必败, 盲发只会烧重试预算并留下假发送风险)。
    try:
        page.wait_for_selector(
            '[contenteditable="true"]', state="visible", timeout=_panel_wait_ms()
        )
    except Exception:
        hit = synthetic_click_row(page, name)
        if hit:
            log(
                f"[核对] 可信点击未打开面板, 已向会话行合成点击 {hit}, "
                f"再等 {perf.ms('contenteditable_ms') / 1000:.1f}s"
            )
        else:
            log("[核对] 可信点击未打开面板, 且会话行合成点击定位失败")
        try:
            page.wait_for_selector(
                '[contenteditable="true"]',
                state="visible",
                timeout=perf.ms("contenteditable_ms"),
            )
        except Exception:
            log(
                f"[失败] 『{name}』会话面板两次等待"
                f"({_panel_wait_ms() / 1000:.1f}s+{perf.ms('contenteditable_ms') / 1000:.1f}s)"
                f"均未挂载, 快速失败不盲发"
            )
            log(
                f"[timing] process_friend({name}) 耗时 {_fmt_duration(time.monotonic() - _t0)}"
            )
            return "fail", False

    # 身份核对: 骨架期(读不到)先复检再裁决, 见 sender.verify_chat_opened_ready。
    ok_id, id_detail = verify_chat_opened_ready(page, name, strict=strict_identity)
    log(f"[核对] {id_detail}")
    events.emit(
        "identity",
        "verify",
        "ok" if ok_id else "failed",
        friend=name,
        account=acc_name,
        attempt=a0,
        verified=True if "confirmed" in id_detail else None,
        error_class=None if ok_id else "identity_conflict",
        extra={"detail": id_detail[:80]},
    )
    if not ok_id:
        # 性能(2026-11-01): 复用页面时, 上一个好友的会话面板可能还挂在右栏,
        # 此时可信点击没生效 → 身份核对看到的是"上一个好友" → 判冲突。板端探针
        # 实测合成点击能可靠切换会话, 故冲突时先抢救一次(最多一次, 不循环)。
        hit = synthetic_click_row(page, name)
        if hit:
            log(f"[重试] 身份核对未通过, 已向会话行合成点击 {hit}, 再复检一次")
            try:
                page.wait_for_selector(
                    '[contenteditable="true"]',
                    state="visible",
                    timeout=perf.ms("contenteditable_retry_ms"),
                )
            except Exception:
                log("[重试] 合成点击后仍无会话面板")
            ok_id, id_detail = verify_chat_opened_ready(
                page, name, strict=strict_identity
            )
            log(f"[重试] 复检: {id_detail}")

    if not ok_id:
        log(f"[失败] 『{name}』发送前身份核对未通过, 跳过(不盲发)")
        log(
            f"[timing] process_friend({name}) 耗时 {_fmt_duration(time.monotonic() - _t0)}"
        )
        return "fail", False

    # 固定发送"续火花": 优先表情包, 失败以文字兜底。
    log(f"[发送] 模式: 表情包 ({SEND_STICKER})")
    a_sticker = events.attempt_id()
    sent, method = send_sticker(
        page,
        SEND_STICKER,
        attempt=a_sticker,
        parent=a0,
        friend=name,
        account=acc_name,
    )
    log(f"[发送] 生效策略: {method}")
    events.emit(
        "sticker",
        "send",
        "ok" if sent else "failed",
        friend=name,
        account=acc_name,
        attempt=a_sticker,
        parent=a0,
        extra={"method": method[:60]},
    )
    if sent:
        log(
            f"[timing] process_friend({name}) 耗时 {_fmt_duration(time.monotonic() - _t0)}"
        )
        return "ok", True
    log(f"[失败] 『{name}』表情发送失败, 以文字兜底『{SEND_STICKER}』")

    log(f"[发送] 文案: {SEND_STICKER}")
    log(f"[debug] 调用 send_message, text={SEND_STICKER}")
    # 基线取在**文字发送之前**而非身份闸门之后: 表情层可能已静默发出去一条(只是自身
    # 没证实), 若用更早的基线, 那条表情气泡会被文字确认当成「我这条的新增证据」→ 假成功。
    base = read_tail(page)
    log(f"[确认] 文字层基线: 气泡数={base[0]}, 尾部={base[1][:24]!r}")
    a_text = events.attempt_id()
    sent, method = send_message(
        page,
        SEND_STICKER,
        attempt=a_text,
        parent=a_sticker,
        friend=name,
        account=acc_name,
    )
    log(f"[发送] 生效策略: {method}, sent={sent}")
    okc, detail = confirm_send(
        page,
        SEND_STICKER,
        baseline=base,
        attempt=a_text,
        friend=name,
        account=acc_name,
    )
    log(f"[确认] {detail}")
    # 与 confirm_send 共用 a_text: 先报 ok 又被否掉 = false_positive(谎报)
    events.emit(
        "text",
        "send",
        "ok" if (sent and okc) else "failed",
        friend=name,
        account=acc_name,
        attempt=a_text,
        parent=a_sticker,
        verified=okc or None,
        extra={"strategy_sent": sent, "confirmed": okc},
    )
    log(f"[debug] confirm_send 返回 okc={okc}, detail={detail}")
    if sent and okc:
        log(
            f"[timing] process_friend({name}) 耗时 {_fmt_duration(time.monotonic() - _t0)}"
        )
        return "ok", True
    log(f"[失败] 『{name}』发送未确认成功")
    # Emergency Visual Fallback: 表情+文字各发送层全失败(=本来就要失败)时,
    # OpenCV 定位红色↑按钮抢救一次。正常好友不经过这里, 零回归。
    a_cv = events.attempt_id()
    ok_cv, cv_detail = cv_send_fallback(
        page, SEND_STICKER, name, attempt=a_cv, parent=a_text, account=acc_name
    )
    if ok_cv:
        log(f"[发送] 视觉兜底救回: {cv_detail}")
        log(
            f"[timing] process_friend({name}) 耗时 {_fmt_duration(time.monotonic() - _t0)}"
        )
        return "ok", True
    log(f"[失败] 『{name}』视觉兜底也未成功: {cv_detail}")
    log(f"[timing] process_friend({name}) 耗时 {_fmt_duration(time.monotonic() - _t0)}")
    return "fail", False


def has_login_state(account: dict) -> bool:
    """账号是否有已保存的登录态(storage 文件存在且有未过期 cookies)。

    调度判据: 只要"当前能登录"就应处理——登录态有效 ≠ 必须配凭据。
    main 账号有有效 storage.json 但无 account/password, 需靠本函数放行;
    好友F 无 storage 文件则不放行(未登录不算可处理账号)。
    """
    try:
        sf = get_account_storage_file(account)
        data = json.loads(sf.read_text(encoding="utf-8"))
        cookies = data.get("cookies", []) or []
        now = time.time()
        return any(
            c.get("expires", 0) > now and "douyin.com" in str(c.get("domain", ""))
            for c in cookies
        )
    except Exception:
        return False


def _discover_with_hang_retry(
    p2, cfg: dict, account: dict, state: dict, today: str, force: bool, dry_run: bool
) -> tuple[list[str], int]:
    """发现阶段(含渲染挂死重开重试)。

    渲染挂死(body 全空)是瞬时环境问题: 重开会话重试 1-2 次, 不走恢复策略
    (2026-09-30 事故: 渲染挂死被误判为选择器失效, 白烧 35 分钟 API(旧 AI 链))。

    返回 (pending, 退出码): 退出码 0 = 发现成功(pending 可为空);
    1 = 渲染挂死重开仍空白/一般异常; 2 = 未登录且无法自动登录(重试无益, 但不影响
    其他账号); 3 = 自愈后重试仍失败(需冷启动重跑)。
    KeyboardInterrupt 不在此捕获, 交由调用方统一处理(与原行为一致)。
    """
    _HANG_RETRIES = 2
    hang_retries = 0
    browser = context = page = None
    _t0 = time.monotonic()
    _heartbeat("discover_start", account.get("name", "?"))
    while True:
        try:
            browser, context, page = open_session(p2, cfg, account)
            list_ok = wait_chat_list(page)
            if not list_ok and page_render_state(page) == "blank":
                raise RenderHangDetected("会话列表超时且页面 body 全空(渲染挂死)")
            if list_ok:
                log("[wait] 聊天列表已就绪")
            else:
                # 超时但页面有内容: 不谎报「已就绪」, 交由发现策略判定是否选择器失效
                log(
                    "[warn] 等待会话列表超时但页面有内容, 按疑似选择器失效继续发现"
                    "(发现失败将走本地恢复策略)"
                )
            pending = discover_pending_friends(page, cfg, state, today, force, dry_run)
            _heartbeat("discover_done", account.get("name", "?"))
            sniff_report(page)
            if pending:
                save_session_state(context, account, tag=" (发现阶段)")
            close_session(browser, context)
            memory_report("discovery-closed")
            log(f"[timing] 发现阶段耗时 {_fmt_duration(time.monotonic() - _t0)}")
            return pending, 0
        except RenderHangDetected as exc:
            hang_retries += 1
            close_session(browser, context)
            browser = context = page = None
            memory_report("discovery-render-hang")
            if hang_retries > _HANG_RETRIES:
                log(
                    f"[ERROR] 渲染挂死重开 {_HANG_RETRIES} 次仍空白, 放弃本账号"
                    f"(瞬时环境问题, 不触发本地恢复): {exc}"
                )
                log(f"[timing] 发现阶段耗时 {_fmt_duration(time.monotonic() - _t0)}")
                return [], 1
            log(
                f"[warn] 检测到渲染挂死, 重开会话重试 {hang_retries}/{_HANG_RETRIES}: {exc}"
            )
        except HealRetryFailed as exc:
            log(f"自愈后重试仍失败, 返回码 3 交由 cron 脚本冷启动重跑: {exc}")
            close_session(browser, context)
            log(f"[timing] 发现阶段耗时 {_fmt_duration(time.monotonic() - _t0)}")
            return [], 3
        except LoginRequiredError as exc:
            # 登录态失效: 重试/自愈都救不回(需要人工登录), 但**绝不能带走其他账号**。
            close_session(browser, context)
            log(f"[登录] 本账号登录态失效, 跳过本账号: {exc}")
            log(f"[timing] 发现阶段耗时 {_fmt_duration(time.monotonic() - _t0)}")
            return [], 2
        except Exception as exc:
            log(f"发现阶段异常: {exc}")
            if page is not None:
                dump_page(page)
            close_session(browser, context)
            log(f"[timing] 发现阶段耗时 {_fmt_duration(time.monotonic() - _t0)}")
            return [], 1


def _coverage_gate(account_name: str, pending: list[str], state: dict) -> int:
    """P5 发现覆盖对照门禁。返回 0(继续正常流程) 或 3(自愈成功, 需冷启动重跑补发)。

    近3天已续好友应再次出现在今日发现名单; 缺失 = 疑似列表漏检
    (2026-09-29「抵不过你眉眼带笑」事故: 列表文本有火花但 class 策略漏检)。
    护栏: ①per-account 24h 冷却 ②走现有自愈链路, 成功后 rc=3 冷启动重跑当晚补发。
    ③**只在无好友可发时才熔断**: 手里已经有一批确认待续的好友时, 缺失只是「可能还
    漏了别人」的怀疑, 为一个怀疑把整晚 0 发送押上去是拿爆炸半径换灵敏度
    (2026-10-07: 改名漂移触发误判 → main 账号 5 个已确认好友一个没发)。此时只告警,
    照常发送, 真漏的那位下一晚会由本门禁/漏发检测再抓到。
    """
    missing = streak_history.check_recent_coverage(
        account_name, list(pending) + list(state.get("sent", []))
    )
    if not missing:
        return 0
    if pending:
        log(
            f"[streak-history][warn] 近3天已续好友 {missing} 未出现在今日发现名单, "
            f"但当前有 {len(pending)} 个确认待续好友 → 不熔断, 先照常发送"
        )
        return 0
    if streak_history.coverage_heal_on_cooldown(account_name):
        log("[streak-history] 覆盖缺失自愈 24h 冷却中, 仅告警不重触")
        return 0
    streak_history.record_coverage_heal(account_name)
    log("[heal] 发现覆盖缺失, 触发本地恢复策略(发现链路)")
    try:
        healed = self_heal(
            "discover",
            f"发现覆盖缺失: {', '.join(missing)}",
            extra="近3天已续好友未出现在今日发现列表, 疑似列表漏检",
        )
    except Exception as exc:
        # 自愈抛异常不得中断本账号发送: 与其他调用点保护惯例一致, 按本次发现结果继续
        log(f"[heal] 覆盖缺失自愈异常, 按本次发现结果继续发送: {exc}")
        return 0
    if healed:
        log("[heal] 覆盖缺失自愈完成, 返回码 3 冷启动重跑以补发")
        return 3
    log("[heal] 覆盖缺失自愈未通过, 继续按本次发现结果发送")
    return 0


def _verify_send_phase(p2, cfg, account, state: dict, pending: list[str]) -> int:
    """发送链路门禁: 对全部待续好友逐一真实发送并确认。返回退出码。"""
    name = account["name"]
    if not pending:
        log("[verify-send] 无待续好友, 发送链路验证通过(无可验证对象)")
        return 0
    log(
        f"[verify-send] 对 {len(pending)} 个待续好友逐一真实发送验证 (账号 {name}): {', '.join(pending)}"
    )
    _heartbeat("verify_send_start", name)
    all_sent = True
    for fname in pending:
        fb = fctx = fpage = None
        status, sent = "fail", False
        try:
            fb, fctx, fpage = open_session(p2, cfg, account)
            status, sent = process_friend(fpage, account, fname, dry_run=False)
            save_session_state(fctx, account, tag=" (verify-send)")
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            log(f"[verify-send] 『{fname}』验证发送异常: {exc}")
        finally:
            if fpage is not None:
                free_page_memory(fpage)
            close_session(fb, fctx)
            memory_report(f"verify-send-{fname}")
        if sent:
            if fname not in state["sent"]:
                state["sent"].append(fname)
                write_state(account, state)
            log(f"[verify-send] 『{fname}』发送成功并确认")
        else:
            all_sent = False
            log(f"[verify-send] 『{fname}』发送未确认成功 (status={status}), 门禁失败")
    if all_sent:
        log("[verify-send] 全部待续好友发送成功并确认, 门禁通过")
        return 0
    return 1


def _send_phase(p2, cfg, account, state: dict, pending: list[str], dry_run: bool):
    """发送阶段: 逐好友发送, 含瞬时故障重试/本地恢复/渲染挂死处理。

    返回 (ok_count, fail_count, failed_names, skipped_names, login_halted)。

    `skipped_names`: 本地策略判定"不值得再试"的失败好友(未知错误 / 登录类) ——
    不进入补扫。抄自同类无人值守项目的 continue_on_failure: 一个救不回的好友
    不该继续吃掉后面好友的时间。
    `login_halted`: True = 会话登录态失效导致整段中断, 本账号今晚不可能再成功。
    """
    shared_browser = None
    reuse = cfg.get("browser_reuse", DEFAULT_BROWSER_REUSE)
    recycle_mb = cfg.get("browser_recycle_mem_mb", DEFAULT_BROWSER_RECYCLE_MB)
    ok_count = fail_count = 0
    halted = False
    failed_names: list[str] = []
    skipped_names: list[str] = []
    _t0 = time.monotonic()
    log(f"[debug] 开始发送阶段, pending={pending}, state.sent={state['sent']}")
    # 性能(2026-11-01): 旧实现每个好友都新建 context + 整页加载 /chat
    # (板端实测约 30s/好友, 9 个好友就是 4-5 分钟)。会话列表本身靠 WS 实时
    # 更新, 同一账号内没有重开的必要; 但也不能无限复用(DOM 会增长), 因此
    # 上限默认 3 个好友共用一个页面, 且只有「干净成功」才保留。
    # 出问题不用改代码: SPARK_REUSE_PAGE=0 关闭, SPARK_PAGE_REUSE_MAX=N 调上限。
    _page_reuse = os.environ.get("SPARK_REUSE_PAGE", "1") != "0"
    try:
        _page_reuse_max = max(1, int(os.environ.get("SPARK_PAGE_REUSE_MAX", "3")))
    except ValueError:
        _page_reuse_max = 3
    cached = None  # (browser, ctx, page): 跨好友复用的会话页面
    page_uses = 0
    for idx, fname in enumerate(pending):
        if idx and not dry_run:
            gap = random.uniform(*_send_gap_range())
            log(f"[pace] 好友间随机停顿 {gap:.1f}s(拟人化节奏)")
            time.sleep(gap)
        log(f"[debug] 处理好友: {fname}, 已发送={state['sent']}")
        if fname in state.get("sent", []):
            log(f"【{fname}】已在 state.json(可能由 verify-send 门禁发送), 跳过")
            ok_count += 1
            continue
        retried = False
        ai_healed = False
        reason = ""  # 最后一次失败的摘要, 供本地策略分类
        while True:
            fb = fctx = fpage = None
            keep_page = False
            try:
                global _IDENTITY_STRICT
                if (
                    _page_reuse
                    and reuse
                    and cached is not None
                    and not retried
                    and not ai_healed
                    and _page_usable(cached[2])
                ):
                    fb, fctx, fpage = cached
                    _IDENTITY_STRICT = True
                    log(
                        f"[perf] 复用上一个好友的会话页面(已用 {page_uses}/{_page_reuse_max})"
                    )
                else:
                    # 评审指出的真泄漏: 保留页被丢弃时必须关闭它的 context,
                    # 否则每丢弃一次就漏一个 context(页面+WS+DOM) 在 1.5G 板子上累积。
                    if cached is not None:
                        log("[perf] 保留页不再复用, 先关闭它的会话上下文")
                        try:
                            close_session(None if reuse else cached[0], cached[1])
                        except Exception as exc:
                            log(f"[perf] 关闭保留页失败(忽略): {exc}")
                        cached = None
                    _IDENTITY_STRICT = False
                    fb, fctx, fpage = open_session(
                        p2, cfg, account, browser=shared_browser
                    )
                    page_uses = 0
                if reuse and shared_browser is None:
                    shared_browser = fb
                status, sent = process_friend(fpage, account, fname, dry_run)
                save_session_state(fctx, account, tag=" (发送后)")
                log(f"【{fname}】会话已刷新保存")
                # 只有「确认发送成功的干净一轮」才把页面留给下一个好友;
                # 任何失败/未确认都丢掉重建, 免得把不确定状态传下去。
                if (
                    _page_reuse
                    and reuse
                    and status == "ok"
                    and sent
                    and not fpage.is_closed()
                    and page_uses + 1 < _page_reuse_max
                ):
                    keep_page = True
                    page_uses += 1
                    cached = (fb, fctx, fpage)
                    log(
                        f"[perf] 保留本页供下一个好友复用(已用 {page_uses}/{_page_reuse_max})"
                    )
                else:
                    cached = None
                if (
                    status == "fail"
                    and not sent
                    and not ai_healed
                    and _heal_after_send_failure(fname, ai_healed, fpage)
                ):
                    ai_healed = True
                    log(f"【{fname}】本地恢复策略建议重试, 重启浏览器重试")
                    continue
            except KeyboardInterrupt:
                raise
            except LoginRequiredError as exc:
                log(f"【{fname}】登录态失效且无法自动登录, 终止本账号剩余好友: {exc}")
                status, sent = "logged_out", False
                break
            except Exception as exc:
                log(f"【{fname}】处理异常: {exc}")
                reason = str(exc)
                is_hang = isinstance(exc, RenderHangDetected)
                is_critical = isinstance(
                    exc, _HEALABLE_ERRORS
                ) or "Target closed" in str(exc)
                if (is_critical or is_hang) and not retried:
                    log(
                        f"【{fname}】渲染挂死, 重开会话重试(不启动 AI)"
                        if is_hang
                        else f"【{fname}】瞬时故障, 先重启浏览器重试(不启动 AI)"
                    )
                    if reuse:
                        _discard_shared_browser(shared_browser, fctx)
                        fctx = None
                        shared_browser = None
                    retried = True
                    continue
                shot = ""
                if fpage is not None:
                    shot = dump_page(fpage)
                if (
                    is_critical
                    and not ai_healed
                    and self_heal(
                        "process_friend",
                        str(exc),
                        f"friend={fname}",
                        screenshot_path=shot,
                    )
                ):
                    log(f"【{fname}】本地恢复完成, 重启浏览器重试")
                    if reuse:
                        _discard_shared_browser(shared_browser, fctx)
                        fctx = None
                        shared_browser = None
                    ai_healed = True
                    if fname in state.get("sent", []):
                        log(f"【{fname}】自愈门禁 verify-send 已发送, 跳过重发")
                        status, sent = "ok", True
                        break
                    continue
                status, sent = "fail", False
                if reuse and is_critical:
                    log(f"【{fname}】复用模式: 丢弃故障共享浏览器, 下个好友重建")
                    _discard_shared_browser(shared_browser, fctx)
                    fctx = None
                    shared_browser = None
            finally:
                if fpage is not None:
                    free_page_memory(fpage)
                if not keep_page:
                    close_session(None if reuse else fb, fctx)
                memory_report(f"friend-{fname}")
                if reuse and shared_browser is not None:
                    shared_browser, _recycled = _recycle_if_low_memory(
                        shared_browser, recycle_mb
                    )
                    if _recycled:
                        cached = None
                        page_uses = 0
            break

        if status == "logged_out":
            log("[中断] 登录态失效, 停止处理剩余好友")
            halted = True
            break
        if sent:
            if fname not in state["sent"]:
                state["sent"].append(fname)
                write_state(account, state)
            log(f"【{fname}】发送成功, state.json 已更新")
            ok_count += 1
        elif status == "ok":
            ok_count += 1
            log(f"【{fname}】定位链路通过(未真正发送)")
        else:
            fail_count += 1
            if not reason:
                reason = (
                    f"发送未确认: {fname}" if status == "fail" else f"处理失败: {fname}"
                )
            if _worth_local_retry(reason):
                failed_names.append(fname)
                log(f"【{fname}】处理失败(本地策略: 重试), 转入补扫")
            else:
                skipped_names.append(fname)
                log(f"【{fname}】处理失败(本地策略: 跳过, 不再补扫, 继续下一个好友)")

    if halted:
        # 登录态失效时**当前这位**连同其后的好友一个都没发, 却既不 +fail 也不留名:
        # 收尾行会印「发送 3 / 失败 0」, health_check 判该账号已收尾且零失败 →
        # 整晚静默漏发没人知道(无人值守验收标准下这是最坏的一种谎)。
        for fname in pending[idx:]:
            fail_count += 1
            skipped_names.append(fname)
            log(f"【{fname}】处理失败(登录态失效, 本轮未处理)")

    cached = None
    if reuse and shared_browser is not None:
        close_session(shared_browser, None)
        memory_report("session-closed")

    log(f"[timing] 发送阶段耗时 {_fmt_duration(time.monotonic() - _t0)}")
    return ok_count, fail_count, failed_names, skipped_names, halted


def _budget_left_min() -> float | None:
    """距整晚硬截止还剩多少分钟; 没有 SPARK_DEADLINE_EPOCH 时返回 None(不限)。

    run_cron.sh 每轮把「今天 00:00 + _HARD_STOP_MIN」的 epoch 导出, 于是这里能知道
    补扫还有多少余量。不知道余量就不限, 保持手动/开发调用时的旧行为。
    """
    raw = os.environ.get("SPARK_DEADLINE_EPOCH", "").strip()
    if not raw:
        return None
    try:
        return (int(raw) - time.time()) / 60.0
    except ValueError:
        return None


# 补扫最低余量(分钟): 一个人补扫要重开整页(板端实测 37.6-40.1s)+ 走完整发送链路,
# 少于这个数就不该再起 —— 烧穿 timeout 会让 run_cron 拿到 rc=124 而**直接退出不重试**,
# 结果比「补扫没做」更差(2026-10-04 事故同型: 5 人漏发)。
_RETRY_MIN_BUDGET_MIN = 30.0


def _retry_phase(p2, cfg, account, state: dict, failed_names: list[str]):
    """补扫阶段: 对首轮失败好友换新会话重试。返回 (retry_ok, retry_fail)。"""
    retry_ok = retry_fail = 0
    if not failed_names:
        return 0, 0
    _t0 = time.monotonic()
    _heartbeat("retry_start", account.get("name", "?"))
    log(
        f"[补扫] 首轮失败 {len(failed_names)} 个({', '.join(failed_names)}), 换新会话重试"
    )
    for fname in failed_names:
        if fname in state.get("sent", []):
            log(f"【{fname}】已在 state.json, 补扫跳过")
            retry_ok += 1
            continue
        fb = fctx = fpage = None
        status, sent = "fail", False
        try:
            fb, fctx, fpage = open_session(p2, cfg, account)
            status, sent = process_friend(fpage, account, fname, dry_run=False)
            save_session_state(fctx, account, tag=" (补扫)")
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            log(f"【{fname}】补扫异常: {exc}")
            sent = False
        finally:
            if fpage is not None:
                free_page_memory(fpage)
            close_session(fb, fctx)
            memory_report(f"retry-{fname}")
        if sent:
            if fname not in state["sent"]:
                state["sent"].append(fname)
                write_state(account, state)
            retry_ok += 1
            log(f"【{fname}】补扫发送成功, state.json 已更新")
        else:
            retry_fail += 1
            log(f"【{fname}】补扫仍失败 (status={status})")
    log(f"[timing] 补扫阶段耗时 {_fmt_duration(time.monotonic() - _t0)}")
    return retry_ok, retry_fail


def process_account(
    p,
    account: dict,
    cfg: dict,
    now,
    start: str,
    end: str,
    force: bool,
    dry_run: bool,
    verify_send: bool,
) -> int:
    """处理单个账号的全流程(发现→发送); 返回退出码。"""
    name = account["name"]
    state = load_state(account)
    today = now.strftime("%Y-%m-%d")
    if state.get("date") != today:
        state = {"date": today, "sent": []}
        write_state(account, state)

    log(f"==================== 账号: {name} ====================")
    _t0_account = time.monotonic()
    with sync_playwright() as p2:
        # ---- 阶段1: 发现会话(含渲染挂死重开重试) ----
        try:
            pending, rc = _discover_with_hang_retry(
                p2, cfg, account, state, today, force, dry_run
            )
        except KeyboardInterrupt:
            log("用户中断 (Ctrl+C)")
            sys.exit(130)
        if rc != 0:
            return rc

        if not pending:
            rc = _coverage_gate(name, pending, state)
            if rc != 0:
                return rc
            log(f"今日({today})没有需要续火花的好友, 账号 {name} 结束")
            # 「今日无待续」也必须落一行历史。过去这条路径直接 return, 于是:
            # ①昨日漏发要到第二晚才被比对到(空晚在 streak_history 里根本不存在);
            # ②选择器/火花图标改版导致「全员看起来没火花」与「真的都没火花」在历史
            #   里长得一模一样 —— 无人值守一个月, 这正是最难发现的那种静默故障。
            streak_history.save_daily_record(
                account=name, discovered=[], sent=state.get("sent", []), failed=[]
            )
            streak_history.trigger_if_gap_detected(
                account=name,
                self_heal_fn=lambda et, em, extra="": self_heal(et, em, extra),
            )
            return 0

        if not force and not in_window(now, start, end):
            log(
                f"【窗口外】发现 {len(pending)} 个好友待续, 但当前不在发送窗口({start}-{end}), 跳过发送"
            )
            for fname in pending:
                log(f"  - {fname}")
            return 0

        # ---- P5: 发现覆盖对照 ----
        rc = _coverage_gate(name, pending, state)
        if rc != 0:
            return rc

        # ---- 发送链路门禁 (--verify-send) ----
        if verify_send:
            return _verify_send_phase(p2, cfg, account, state, pending)

        # ---- 阶段2: 发送会话 ----
        ok_count, fail_count, failed_names, skipped_names, login_halted = _send_phase(
            p2, cfg, account, state, pending, dry_run
        )

        # ---- 阶段3: 失败补扫(仅本地策略判定"值得重试"的) ----
        if skipped_names:
            log(
                f"[skip] 本地策略跳过 {len(skipped_names)} 个好友"
                f"(不再补扫, 不中断本账号): {', '.join(skipped_names)}"
            )
        if failed_names and not dry_run:
            _left_min = _budget_left_min()
            if _left_min is not None and _left_min < _RETRY_MIN_BUDGET_MIN:
                log(
                    f"[补扫] 距硬截止只剩 {_left_min:.0f} 分钟(<{_RETRY_MIN_BUDGET_MIN:.0f}), "
                    f"放弃补扫 {len(failed_names)} 个({chr(44).join(failed_names)}); "
                    f"硬跑会烧穿 timeout 让整晚 rc=124 且不重试, 比不补扫更糟"
                )
                retry_ok, retry_fail = 0, 0
            else:
                retry_ok, retry_fail = _retry_phase(
                    p2, cfg, account, state, failed_names
                )
            ok_count += retry_ok
            fail_count = fail_count - retry_ok + retry_fail
            failed_names = [n for n in failed_names if n not in state["sent"]]

        log(
            f"==== 账号 {name} 完成: 发送 {ok_count} 个 / 失败 {fail_count} 个"
            f", 总耗时 {_fmt_duration(time.monotonic() - _t0_account)} ===="
        )
        _heartbeat("account_done", name)

        streak_history.save_daily_record(
            account=name,
            discovered=pending,
            sent=state["sent"],
            failed=[*failed_names, *skipped_names],
        )
        streak_history.trigger_if_gap_detected(
            account=name,
            self_heal_fn=lambda et, em, extra="": self_heal(et, em, extra),
        )
    # 登录态失效而中断: 本账号今晚不可能再成功, 给 rc=2(未登录, cron 不重试),
    # 但这是**账号级**返回值 —— run() 会记录它并继续跑后面的账号。
    return 2 if login_halted else 0


def _merge_rc(cur: int, new: int) -> int:
    """账号级退出码合并: 取「最该让 cron 据此行动」的那个, 而不是最后一个。

    3(自愈后仍需冷启动重跑) > 1(一般异常, 可重试一次) > 2(未登录, 重试无益)。
    若按「最后一个赢」, 前面的账号可重试失败会被后面一个掉线的账号盖成 rc=2,
    于是 run_cron 放弃重试, 那位好友今晚就是真漏发。未知退出码一律优先(宁可
    多报, 也不把没见过的故障降级成 0)。
    """
    if not new:
        return cur
    if not cur:
        return new
    rank = {3: 3, 1: 2, 2: 1}
    return new if rank.get(new, 9) >= rank.get(cur, 9) else cur


def run() -> None:
    parser = argparse.ArgumentParser(description="抖音网页版自动续火花(多账号)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="演练定位链路, 不真正发送")
    mode.add_argument(
        "--verify-send",
        action="store_true",
        help="发送链路门禁: 窗口内对全部待续好友逐一真实发送并确认(自愈验证用)",
    )
    parser.add_argument(
        "--no-jitter", action="store_true", help="跳过随机抖动(自动化验证用)"
    )
    parser.add_argument(
        "--force", action="store_true", help="忽略发送窗口与今日幂等限制"
    )
    args = parser.parse_args()

    cfg = load_config()
    # 2026-11-01: 启动即把档位打进日志。以前「这台机器在跑哪一档」只能翻 config.json
    # 猜, 而档位同时决定浏览器资源上限与全流程等待节奏, 出问题第一个要看的就是它。
    log(f"[perf] {perf.describe()}")
    _mismatch = perf.profile_mismatch(cfg.get("perf_profile"))
    if _mismatch:
        log(f"[perf][warn] {_mismatch}")
    _check_clock_sanity()
    now = datetime.now()
    win = cfg.get("send_window") or {}
    start = win.get("start", "23:30")
    end = win.get("end", "00:30")

    in_win = args.force or in_window(now, start, end)
    if args.force:
        log("[force] 已忽略发送窗口/幂等限制")
    elif not in_win and not args.dry_run:
        log(f"【不在发送窗口 {start}-{end}(当前 {now:%H:%M})，直接退出】")
        return
    elif not in_win:
        log(f"【演练模式: 不在发送窗口 {start}-{end}(当前 {now:%H:%M})，仅定位不发送】")

    accounts = cfg.get("accounts") or []
    if not accounts:
        log("【config.json 中未配置 accounts, 无可处理的账号】")
        return

    if not _acquire_pid_lock():
        # 获锁失败必须给**非 0** 退出码: 裸 return 会让 cron 与自愈验证都看到 rc=0,
        # 于是"锁被挂死实例长期持有"这种真实故障在监控面完全静默(天天"成功")。
        # rc=4 = 本次跳过(已有实例持锁); run_cron.sh 对其单独分支, 不重试、不计成功。
        log("[skip] 未获得 PID 锁(另一实例持锁), 以 rc=4 退出")
        sys.exit(4)
    global _TASK_ID, _HB_SEQ
    _TASK_ID = events.start_task()
    _HB_SEQ = 0
    _heartbeat("run_start")
    events.emit("task", "start", "ok", extra={"argv": sys.argv[1:]})
    try:
        rng = random.Random()
        if args.force or args.no_jitter:
            log("[jitter] 跳过随机抖动 (--force/--no-jitter)")
        else:
            jitter_sec = rng.randint(0, _START_JITTER_MAX)
            if jitter_sec > 0:
                log(f"[jitter] 随机延迟 {jitter_sec // 60}分{jitter_sec % 60}秒")
                time.sleep(jitter_sec)
                # 抖动后重取时间: 跨午夜时 today/窗口判定不能用抖动前的旧值
                now = datetime.now()
                _heartbeat("jitter_done")

        # 逐账号处理
        _t0_run = time.monotonic()
        worst_rc = 0
        for idx, account in enumerate(accounts):
            _heartbeat(f"account_start:{account.get('name', idx)}")
            if (
                account.get("password")
                or account.get("account")
                or has_login_state(account)
            ):
                rc = process_account(
                    None,
                    account,
                    cfg,
                    now,
                    start,
                    end,
                    args.force,
                    args.dry_run,
                    args.verify_send,
                )
                # verify-send 为一次性门禁: 只对第一个账号执行后即退出
                if args.verify_send:
                    sys.exit(rc)
                if rc != 0:
                    # 单个账号失败不再掐断整轮(2026-10-04 评审): 否则 main 一挂,
                    # 后面的副账号当晚根本不会跑。记下最差 rc, 继续处理后续账号。
                    log(
                        f"[账号 {account.get('name', idx)}] 返回码 {rc}, "
                        "记录并继续处理后续账号"
                    )
                    worst_rc = _merge_rc(worst_rc, rc)
            else:
                log(
                    f"【账号 {account.get('name', idx)} 未配置 account/password 且无登录态, 跳过】"
                )
        log(f"[timing] 本次运行总耗时 {_fmt_duration(time.monotonic() - _t0_run)}")
        # 任务收尾事件(2026-11-01 会话50): 记下可用内存最低值——以前没人记,
        # 周报只能写"内存未采集"。available_mb() 读不到时返回 None, 事件里如实留空。
        _mem = available_mb()
        events.emit(
            "task",
            "end",
            "failed" if worst_rc else "ok",
            extra={
                "mem_available_mb": round(_mem, 1) if _mem is not None else None,
                "rc": worst_rc,
            },
        )
        if worst_rc:
            sys.exit(worst_rc)
    finally:
        _release_pid_lock()
