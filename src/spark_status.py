"""火花状态检测模块.

纯 DOM/JS 检测，不依赖识图能力。

策略 (按优先级):
  1. CSS 类名选择器 (conversationConversationItem + commonStreak*)
  2. 文本特征兜底 (火焰图标 src 关键词 + 数字徽标 + 名字定位)
  3. 通用火焰图标扫描 (任何 img/svg src 含 flame/fire/streak 等)
  4. 结构性兜底 (不依赖任何混淆类名: 火焰 src 关键词 + 头像 img[alt] + 祖先遍历)

注意: 策略 1-3 全部锚定 conversationConversationItemwrapper 等混淆类名,
抖音一次类名重命名会同时打掉三条兜底; 策略 4 是类名失效时唯一存活的发现路径。

对外接口:
    discover_friends_with_spark(page) -> list[dict]
"""

from __future__ import annotations

import time

from logutil import log

try:
    from . import perf
except ImportError:  # 以顶层模块方式加载时(src/ 在 sys.path 上)
    import perf  # type: ignore[no-redef]

try:
    from . import vision
except ImportError:  # 以顶层模块方式加载时(src/ 在 sys.path 上)
    import vision  # type: ignore[no-redef]


class SparkDiscoveryError(Exception):
    """火花发现的四条检测策略( CSS class / 文字特征 / 图标扫描 / 结构兜底 )全部未命中。

    用于向 spark.py 传递「疑似抖音页面改版导致选择器失效」的信号,
    从而触发本地恢复策略/告警( self_heal )。
    """


_JS_DISCOVER_BY_CLASS = """
() => {
    const out = [];
    const seen = new Set();
    const items = document.querySelectorAll('[class*="conversationConversationItemwrapper"]');
    for (const el of items) {
        const cls = (el.className || '').toString();
        if (!cls.includes('conversationConversationItem')) continue;

        const titleWrapper = el.querySelector('[class*="conversationConversationItemtitleWrapper"]');
        let name = '';
        if (titleWrapper) {
            const children = titleWrapper.children;
            for (let i = 0; i < children.length; i++) {
                const childCls = (children[i].className || '').toString();
                if (childCls.includes('conversationConversationItemtitle')
                    && !childCls.includes('Wrapper')) {
                    name = children[i].innerText.trim().split('\\n')[0].trim();
                    break;
                }
            }
        }
        if (!name) {
            const titleEl = el.querySelector('[class*="conversationConversationItemtitle"]');
            if (titleEl) name = titleEl.innerText.trim().split('\\n')[0].trim();
        }

        const streakEl = el.querySelector('[class*="commonStreakstreakContainer"]');
        const iconEl = el.querySelector('[class*="commonStreakicon"]');
        const numEl = el.querySelector('[class*="commonStreaknormalText"]');
        const isPinned = cls.includes('isStickOnTop');

        if (!name || seen.has(name)) continue;
        seen.add(name);

        const hasStreak = !!streakEl;
        const iconSrc = iconEl ? (iconEl.src || '').split('/').pop() : '';
        const streakNum = numEl ? parseInt(numEl.innerText.trim(), 10) || 0 : 0;

        out.push({name, hasStreak, iconSrc, streakNum, method: 'class', isPinned});
    }
    return out;
}
"""

_JS_DISCOVER_BY_TEXT = """
() => {
    const out = [];
    const seen = new Set();
    const convItems = document.querySelectorAll(
        '[class*="conversationConversationItemwrapper"]'
    );
    for (const el of convItems) {
        const titleWrapper = el.querySelector('[class*="conversationConversationItemtitleWrapper"]');
        let name = '';
        if (titleWrapper) {
            const children = titleWrapper.children;
            for (let i = 0; i < children.length; i++) {
                const childCls = (children[i].className || '').toString();
                if (childCls.includes('conversationConversationItemtitle')
                    && !childCls.includes('Wrapper')) {
                    name = children[i].innerText.trim().split('\\n')[0].trim();
                    break;
                }
            }
        }
        if (!name) {
            const titleEl = el.querySelector('[class*="conversationConversationItemtitle"]');
            if (titleEl) name = titleEl.innerText.trim().split('\\n')[0].trim();
        }
        if (!name || seen.has(name)) continue;
        seen.add(name);

        const iconEl = el.querySelector('[class*="commonStreakicon"]');
        let iconSrc = '';
        let hasStreak = false;
        if (iconEl) {
            const src = (iconEl.src || '').toLowerCase();
            if (src.includes('flame') || src.includes('fire') || src.includes('streak') || src.includes('spark') || src.includes('hot') || src.includes('light') || src.includes('normal') || src.includes('blue')) {
                iconSrc = src.split('/').pop();
                hasStreak = true;
            }
        }

        let streakNum = 0;
        if (hasStreak) {
            const numEl = el.querySelector('[class*="commonStreaknormalText"]');
            if (numEl) streakNum = parseInt(numEl.innerText.trim(), 10) || 0;
        }

        const isPinned = (el.className || '').toString().includes('isStickOnTop');

        if (hasStreak || isPinned) {
            out.push({name, hasStreak, iconSrc, streakNum, method: 'text', isPinned});
        }
    }
    return out;
}
"""

