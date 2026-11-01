"""浏览器会话生命周期管理：启动、关闭、资源拦截、内存上报。"""

from __future__ import annotations

import os
from urllib.parse import urlparse

from playwright.sync_api import Browser, BrowserContext, Page

from logutil import log

from .config import CHAT_URL, get_account_storage_file
from .debug import dump_page

try:
    from . import perf
except ImportError:  # 以顶层模块方式加载时(src/ 在 sys.path 上)
    import perf  # type: ignore[no-redef]
from .login import (
    _CHROMIUM_PATH,
    auto_login,
    is_logged_in,
    restore_state,
    save_state,
)
from .net_sniff import NetSniffer

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--mute-audio",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-sync",
    "--disable-default-apps",
    "--disable-features=TranslateUI,MediaRouter,InterestFeedContentSuggestions",
    "--no-first-run",
    "--js-flags=--max-old-space-size=256 --max-semi-space-size=32",
    "--renderer-process-limit=1",
    # 2026-11-01: 后台节流三连。板端 headed 跑在 Xvfb 上, 窗口很容易被判
    # "不可见/被遮挡", chromium 随即把定时器与渲染降频 —— 表现正是「表情面板
    # 打开慢 → 按旧布局取坐标 → 点到空白处, 面板被关掉但没发出消息」。
    # 板端近一周「首轮必失败、第二轮重进好友才成功」与这条吻合。
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
]

# 桌面档(perf_profile=high, 2026-11-01): 本机 18 核 / 有显卡(或 WSLg) 时没必要
# 套板子的 1.5G 内存参数 —— 单渲染进程串行 + 256MB JS 堆会把重型 SPA 拖成
# 60s 级等待。去掉这些限制即可, 不是"另一套策略", 只改资源上限。
_DESKTOP_DROP = {
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--renderer-process-limit=1",
    "--js-flags=--max-old-space-size=256 --max-semi-space-size=32",
}
_DESKTOP_JS_FLAGS = "--js-flags=--max-old-space-size=2048 --max-semi-space-size=128"
_DESKTOP_RENDERER_LIMIT = "--renderer-process-limit=4"

# 中间档(perf_profile=mid, 2026-11-01): 4-8 核 / 8G 笔记本, 大概率无独显。
# 只放开「单渲染进程」与 JS 堆这两个纯粹的资源硬顶(GPU 相关开关保持板端口径,
# 因为无独显时软件合成反而是稳的那条路), 其余等待节奏由 src/perf.py 的 mid 列负责。
_MID_DROP = {
    "--renderer-process-limit=1",
    "--js-flags=--max-old-space-size=256 --max-semi-space-size=32",
}
_MID_JS_FLAGS = "--js-flags=--max-old-space-size=1024 --max-semi-space-size=64"
_MID_RENDERER_LIMIT = "--renderer-process-limit=2"


def build_launch_args(cfg: dict, headless: bool) -> list:
    """按 cfg 的 perf_profile 生成 chromium 启动参数。

    low(默认) = 板端档(4 核 / 1.5G): 行为与 2026-10 之前完全一致(headless 时直接
    返回 LAUNCH_ARGS 本体, 保证既有断言的对象同一性不变)。
    mid = 普通笔记本档(4-8 核 / 8G, 无独显): 放开单渲染进程与 256MB JS 堆,
    GPU 相关开关沿用板端口径。
    high = 本机桌面档(>=8 核 / 大内存 / 有 GPU): 放开 GPU / 多渲染进程 / 2GB JS 堆。

    注意这里只管「资源上限」; 等待/超时/轮询由 src/perf.py 按同一档位给出,
    两处共用 perf_profile 字段, 不会各说各话。
    """
    profile = str(cfg.get("perf_profile") or "low").lower()
    if profile == "mid":
        args = [a for a in LAUNCH_ARGS if a not in _MID_DROP]
        args += [_MID_JS_FLAGS, _MID_RENDERER_LIMIT]
        if not headless and cfg.get("display"):
            args += _SOFTWARE_GL_ARGS
        return args
    if profile == "high":
        args = [a for a in LAUNCH_ARGS if a not in _DESKTOP_DROP]
        args += [_DESKTOP_JS_FLAGS, _DESKTOP_RENDERER_LIMIT]
        if not headless and cfg.get("display"):
            # 虚拟显示(Xvfb)仍要 SwiftShader; 但桌面档要真合成, 不能只
            # 关掉 --disable-gpu 又把 --disable-gpu-compositing 加回来
            args += [a for a in _SOFTWARE_GL_ARGS if a != "--disable-gpu-compositing"]
        return args
    if headless:
        return LAUNCH_ARGS
    # --disable-software-rasterizer 与虚拟显示冲突, headed 下必须移除
    return [
        a for a in LAUNCH_ARGS if a != "--disable-software-rasterizer"
    ] + _SOFTWARE_GL_ARGS


