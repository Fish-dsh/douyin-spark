"""抖音网页版（www.douyin.com）登录模块.

对外接口:
    is_logged_in(page)
    save_state(context)
    restore_state(browser)
    auto_login(browser, page, account, password, storage_path=None)
    main()

登录态持久化到 storage.json, 供后续无人值守脚本复用。
"""

from __future__ import annotations

import glob
import json
import os
import sys
import time
from pathlib import Path

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

from logutil import log

try:
    from . import perf
except ImportError:  # 以顶层模块方式加载时(src/ 在 sys.path 上)
    import perf  # type: ignore[no-redef]
from .config import CHAT_URL, RUNTIME_DIR, load_env
from .stealth import apply_stealth

# playwright 在 aarch64（OrangePi 板子）可能无法自动下载 chromium, 需探测已装浏览器。
_CHROMIUM_PATH = None


def _resolve_chromium() -> str | None:
    """返回可用的 chromium 可执行文件路径, 无则 None(交 playwright 默认处理)。"""
    global _CHROMIUM_PATH
    if _CHROMIUM_PATH:
        return _CHROMIUM_PATH
    if (
        (env := os.environ.get("PLAYWRIGHT_CHROMIUM_PATH"))
        and os.path.isfile(env)
        and os.access(env, os.X_OK)
    ):
        _CHROMIUM_PATH = env
        return env
    for user in (str(Path.home()), "/home/BOARD"):
        for pat in ("chromium*/chrome-linux*/chrome", "chromium*/chrome-linux/chrome"):
            for hit in glob.glob(f"{user}/.cache/ms-playwright/{pat}"):
                if os.path.isfile(hit) and os.access(hit, os.X_OK):
                    _CHROMIUM_PATH = hit
                    return hit
    return None


_resolve_chromium()

STORAGE = "storage.json"

BASE_URL = "https://www.douyin.com/"
STORAGE_PATH = RUNTIME_DIR / STORAGE

# 登录弹层候选选择器, 任一可见即认为处于未登录状态。
LOGIN_PANEL_SELECTORS = (
    "#login-panel",
    "#login-modal",
    "#login",
    ".login-panel",
    "[data-e2e='login-modal']",
    "[data-e2e='login-qrcode']",
)

QR_CODE_SELECTORS = (
    "img[src*='qrcode']",
    "img[src*='qrCode']",
    "img[class*='qrcode']",
    "img[class*='qr']",
    "[class*='qrcode'] img",
)

# 已登录用户信息证据
USER_INFO_SELECTORS = (
    "[data-e2e='user-info']",
    "[data-e2e='user-avatar']",
    "img[src*='douyinpic.com']",
    "img[class*='avatar']",
)

# 登录弹窗独有文本特征(选择器随版本变动时的兜底):
# 抖音网页版登录弹窗固定包含这些文案, 已登录页面不会同时出现。
LOGIN_TEXT_MARKS = (
    "扫码登录",
    "验证码登录",
    "登录即代表同意用户协议",
)

# 仅登录后可达的路径前缀(现仅作诊断日志标注, 不参与登录判定)。
LOGGED_URL_PREFIXES = ("/chat", "/messages", "/user", "/favorite", "/history")

# douyin 会话 cookie 名称, 存在即视为有登录态。
SESSION_COOKIES = ("sessionid", "sessionid_ss", "uid_tt")


def _log(msg: str) -> None:
    # 统一日志出口: 复用 logutil, 保留旧前缀风格
    log(msg, prefix="login")


def _element_visible(page: Page, selector: str) -> bool:
    try:
        el = page.query_selector(selector)
        return bool(el and el.is_visible())
    except Exception:
        return False


def _body_text(page: Page, limit: int = 400) -> str:
    try:
        text = page.evaluate("() => document.body.innerText") or ""
        return " ".join(str(text).split())[:limit] if text else ""
    except Exception:
        return ""


# 页面出现实质内容的文本量阈值(骨架阶段仅十几字符)
_CONTENT_MIN_CHARS = 80