_JS_DISCOVER_BY_ICON = """
() => {
    const out = [];
    const seen = new Set();
    const allImgs = document.querySelectorAll('[class*="commonStreakicon"]');
    for (const img of allImgs) {
        const src = (img.src || img.getAttribute('src') || '').toLowerCase();
        if (!(src.includes('flame') || src.includes('fire') || src.includes('streak') || src.includes('spark') || src.includes('hot') || src.includes('light') || src.includes('normal') || src.includes('blue'))) continue;

        let row = img.closest('[class*="conversationConversationItemwrapper"]');
        if (!row) continue;

        const titleWrapper = row.querySelector('[class*="conversationConversationItemtitleWrapper"]');
        let name = '';
        if (titleWrapper) {
            const children = titleWrapper.children;
            for (let i = 0; i < children.length; i++) {
                const childCls = (children[i].className || '').toString();
                if (childCls.includes('conversationConversationItemtitle')
                    && !childCls.includes('Wrapper')) {
                    name = children[i].innerText.trim().split('\\n')[0].trim();
                    break;
                }
            }
        }
        if (!name) continue;
        if (seen.has(name)) continue;
        seen.add(name);

        const numEl = row.querySelector('[class*="commonStreaknormalText"]');
        const streakNum = numEl ? parseInt(numEl.innerText.trim(), 10) || 0 : 0;
        const isPinned = (row.className || '').toString().includes('isStickOnTop');

        out.push({name, hasStreak: true, iconSrc: src.split('/').pop(), streakNum, method: 'icon', isPinned});
    }
    return out;
}
"""

_JS_DISCOVER_GENERIC = """
() => {
    // 结构性兜底策略: 不依赖任何混淆类名, 只锚定两类稳定信号——
    //   a) 火焰图标的资源 URL 含 flame/fire/streak/spark 关键词
    //   b) 会话行头像 img 的 alt 属性即好友昵称
    // 从火焰图标向上遍历祖先找到包含头像的『行』, 提取 name/streakNum/isPinned。
    // 名字取不到时补充 React fiber 遍历: 组件 props 里的昵称不依赖类名与 alt。
    const KEYWORDS = ['flame', 'fire', 'streak', 'spark'];
    const out = [];
    const nameFromFiber = (row) => {
        const keys = Object.keys(row);
        let fk = null;
        for (const k of keys) {
            if (k.startsWith('__reactFiber$') || k.startsWith('__reactInternalInstance$')) { fk = k; break; }
        }
        if (!fk) return '';
        let f = row[fk];
        for (let i = 0; i < 12 && f; i++) {
            const p = f.memoizedProps || {};
            for (const key of ['nickname', 'nickName', 'title', 'remark', 'name']) {
                const v = p[key];
                if (typeof v === 'string' && v.trim() && v.length <= 30
                    && !/^[0-9\\s]+$/.test(v) && !v.includes('火花')
                    && !/^(头像|avatar|图片)$/i.test(v)) return v.trim();
            }
            f = f.return;
        }
        return '';
    };
    const urlOf = (el) =>
        ((el.src && typeof el.src === 'string' ? el.src : '') ||
         el.getAttribute('src') || el.getAttribute('href') || '').toLowerCase();
    const icons = [];
    for (const el of document.querySelectorAll('img, image, use')) {
        const u = urlOf(el);
        if (u && KEYWORDS.some(k => u.includes(k))) icons.push({el, u});
    }
    for (const item of icons) {
        const icon = item.el;
        const iconUrl = item.u;
        let row = null;
        let fiberRow = null;
        let node = icon.parentElement;
        for (let depth = 0; depth < 8 && node; depth++) {
            if (node.querySelector('img[alt]')) { row = node; break; }
            if (!fiberRow && Object.keys(node).some(k => k.startsWith('__react'))) fiberRow = node;
            node = node.parentElement;
        }
        if (!row) row = fiberRow;
        if (!row) continue;
        let name = '';
        for (const av of row.querySelectorAll('img[alt]')) {
            const a = (av.alt || '').trim();
            if (a && a.length <= 30 && !/^(头像|avatar|图片)$/i.test(a)) { name = a; break; }
        }
        if (!name) name = nameFromFiber(row);
        if (!name) continue;
        let streakNum = 0;
        for (const el of row.querySelectorAll('*')) {
            if (el.children.length) continue;
            const t = (el.textContent || '').trim();
            if (/^[0-9]{1,4}$/.test(t)) { streakNum = parseInt(t, 10); break; }
        }
        const cls = (row.className || '').toString();
        const isPinned = /stick|pinned/i.test(cls);
        out.push({name, hasStreak: true, iconSrc: iconUrl.split('/').pop(),
                  streakNum, method: 'generic', isPinned});
    }
    return out;
}
"""