# 2026-10-05 板子实测修订: 原先连 image/font 一起拦, 依据是"图片是软渲染下的
# 内存大头"。实测证伪 —— A=现状全拦 / B=放行抖音自家 / C=完全不拦, 三档峰值
# 1276/1281/1281 MB, 而同一档重跑波动就有 ±70MB: 拦图对 RSS 毫无影响(真正吃
# 1.2GB 的是 chromium 自身)。但"一张图都不加载"是极强的机器人特征, 故改为只
# 拦 media, 图片/字体一律放行。
_BLOCKED_TYPES = {"media"}

# 无物理 GPU 的虚拟显示(Xvfb)下 headed 模式必需: 保留软件光栅器 + 固定
# SwiftShader GL, 否则重型页面(抖音/chat)合成器挂死、body 仅留 a11y 标签、
# 截图超时; Chrome 124+ 用 SwiftShader 还需 --enable-unsafe-swiftshader
# (2026-09-24 板子实测)
_SOFTWARE_GL_ARGS = [
    "--enable-unsafe-swiftshader",
    "--use-gl=angle",
    "--use-angle=swiftshader",
    "--disable-gpu-compositing",
]

# 只拦第三方广告: 抖音看不到"这些请求有没有发出去", 拦掉不改变它眼中的会话形态。
# 抖音自家的埋点/风控/SSO 域(mon.snssdk.com、log.snssdk.com、sso.toutiao.com)
# 一律放行 —— 拦自家埋点等于告诉抖音"这个会话没有风控心跳", 是自证机器人。
_BLOCKED_DOMAINS = {
    "ad.toutiao.com",
    "frontier.tiktokv.com",
}


def block_resources(page: Page) -> None:
    def _route(route):
        req = route.request
        rtype = req.resource_type
        url = req.url.lower()
        if rtype in _BLOCKED_TYPES:
            route.abort()
            return
        host = urlparse(url).hostname or ""
        if any(host == d or host.endswith("." + d) for d in _BLOCKED_DOMAINS):
            route.abort()
            return
        route.continue_()

    page.route("**/*", _route)


def _read_meminfo() -> dict:
    """读取 /proc/meminfo 为 {键: kB 值}; 读取失败抛异常。"""
    with open("/proc/meminfo", encoding="utf-8") as f:
        info = {}
        for line in f:
            k, _, v = line.partition(":")
            info[k.strip()] = int(v.strip().split()[0])
    return info


def available_mb() -> float | None:
    """当前可用内存(MB, 取 /proc/meminfo 的 MemAvailable); 读不到返回 None。

    供调度器决定是否回收复用的浏览器实例(见 config.browser_recycle_mem_mb)。
    """
    try:
        kb = _read_meminfo().get("MemAvailable")
    except Exception:
        return None
    return kb / 1024 if kb else None


def memory_report(tag: str) -> None:
    """记录当前内存使用（帮助定位内存问题）。"""
    try:
        info = _read_meminfo()
        mem = info.get("MemTotal", 0) / 1024
        free = info.get("MemAvailable", 0) / 1024
        log(f"[mem:{tag}] {free:.0f}/{mem:.0f} MB available")
    except Exception as exc:
        log(f"[mem:{tag}] 读取内存信息失败: {exc}")


def free_page_memory(page: Page) -> None:
    """尝试触发浏览器 GC 并释放页面积累的内存。"""
    try:
        page.evaluate("() => { try { window.gc && window.gc(); } catch(e) {} }")
    except Exception as exc:
        log(f"[mem] GC 触发失败: {exc}")


def launch_browser(p, cfg: dict):
    """按 cfg 启动 chromium。

    headless: 默认 True; 设 false 时以有头模式启动(用于 Xvfb/WSLg 虚拟显示,
    规避抖音对 headless chromium 的登录拦截页, 2026-09-24 实测)。
    display: 有头模式下注入 DISPLAY 环境变量(如板子 Xvfb :99)。
    perf_profile: low(板端档) / high(本机桌面档), 见 build_launch_args。
    """
    headless = bool(cfg.get("headless", True))
    args = build_launch_args(cfg, headless)
    if not headless:
        display = cfg.get("display")
        if display:
            os.environ["DISPLAY"] = str(display)
    browser = p.chromium.launch(
        headless=headless, executable_path=_CHROMIUM_PATH, args=args
    )
    memory_report("launch")
    return browser