def _wait_for_content(page: Page) -> None:
    """等待页面渲染出实质内容; 超时即返回(不阻塞, 交由后续判定兜底)。"""
    # 上限与轮询间隔跟性能档位走(板端 12s/500ms, 桌面档 6s/250ms)。
    _poll = max(1, perf.ms("login_poll_ms"))
    for _ in range(max(1, perf.ms("login_content_ms") // _poll)):
        try:
            if len(_body_text(page, 4000)) >= _CONTENT_MIN_CHARS:
                return
        except Exception:
            return
        try:
            page.wait_for_timeout(_poll)
        except Exception:
            return


def _raw_text(page: Page, limit: int = 4000) -> str:
    """取全量文本(textContent) —— 覆盖 innerText 拿不到的隐藏/未展开节点。

    抖音登录页把"扫码登录/验证码登录"等文案放在未展开容器里, innerText 看不到,
    导致 is_logged_in 退回"有 cookie 即已登录"的误判。
    """
    try:
        text = page.evaluate("() => document.body.textContent") or ""
        return " ".join(str(text).split())[:limit] if text else ""
    except Exception:
        return ""


def _panel_text(page: Page, limit: int = 200) -> str:
    area = page.query_selector("#login-panel") or page.query_selector("#login")
    if not area:
        return ""
    try:
        return " ".join(area.inner_text().split())[:limit]
    except Exception:
        return ""


def _session_cookies(page: Page) -> list[str]:
    try:
        cookies = page.context.cookies("https://www.douyin.com")
        return [c["name"] for c in cookies if c["name"] in SESSION_COOKIES]
    except Exception:
        return []


def _any(page: Page, selectors: tuple[str, ...]) -> bool:
    return any(_element_visible(page, s) for s in selectors)


def is_logged_in(page: Page, quick: bool = False) -> bool:
    """判断当前 douyin.com 页面是否已登录.

    检测策略:
    1. 页面出现登录弹层/二维码/登录文案 -> 未登录
    2. 出现用户头像等信息元素, 或 douyin 会话 cookie 存在 -> 已登录
    3. 其余 -> 未登录
    URL 处于 /chat 等路径(path)仅作诊断日志输出, 不参与判定
    (旧「已登录路径」分支因与策略 2 条件重复永不可达, 2026-10-01 移除)。
    每轮输出 URL、选择器命中情况与文本片段, 便于调试。
    """
    # 页面骨架阶段 innerText 只有导航文案("开启读屏标签"等), 此时判定必然误判;
    # 等出现实质内容(或超时)再检测 —— 实测骨架到渲染完成相差约 37s。
    # 性能(2026-11-01): quick=True 用于「页面显然已经加载完」的重复复核
    # (例如同一好友流程里第二次调用), 跳过最多 12s 的骨架内容轮询。
    # 判定逻辑本身不变, 只是不再等一个已经满足的条件。
    if not quick:
        _wait_for_content(page)

    url = page.url
    path = url.removeprefix("https://www.douyin.com").split("?", 1)[0].rstrip("/")
    path_logged = any(path.startswith(p) for p in LOGGED_URL_PREFIXES)

    panel_hit = _any(page, LOGIN_PANEL_SELECTORS)
    qr_hit = _any(page, QR_CODE_SELECTORS)
    user_hit = _any(page, USER_INFO_SELECTORS)
    cookies = _session_cookies(page)

    snippet = _body_text(page, 600)
    text_hit = any(m in snippet for m in LOGIN_TEXT_MARKS)
    if not text_hit:
        # innerText 看不到隐藏/未展开的登录面板; 用 textContent 复核一次
        raw_snippet = _raw_text(page)
        if any(m in raw_snippet for m in LOGIN_TEXT_MARKS):
            text_hit = True
            _log("[detect] innerText 未含登录文案, 但 textContent 命中 -> 判为登录页")

    _log(
        f"[detect] url={url} | login_panel={panel_hit} | qrcode={qr_hit} "
        f"| user_info={user_hit} | login_text={text_hit} | path={path or '<root>'} | "
        f"logged_path={path_logged} | session_cookies={cookies}"
    )

    if snippet:
        _log(f"[detect] text='{snippet[:200]}'")

    if panel_hit or qr_hit or text_hit:
        _log("[detect] 判定=未登录: 存在登录弹层/二维码/登录文案")
        return False

    if user_hit or cookies:
        _log(f"[detect] 判定=已登录: 用户信息可见或会话 cookie={cookies}")
        return True

    _log(
        "[detect] 判定=未登录: 无用户信息且非已登录路径(或处于已登录路径但无任何会话依据)"
    )
    return False


def _show_login_panel(page: Page, timeout: float = 60.0) -> None:
    """若登录弹层未出现, 尝试点击页面上的『登录』入口使其弹出二维码。

    /chat 未登录时登录面板并非立即渲染, 实测需 6-12s; 板子(弱设备)可达 40s+,
    故默认放宽到 60s, 否则还没渲染出来就放弃(2026-10-05: 此前 20s 上限直接
    导致密码登录永远走不到, 被误判为"抖音撤了密码登录")。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _any(page, LOGIN_PANEL_SELECTORS):
            _log("[action] 登录弹层已可见, 无需点击登录入口")
            return

        _log("[action] 未发现登录弹层, 尝试点击『登录』入口")
        clicked = False
        try:
            clicked = bool(
                page.evaluate(
                    """() => {
                        const els = [...document.querySelectorAll('*')].filter(
                            e => e.children.length === 0
                              && e.innerText && e.innerText.trim() === '登录'
                              && e.offsetParent !== null
                        );
                        if (els.length === 0) return false;
                        els[0].click();
                        return true;
                    }"""
                )
            )
        except Exception:
            clicked = False

        if not clicked:
            for sel in ("a:has-text('登录')", "button:has-text('登录')"):
                try:
                    page.locator(sel).first.click(timeout=perf.ms("click_small_ms"))
                    clicked = True
                    break
                except Exception:
                    continue

        _log(
            "[action] 已点击『登录』入口, 期待二维码出现"
            if clicked
            else "[action] 未找到可点击的『登录』入口"
        )
        # 每轮尝试后等一拍检测弹层是否浮现, 未出现则重试
        page.wait_for_timeout(perf.ms("login_step_wait_ms"))
    try:
        page.wait_for_selector(
            "#login-panel", state="visible", timeout=perf.ms("login_panel_ms")
        )
        _log("[action] 登录弹层已出现")
    except Exception:
        _log("[action] 等待登录弹层超时, 继续轮询检测")


def save_state(context: BrowserContext, storage_path: str | Path | None = None) -> None:
    """把 context 的登录态(cookies/localStorage)写入指定 storage 文件。

    storage_path 为 None 时写入默认 STORAGE_PATH(兼容旧单账号调用)。
    """
    dest = Path(storage_path) if storage_path else STORAGE_PATH
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(dest))
        _log(f"[state] 登录态已保存到 {dest.name}")
    except Exception as exc:
        _log(f"[state] 保存登录态失败: {exc}")
        raise RuntimeError(f"保存登录态失败: {exc}") from exc


def restore_state(
    browser: Browser, storage_path: str | Path | None = None
) -> BrowserContext:
    """从指定 storage 文件恢复 context, 文件不存在时返回干净 context 并打日志。

    storage_path 为 None 时用默认 STORAGE_PATH(兼容旧单账号调用)。
    """
    src = Path(storage_path) if storage_path else STORAGE_PATH
    if not src.is_file():
        _log(f"[state] 未找到 {src.name}, 使用干净 context")
        context = browser.new_context(
            locale="zh-CN", viewport={"width": 800, "height": 600}
        )
    else:
        try:
            context = browser.new_context(
                storage_state=str(src),
                locale="zh-CN",
                viewport={"width": 800, "height": 600},
            )
            _log(f"[state] 已从 {src.name} 恢复登录态")
        except Exception as exc:
            _log(f"[state] 恢复登录态失败({exc}), 退回干净 context")
            context = browser.new_context(
                locale="zh-CN", viewport={"width": 800, "height": 600}
            )
    # 所有 context 的统一出口: 注入自动化特征伪装(见 stealth 模块, 2026-10-05)
    apply_stealth(context)
    return context


def _open_home(page: Page) -> None:
    try:
        page.goto(
            BASE_URL, wait_until="domcontentloaded", timeout=perf.ms("page_dom_ms")
        )
        _log("[page] 已打开 https://www.douyin.com/")
    except Exception as exc:
        _log(f"[page] 打开首页失败: {exc}")
        sys.exit(1)


def _open_chat(page: Page) -> None:
    """打开聊天页: 未登录时会自动弹出登录框(密码/验证码), 是登录与续火的主入口。

    直接 goto /chat(登录弹层在该页自动出现, 无需先开首页触发);
    wait_until=commit 不阻塞等首页海量视频资源, 兼容弱设备/X 转发(2026-09-24)。
    """
    try:
        page.goto(CHAT_URL, wait_until="commit", timeout=perf.ms("goto_commit_ms"))
        _log("[page] 已打开 https://www.douyin.com/chat")
    except Exception as exc:
        _log(f"[page] 打开聊天页失败: {exc}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# 密码登录与自动重登 (供 spark.py 无人值守调用)
# ---------------------------------------------------------------------------

LOGIN_STATE_FILE = RUNTIME_DIR / "login_state.json"

# 密码登录入口 tab 候选文本
PASSWORD_TAB_TEXTS = ("密码登录", "账号密码登录", "使用密码登录")
# 账号输入框候选
ACCOUNT_INPUT_SELECTORS = (
    "input[placeholder*='手机号']",
    "input[placeholder*='账号']",
    "input[type='text']",
    "input[type='tel']",
)
PASSWORD_INPUT_SELECTORS = (
    "input[type='password']",
    "[type='password']",
    "input[placeholder*='密码']",
    "input[name='password']",
    "input[id*='password']",
    "input[class*='password']",
)
LOGIN_SUBMIT_SELECTORS = (
    "button:has-text('登录')",
    "button:has-text('立即登录')",
)
# 滑块/行为验证码出现的证据
CAPTCHA_SELECTORS = (
    "iframe[src*='captcha']",
    "iframe[src*='verify']",
    "[class*='captcha']",
    "[class*='verify-code']",
    "[class*='slider']",
    "img[src*='captcha']",
)

# 自动重登失败后的最小重试间隔(秒), 避免频繁重试触发风控
AUTO_LOGIN_MIN_INTERVAL = 3600


def _click_text(page: Page, text: str) -> bool:
    """按精确文本点击页面上第一个可见的文本叶子节点。

    douyin 登录 tab 存在重复/隐藏文本副本, Playwright 的 text= 引擎会命中
    非交互副本导致点击无效(实测「密码登录」tab 点了但面板不变切换);
    JS 过滤(可见 + 叶子节点)后点击, 见 2026-09-24 排障。
    """
    try:
        return bool(
            page.evaluate(
                """(text) => {
                    const els = [...document.querySelectorAll('span,button,div,a')].filter(
                        e => e.innerText && e.innerText.trim() === text
                          && e.children.length === 0
                          && e.offsetParent !== null
                          && e.getBoundingClientRect().width > 0
                    );
                    if (!els.length) return false;
                    els[0].click();
                    return true;
                }""",
                text,
            )
        )
    except Exception:
        return False


def _captcha_visible(page: Page) -> bool:
    return _any(page, CAPTCHA_SELECTORS)


def _fill_input(page: Page, selectors: tuple[str, ...], value: str) -> bool:
    """依次尝试候选选择器填充输入框, 成功返回 True。"""
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.fill(value)
                _log(f"[pwd] 已填充输入框({sel})")
                return True
        except Exception:
            continue
    return False


def password_login(page: Page, account: str, password: str) -> bool:
    """执行网页版密码登录流程.

    流程: 打开登录面板 -> 切换到『密码登录』tab -> 填账号密码 -> 点登录。
    成功(is_logged_in)返回 True; 出现滑块验证或超时返回 False。
    """
    _show_login_panel(page)

    # 板子(弱设备)弹层渲染慢, tab 文案可能晚于弹层出现; 轮询重试而非只试一次
    # (2026-10-05: 一次性点击在慢设备上必失败, 会误判为"没有密码登录入口")。
    tab_ok = False
    deadline = time.monotonic() + perf.sec("login_tab_deadline_ms")
    while time.monotonic() < deadline:
        for text in PASSWORD_TAB_TEXTS:
            if _click_text(page, text):
                _log(f"[pwd] 已切换到『{text}』")
                tab_ok = True
                break
        if tab_ok:
            break
        page.wait_for_timeout(perf.ms("login_step_wait_ms"))
    if not tab_ok:
        _log("[pwd] 未找到『密码登录』入口, 尝试直接填写(部分版本默认即为账号密码)")

    # 等密码框真正渲染出来再填(弱设备渲染慢, 过早 fill 会落空)
    _pwd_poll = max(1, perf.ms("pwd_field_poll_ms"))
    _pwd_budget = perf.ms("pwd_field_budget_ms")
    for _ in range(max(1, _pwd_budget // _pwd_poll)):
        if _element_visible(page, "input[type='password']"):
            break
        page.wait_for_timeout(_pwd_poll)

    if not _fill_input(page, ACCOUNT_INPUT_SELECTORS, account):
        _log("[pwd] 未找到账号输入框, 登录失败")
        return False
    if not _fill_input(page, PASSWORD_INPUT_SELECTORS, password):
        _log("[pwd] 未找到密码输入框, 登录失败")
        return False

    submitted = False
    for sel in LOGIN_SUBMIT_SELECTORS:
        try:
            btn = page.locator(sel).first
            if btn.is_visible():
                btn.click(timeout=perf.ms("input_click_ms"))
                _log(f"[pwd] 已点击登录按钮({sel})")
                submitted = True
                break
        except Exception:
            continue
    if not submitted:
        # 抖音登录按钮是 DIV 而非 button: 精确文本『登录』的可点击元素
        submitted = bool(
            page.evaluate(
                """() => {
                    const els = [...document.querySelectorAll('div,button,span,a')].filter(
                        e => e.children.length === 0
                          && e.innerText && e.innerText.trim() === '登录'
                          && e.offsetParent !== null
                    );
                    if (!els.length) return false;
                    els[els.length - 1].click();
                    return true;
                }"""
            )
        )
        if submitted:
            _log("[pwd] 已通过精确文本『登录』点击登录按钮")
        else:
            _log("[pwd] 未找到登录按钮, 尝试回车提交")
            page.keyboard.press("Enter")
            submitted = True

    deadline = time.monotonic() + perf.sec("login_state_deadline_ms")
    while time.monotonic() < deadline:
        if _captcha_visible(page):
            _log("[pwd] 检测到滑块/行为验证, 自动登录失败")
            return False
        if is_logged_in(page):
            _dismiss_save_login_prompt(page)
            _log("[pwd] 密码登录成功")
            return True
        page.wait_for_timeout(perf.ms("login_state_poll_ms"))

    _log("[pwd] 登录超时, 自动登录失败")
    return False


def _dismiss_save_login_prompt(page: Page) -> None:
    """登录成功后抖音会弹『是否保存登录信息』确认框, 自动点『保存』。"""
    try:
        text = page.evaluate("() => document.body.innerText") or ""
        if "是否保存登录信息" not in text:
            return
        ok = page.evaluate(
            """() => {
                const els = [...document.querySelectorAll('div,button,span,a')].filter(
                    e => e.children.length === 0
                      && e.innerText && e.innerText.trim() === '保存'
                      && e.offsetParent !== null
                );
                if (!els.length) return false;
                els[els.length - 1].click();
                return true;
            }"""
        )
        _log(
            "[pwd] 已处理『是否保存登录信息』弹窗(点保存)"
            if ok
            else "[pwd] 未找到『保存』按钮, 忽略保存弹窗"
        )
    except Exception as exc:
        _log(f"[pwd] 处理保存登录弹窗异常: {exc}")


def _read_login_state() -> dict:
    try:
        if LOGIN_STATE_FILE.is_file():
            data = json.loads(LOGIN_STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _write_login_state(state: dict) -> None:
    try:
        LOGIN_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = LOGIN_STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(str(tmp), str(LOGIN_STATE_FILE))
    except Exception as exc:
        _log(f"[state] 写 login_state.json 失败: {exc}")


def auto_login(
    browser: Browser,
    page: Page,
    account: str | None,
    password: str | None,
    storage_path: str | Path | None = None,
) -> bool:
    """无人值守自动重登: 用账号密码重新登录并保存登录态.

    含风控退避: 距上次失败不足 AUTO_LOGIN_MIN_INTERVAL 秒时直接拒绝,
    避免频繁重试触发滑块/账号锁定。
    storage_path 为 None 时保存到默认 STORAGE_PATH。
    严格同账号: 缺凭据直接拒绝, 绝不回退 .env —— 多账号下 .env 凭据
    登入当前账号的 storage 文件即跨账号误登(2026-09-24 事故)。
    """
    if not account or not password:
        _log("[auto] 未提供账号密码(不回退 .env, 防跨账号误登), 无法自动重登")
        return False

    st = _read_login_state()
    last_fail = st.get("last_fail")
    if last_fail:
        try:
            waited = time.time() - float(last_fail)
            if waited < AUTO_LOGIN_MIN_INTERVAL:
                _log(
                    f"[auto] 距上次登录失败不足 {AUTO_LOGIN_MIN_INTERVAL / 60:.0f} 分钟, "
                    f"跳过自动重登(避免风控)"
                )
                return False
        except Exception:
            pass

    _log("[auto] 开始自动重登: 账号=***")
    ok = password_login(page, account, password)
    if ok:
        try:
            save_state(page.context, storage_path)
        except Exception as exc:
            _log(f"[auto] 保存登录态异常: {exc}")
        _write_login_state({})
        return True

    st["last_fail"] = str(time.time())
    st["fail_count"] = st.get("fail_count", 0) + 1
    _write_login_state(st)
    _log(f"[auto] 自动重登失败(累计 {st['fail_count']} 次), 明日将再次尝试")
    return False


def manual_login(
    page: Page,
    storage_path: str | Path | None = None,
    timeout: float = 900.0,
) -> bool:
    """人工交互登录: 打开聊天页后由用户在浏览器窗口内自行完成登录.

    用于密码提交被反爬拦截、只能走短信验证码等人工步骤的账号。
    轮询 is_logged_in(), 检测到已登录即保存登录态到 storage_path。
    超时返回 False。
    """
    _open_chat(page)
    page.wait_for_timeout(perf.ms("login_step_wait_ms"))
    if is_logged_in(page):
        _log("[manual] 已有有效登录态, 无需登录")
        save_state(page.context, storage_path)
        return True

    _log(
        f"[manual] 请在浏览器窗口内完成登录(如短信验证码), 最多等待 {timeout / 60:.0f} 分钟"
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_logged_in(page):
            _dismiss_save_login_prompt(page)
            save_state(page.context, storage_path)
            _log("[manual] 登录成功, 登录态已保存")
            return True
        page.wait_for_timeout(perf.ms("manual_poll_ms"))
    _log("[manual] 等待登录超时")
    return False


def main() -> None:
    """CLI 入口.

    python login.py --password [--storage file]  用 .env 中的账号密码登录(自动重登入口)
    python login.py --manual [--storage file]    有头浏览器人工登录(短信验证码等), 成功后存登录态
    """
    args = sys.argv[1:]
    storage_path = None
    if "--storage" in args:
        idx = args.index("--storage")
        if idx + 1 < len(args):
            storage_path = args[idx + 1]
            args = [a for i, a in enumerate(args) if i not in (idx, idx + 1)]

    headless = "--headless" in args

    if "--manual" in args:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=False,
                executable_path=_CHROMIUM_PATH,
                args=[
                    "--lang=zh-CN",
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    # WSLg 下不加此参数窗口进程存活但界面渲染空白(任务栏有图标无窗口)
                    "--disable-gpu",
                ],
            )
            context = restore_state(browser, storage_path)
            page = context.new_page()
            try:
                if not manual_login(page, storage_path):
                    sys.exit(1)
            finally:
                context.close()
                browser.close()
        return

    if "--password" in args:
        account, password = load_env()
        account = account or ""
        if not account or not password:
            _log(
                "[pwd] 缺少账号密码: 请在 .env 中配置 DOUYIN_ACCOUNT / DOUYIN_PASSWORD"
            )
            sys.exit(1)
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=headless,
                executable_path=_CHROMIUM_PATH,
                args=["--lang=zh-CN"],
            )
            context = restore_state(browser, storage_path)
            page = context.new_page()
            try:
                _open_chat(page)
                page.wait_for_timeout(perf.ms("login_step_wait_ms"))
                if is_logged_in(page):
                    _log("[pwd] 已有有效登录态, 无需登录")
                elif password_login(page, account, password):
                    save_state(context, storage_path)
                else:
                    _log("[pwd] 密码登录失败(可能弹滑块)")
                    sys.exit(1)
            finally:
                context.close()
                browser.close()
        return

    _log(
        "[cli] 未指定登录方式, 请使用 --password(账号密码登录) 或 --manual(有头人工登录)"
    )
    sys.exit(2)


if __name__ == "__main__":
    main()