_JS_SCROLL_LIST = """
() => {
    const candidates = document.querySelectorAll(
        '[class*="conversationList"], [class*="chatList"], [class*="sessionList"], [data-e2e*="chat"]'
    );
    let el = null;
    for (const c of candidates) {
        if (c.scrollHeight > c.clientHeight + 50) { el = c; break; }
    }
    if (!el) {
        for (const c of document.querySelectorAll('div')) {
            const style = window.getComputedStyle(c);
            if ((style.overflow === 'auto' || style.overflow === 'scroll'
                 || style.overflowY === 'auto' || style.overflowY === 'scroll')
                && c.scrollHeight > c.clientHeight + 100) { el = c; break; }
        }
    }
    if (!el) return false;
    // 按约一屏步进滚动(2026-10-07 事故: 原先一次跳到底, 虚拟列表只渲染滚动位置
    // 附近若干行, 列表中段的好友从未进入 DOM, 扫描永远漏人)。
    // 返回 false = 已到底部无法再推进(调用方据此结束滚动循环)。
    const before = el.scrollTop;
    if (before + el.clientHeight >= el.scrollHeight - 5) {
        el.scrollTop = 0;  // 已到底: 归零让下一轮重新从顶开始(循环随即结束)
        return false;
    }
    el.scrollTop = Math.min(before + Math.round(el.clientHeight * 0.9), el.scrollHeight);
    return el.scrollTop > before;
}
"""

_JS_STREAK_COUNT = (
    "() => document.querySelectorAll("
    '\'[class*="commonStreakstreakContainer"], img[src*="flame"], '
    'img[src*="fire"], img[src*="streak"], img[src*="spark"]\').length'
)


def _wait_for_streaks(page, max_wait_ms: int | None = None) -> bool:
    """等待火花图标渲染完成（名单先渲染, 火花图标懒加载, 需轮询等待）。

    返回是否检测到至少一个火花容器；超时返回 False（不阻塞, 交由后续策略兜底）。

    max_wait_ms 缺省取性能档位的 streak_wait_ms —— 不要写成函数默认值, 否则
    默认参数在 import 时求值、档位切换对它无效(2026-11-01 审计发现的老写法)。
    """
    if max_wait_ms is None:
        max_wait_ms = perf.ms("streak_wait_ms")
    deadline = time.monotonic() + max_wait_ms / 1000.0
    while time.monotonic() < deadline:
        try:
            n = int(page.evaluate(_JS_STREAK_COUNT) or 0)
        except Exception:
            n = 0
        if n > 0:
            log(f"[spark] 火花图标已渲染 ({n} 个容器)")
            return True
        page.wait_for_timeout(perf.ms("streak_poll_ms"))
    return False


def _classify_icon(icon_src: str) -> str:
    if not icon_src:
        return "unknown"
    s = icon_src.lower()
    if "gray" in s or "grey" in s:
        return "need"
    if "active" not in s and "fire" not in s and "streak" not in s:
        log(f"[warn] 未识别的火花图标: {icon_src.split('/')[-1]}, 按 active 处理")
    return "active"


_DISCOVERY_STRATEGIES = [
    ("class", _JS_DISCOVER_BY_CLASS),
    ("text", _JS_DISCOVER_BY_TEXT),
    ("icon", _JS_DISCOVER_BY_ICON),
    ("generic", _JS_DISCOVER_GENERIC),
]


