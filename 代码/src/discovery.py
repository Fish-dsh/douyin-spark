"""好友发现编排：自动发现待续火花好友、自愈触发、pending 过滤。"""

from __future__ import annotations

from playwright.sync_api import Page

from logutil import log

from .config import CHAT_URL
from .debug import dump_page
from .finder import page_render_state
from .self_heal import self_heal

try:
    from . import perf
except ImportError:  # 以顶层模块方式加载时(src/ 在 sys.path 上)
    import perf  # type: ignore[no-redef]
from .spark_status import (
    SparkDiscoveryError,
    discover_friends_with_spark,
)
from .spark_status import (
    _wait_for_streaks as wait_for_streaks,
)


class HealRetryFailed(Exception):
    """自愈成功但本进程内重试发现仍失败; 调用方应冷启动重跑(退出码 3)。"""


class RenderHangDetected(Exception):
    """页面 body 全空(渲染挂死): 瞬时环境问题, 应重开会话而非触发自愈。

    2026-09-30 事故: headless 渲染挂死 → 发现策略全未命中 → 抛 SparkDiscoveryError
    → 误触发旧 AI 自愈(GLM 429 重试 35 分钟, 根因错判; 该链已于 2026-10-05 删除)。
    本异常用于在三态判别中把「渲染挂死」从「选择器失效」里剥离出来。
    """


def discover_pending_friends(
    page: Page,
    cfg: dict,
    state: dict,
    today: str,
    force: bool,
    dry_run: bool,
) -> list[str]:
    """发现阶段：自动发现有火花且需续的好友；置顶好友不受状态检测限制强制待发送。"""
    log("[auto] 自动发现有火花且需续的好友")

    try:
        discovered = discover_friends_with_spark(page)
    except SparkDiscoveryError as exc:
        # 三态判别: body 全空 = 渲染挂死(瞬时, 重开会话即可);
        # body 有内容但策略全未命中 = 疑似选择器失效(才走本地恢复策略)。
        if page_render_state(page) == "blank":
            raise RenderHangDetected(
                f"发现策略全未命中且页面 body 全空(渲染挂死, 非选择器问题): {exc}"
            ) from exc
        log(
            f"[heal] 火花发现策略全失效(页面有内容, 疑似选择器失效), 触发本地恢复策略: {exc}"
        )
        shot = dump_page(page)
        if self_heal(
            "discover", str(exc), extra="好友发现返回空", screenshot_path=shot
        ):
            discovered = None
            for attempt in (0, 1):
                try:
                    if attempt == 0:
                        log("[heal] 修复成功, reload 重试发现")
                        page.reload(
                            wait_until="domcontentloaded",
                            timeout=perf.ms("page_dom_ms"),
                        )
                    else:
                        log("[heal] reload 重试仍失败, 冷重开页面再试")
                        page.goto(
                            CHAT_URL,
                            wait_until="domcontentloaded",
                            timeout=perf.ms("page_dom_ms"),
                        )
                    page.wait_for_timeout(perf.ms("heal_settle_ms"))
                    wait_for_streaks(page, perf.ms("streak_scan_ms"))
                    discovered = discover_friends_with_spark(page)
                    break
                except Exception:
                    if attempt == 1:
                        raise HealRetryFailed(
                            "自愈成功但本进程重试发现仍失败, 需冷启动重跑"
                        )
            if discovered is None:
                raise SparkDiscoveryError("自愈后重试发现仍未成功")
        else:
            raise

    pending = []
    for d in discovered:
        name = d["name"]
        if not force and name in state["sent"]:
            log(f"【{name}】今日已发送过, 跳过")
            continue
        if d.get("isPinned"):
            log(f"【{name}】置顶好友, 忽略状态检测({d['status']})强制续")
            pending.append(name)
            continue
        if d["status"] == "active":
            log(f"【{name}】火花已续({d['streak_days']}天), 跳过")
            continue
        if d["status"] != "need":
            log(f"【{name}】状态={d['status']}, 非待续火花, 跳过")
            continue
        log(f"【{name}】需要续火花({d['streak_days']}天), 待发送")
        pending.append(name)

    log(f"[auto] 待发送 {len(pending)} 个好友")
    return pending