def open_session(
    p, cfg: dict, account: dict, browser: Browser | None = None
) -> tuple[Browser, BrowserContext, Page]:
    """启动会话并导航到聊天页, 返回 (browser, context, page)。

    传入 browser 时复用该实例(多好友共享浏览器, 省冷启动), 否则新建。
    account: 包含 name, storage_file 等信息的账号配置
    """
    new_browser = browser is None
    if new_browser:
        browser = launch_browser(p, cfg)
    storage_file = get_account_storage_file(account)
    context = None
    try:
        context = restore_state(browser, storage_file)
        page = context.new_page()
        if cfg.get("block_resources", True):
            block_resources(page)
        # 被动嗅探: goto 之前挂载, 才能捕获首屏会话列表的 IM JSON/WS 流量。
        # 纯旁路, 挂载失败不影响主链路。默认开启(cfg.net_sniff=false 可关)。
        if cfg.get("net_sniff", True):
            try:
                NetSniffer(account.get("name", "default")).attach(page)
            except Exception as exc:
                log(f"[sniff] 嗅探器挂载失败(忽略): {exc}")
        page.goto(CHAT_URL, wait_until="commit", timeout=perf.ms("goto_ms"))
        page.wait_for_timeout(perf.ms("settle_ms"))
        if not is_logged_in(page):
            _ensure_login(browser, page, cfg, account)
    except BaseException:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if new_browser:
            try:
                browser.close()
            except Exception:
                pass
        raise
    memory_report("session-ready")
    return browser, context, page


def close_session(browser: Browser | None, context: BrowserContext | None) -> None:
    """关闭会话, 释放浏览器内存。browser 为 None 时仅关闭 context(复用模式)。"""
    try:
        if context is not None:
            context.close()
    except Exception as exc:
        log(f"[session] context 关闭失败: {exc}")
    try:
        if browser is not None:
            browser.close()
    except Exception as exc:
        log(f"[session] browser 关闭失败: {exc}")


class LoginRequiredError(RuntimeError):
    """账号登录态失效且无法自动登录 —— 需要人工登录, 当晚重试也无益。

    以前这里直接 `sys.exit(2)`: 多账号串行时第一个账号掉线会让**整个进程消失**,
    后面本来健康的账号当量根本没跑。SystemExit 是 BaseException, 绕过调度层所有
    `except Exception`(2026-10-05 那条「单账号失败不再掐断整轮」的修复对它无效),
    所以必须换成普通异常, 让账号级隔离真的能隔离。
    """


def _ensure_login(
    browser: Browser, page: Page, cfg: dict, account: dict | None = None
) -> None:
    """未登录时按账号 dict 配置尝试自动登录; 无法登录则抛 LoginRequiredError。

    严格同账号: account dict 缺凭据 = 不登录(不跨账号回退 .env, 避免
    「.env 凭据登入另一账号的 storage 文件」事故, 2026-09-24 修复)。
    """
    account = account or {}
    acc_name = account.get("name", "default")
    acc_cred = (account.get("account"), account.get("password"))
    storage_path = get_account_storage_file(account) if account else None
    if cfg.get("auto_login") and auto_login is not None and acc_cred[0] and acc_cred[1]:
        log(f"[登录] 账号 {acc_name} 使用配置的账号密码自动登录")
        try:
            ok = bool(auto_login(browser, page, acc_cred[0], acc_cred[1], storage_path))
        except Exception as exc:
            log(f"[登录] auto_login 执行异常: {exc}")
            ok = False
        if ok:
            log(f"[登录] 账号 {acc_name} 自动登录成功")
            return
        log("[登录] auto_login 未能完成登录, 转密码登录")
    elif not cfg.get("auto_login"):
        log("[登录] config.auto_login 为 false, 跳过自动登录")
    elif auto_login is None:
        log("[登录] login.py 暂未提供 auto_login 接口, 跳过自动登录")
    else:
        log(f"[登录] 账号 {acc_name} 缺少 account/password 配置, 跳过密码重登")
    dump_page(page)
    log(
        f"【未登录】账号 {acc_name} 请运行 .venv/bin/python login.py --manual 用浏览器人工登录，或检查 config.json 账号配置"
    )
    # 会话生命周期归 open_session 的 except 分支统一收(它会关 context, 以及
    # 本进程新建的 browser); 这里不能再自己关 browser —— 复用模式下那是多个账号
    # 共享的实例, 关掉就等于把下一个好友踩在一具尸体上。
    raise LoginRequiredError(f"账号 {acc_name} 未登录且无法自动登录")


def save_session_state(context: BrowserContext, account: dict, tag: str = "") -> None:
    """保存登录态到账号专用的 storage 文件。"""
    try:
        storage_file = get_account_storage_file(account)
        save_state(context, storage_file)
        log(f"登录态已保存{tag} ({storage_file.name})")
    except Exception as exc:
        log(f"[state] 保存登录态失败: {exc}")