def _run_discover(page) -> list[dict]:
    seen: dict[str, dict] = {}
    methods_used: list[str] = []

    for method, js in _DISCOVERY_STRATEGIES:
        try:
            items = page.evaluate(js)
        except Exception:
            continue
        if not items or not isinstance(items, list):
            continue
        methods_used.append(method)
        for item in items:
            if (item.get("hasStreak") or item.get("isPinned")) and item[
                "name"
            ] not in seen:
                seen[item["name"]] = item

    if not methods_used:
        log("[spark] 所有检测策略均未找到火花 (CSS/文本/图标/结构 全部未命中)")
        raise SparkDiscoveryError(
            "火花检测四策略( CSS class / 文字特征 / 图标扫描 / 结构兜底 )全部未命中, "
            "疑似抖音页面改版导致选择器失效"
        )

    if methods_used:
        log(f"[spark] 检测策略: {', '.join(methods_used)}")

    # 注意: 不要在此处告警「无火花标记」。本函数每个滚动 pass 都会被调用, 中间 pass
    # 火花图标尚未渲染时会误报(会话19 遗留①)。改由调用方在汇总后判定, 见
    # discover_friends_with_spark。
    return list(seen.values())


def _vision_discover(page) -> list[dict]:
    """视觉兜底: 四条 DOM 策略全失效时, 用模型粗定位 + DOM 取证找回会话行。

    返回视觉/DOM 共同确认的行(真实昵称由 DOM 回填); 未启用/失败返回 `[]`,
    由调用方决定是否抛出 SparkDiscoveryError(绝不吞掉这个信号)。
    """
    if not vision.vision_enabled():
        log("[spark] 视觉兜底未启用, 跳过")
        return []
    rows = vision.locate_rows(page)
    if not rows:
        log("[spark] 视觉兜底未取得候选行, 回落原策略")
        return []
    log(
        "[spark] 视觉兜底取得 "
        f"{len(rows)} 行: {', '.join(str(r.get('name')) for r in rows)}"
    )
    return rows


def discover_friends_with_spark(page) -> list[dict]:
    all_items: dict[str, dict] = {}
    _wait_for_streaks(page)
    max_scrolls = 20
    used_vision = False

    try:
        items = _run_discover(page)
        for item in items:
            if item.get("hasStreak") or item.get("isPinned"):
                all_items[item["name"]] = item
    except SparkDiscoveryError:
        # 四条 DOM 策略全失效(疑似改版): 先试视觉兜底; 拿到带火花的行才继续,
        # 否则如实抛出 —— 视觉只找到行、行内却无火花时也不能假装"发现 0 个"。
        items = _vision_discover(page)
        for item in items:
            if item.get("hasStreak") or item.get("isPinned"):
                all_items[item["name"]] = item
        if not all_items:
            raise
        used_vision = True
    except Exception as exc:
        log(f"[spark] 初始扫描火花失败: {exc}")
        return []

    # 视觉只覆盖当前视口(且已无类名可复用), 故走视觉时不再滚动补扫
    # 停止条件是「滚到底」而非「本轮无新增火花好友」: 列表中段连续若干排无火花
    # 是常态, 按无新增即停会漏掉更下面待续的好友(2026-10-07 『好友E』漏检)。
    for scroll_pass in range(0 if used_vision else max_scrolls):
        try:
            scrolled = page.evaluate(_JS_SCROLL_LIST)
            if not scrolled:
                break
            page.wait_for_timeout(perf.ms("scroll_settle_ms"))
            new_items = _run_discover(page)
            for item in new_items:
                if (item.get("hasStreak") or item.get("isPinned")) and item[
                    "name"
                ] not in all_items:
                    all_items[item["name"]] = item
            log(
                f"[spark] 滚动第 {scroll_pass + 1} 屏: 收集 {len(new_items)} 行, "
                f"累计火花好友 {len(all_items)} 个"
            )
        except Exception:
            break

    result = []
    for item in all_items.values():
        icon = item.get("iconSrc", "")
        is_pinned = item.get("isPinned", False)
        if is_pinned and not item.get("hasStreak"):
            status = "pinned"
        else:
            status = _classify_icon(icon)
        entry = {
            "name": item["name"],
            "streak_days": item.get("streakNum", 0),
            "status": status,
            "isPinned": is_pinned,
        }
        result.append(entry)
        log(
            f"[spark] 发现: {item['name']} {item.get('streakNum', 0)}天 "
            f"status={status} pinned={is_pinned} icon={icon} method={item.get('method', '?')}"
        )

    # 只有「滚完所有 pass 仍未发现任何火花好友」才算异常信号; 单个 pass 无火花是正常的
    # (图标懒加载)。会话列表完全不存在时 _run_discover 已抛 SparkDiscoveryError。
    if not result:
        log(
            "[spark][warn] 检测到会话列表但无任何火花标记(hasStreak/isPinned 全 False), "
            "疑似选择器退化或确无火花, 请人工留意"
        )

    log(f"[spark] 共发现 {len(result)} 个有火花的好友")
    return result
