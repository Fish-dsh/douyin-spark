"""元素查找通用工具：选择器候选、聊天区域等待、会话列表等待。"""

from __future__ import annotations

from playwright.sync_api import Page

from logutil import log

from .debug import dump_page

try:
    from . import perf
except ImportError:  # 以顶层模块方式加载时(src/ 在 sys.path 上)
    import perf  # type: ignore[no-redef]

# 性能(2026-11-01): 判断「已经在聊天页且列表有内容」, 用于跳过一次整页重载。
_JS_CHAT_READY = """() => {
    if (!location.pathname.includes('/chat')) return false;
    let n = 0;
    for (const it of document.querySelectorAll(
        '[class*="conversation"], [class*="Conversation"], [class*="semi-list-item"]')) {
        if (((it.innerText || '').trim())) n++;
    }
    return n >= 3;
}"""


def chat_page_ready(page: Page) -> bool:
    """页面已在 /chat 且会话列表已渲染出 ≥3 条有文字的会话。

    只用于「要不要重新 goto」这一个决策; 读不到一律返回 False(退回重载, 保守)。
    """
    try:
        return bool(page.evaluate(_JS_CHAT_READY))
    except Exception:
        return False


CHAT_UI_SELECTORS = [
    "[data-e2e='chat']",
    "[data-e2e*='chat']",
    "[class*='chat']",
    "div[class*='list']",
    "[class*='message']",
    "a[href*='/chat']",
]


def find(page: Page, selectors, wait_ms: int | None = None):
    """依次 wait_for_selector 每个候选; 命中打印日志并返回 locator, 全败返回 None。

    wait_ms 来自 perf 档位(板端 1500ms, 桌面档 800ms), 显式传参时以传参为准。
    """
    if wait_ms is None:
        wait_ms = perf.ms("selector_wait_ms")
    if isinstance(selectors, str):
        selectors = [selectors]
    # 性能(2026-11-01): 旧实现逐个 wait_for_selector, 全不命中时 6 个候选 × 1.5s
    # = 9s 纯等待, 加上随后的 dump_page 截图, 每个好友白烧 ~15s(板端实测)。
    # 先做一次零等待探测, 命中即刻返回; 没命中再走原来的等待循环, 上限不变。
    try:
        for sel in selectors:
            if page.query_selector(sel) is not None:
                log(f"命中选择器(即时): {sel}")
                return page.locator(sel).first
    except Exception:
        pass
    for sel in selectors:
        try:
            handle = page.wait_for_selector(sel, state="attached", timeout=wait_ms)
            if handle is not None:
                log(f"命中选择器: {sel}")
                return page.locator(sel).first
        except Exception:
            continue
    log(f"[debug] 以下候选选择器均未命中: {selectors}")
    dump_page(page)
    return None


def wait_chat_ui(page: Page) -> bool:
    hit = find(page, CHAT_UI_SELECTORS)
    if hit is not None:
        log("主聊天区域已就绪")
        return True
    try:
        page.wait_for_url(lambda u: "/chat" in u, timeout=perf.ms("url_stable_ms"))
        log("URL 已稳定在 /chat")
        return True
    except Exception:
        log("[debug] 等待聊天区域/URL 稳定超时")
        return False


def page_render_state(page: Page) -> str:
    """检测页面渲染状态: 返回 'blank'(渲染挂死) 或 'content'(有实质内容)。

    2026-09-30 事故: headless 下渲染挂死, body 全空, 发现策略全部未命中,
    被误判为「选择器失效」触发自愈(根因错判, 白烧 35 分钟 API)。
    body 全空 = 瞬时渲染问题, 重开会话即可; 有内容但列表超时才疑似选择器失效。
    阈值 50 字符: 正常聊天页有导航/标题/会话文本, 远超此值; 挂死页 body 为空。
    """
    js = "() => document.body ? (document.body.innerText || '').trim().length : 0"
    try:
        n = int(page.evaluate(js) or 0)
    except Exception:
        return "blank"
    return "content" if n >= 50 else "blank"


def wait_chat_list(
    page: Page, tries: int | None = None, interval_ms: int | None = None
) -> bool:
    """轮询等待左侧会话列表「真正渲染出内容」。

    旧判定 querySelectorAll('[class*=conversation]').length>0 会命中空骨架容器,
    板子 Xvfb 软渲染下列表 ~75s 才填充, 44s 即误判就绪导致发现阶段空跑
    (2026-09-24 实测)。新判定要求至少一个 conversation 元素 innerText 非空。
    默认上限取 perf.list_budget_ms(三档统一 150s): 覆盖板子软渲染最慢 ~75s 的实测,
    并留一倍余量(2026-09-30 事故: 旧默认 36s 不够, 导致 0 火花)。粒度取
    perf.list_step_ms(板端 250ms)。

    列表中无会话(新账号)时也会超时返回 False, 不抛错。
    """
    js = (
        "() => Array.from(document.querySelectorAll('[class*=conversation]'))"
        ".filter(el => (el.innerText || '').trim().length > 0).length"
    )
    # 性能(2026-11-01): 原实现固定 5s 一档, 每好友至少白等一个 5s 台阶(板端实测)。
    # 判定条件与**总预算**完全不变, 只把粒度改成 250ms、首拍立刻查。
    # 预算与轮询粒度都跟档位走: 板端 150s/250ms(实测最慢 75s), 桌面档 60s/100ms。
    if tries is not None and interval_ms is not None:
        budget_ms = tries * interval_ms
    else:
        budget_ms = perf.ms("list_budget_ms")
    step_ms = perf.ms("list_step_ms")
    waited = 0
    i = 0
    while waited < budget_ms:
        i += 1
        try:
            count = page.evaluate(js)
        except Exception:
            count = 0
        if count and count > 0:
            log(f"[wait] 会话列表已渲染出 {count} 个会话项 (第 {i} 轮)")
            return True
        page.wait_for_timeout(step_ms)
        waited += step_ms
    log(
        f"[ERROR] 等待会话列表内容超时({budget_ms // 1000}s): "
        "会话列表始终未渲染出内容。可能原因: ①页面渲染挂死(body 空, 应重开会话) "
        "②选择器失效(页面有内容但结构变了) ③确无任何会话。"
        "请结合 page_render_state 判别, 勿直接触发恢复策略"
    )
    return False
