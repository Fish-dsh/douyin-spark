"""调试辅助：页面截图、文本 dump。"""

from __future__ import annotations

from datetime import datetime

from playwright.sync_api import Page

from logutil import log

from .config import BASE_DIR

try:
    from . import perf
except ImportError:  # 以顶层模块方式加载时(src/ 在 sys.path 上)
    import perf  # type: ignore[no-redef]

SCREENSHOT_DIR = BASE_DIR / "runtime" / "heal_screenshots"

# 每次选择器失败都会落一张截图, 无人值守长期运行须防无界增长
_SCREENSHOT_KEEP = 20


def save_screenshot(page: Page, tag: str = "dump") -> str:
    """保存页面截图到 heal_screenshots/, 供 AI 视觉诊断选择器失效根因。

    返回截图路径; 失败/超时返回空串(不阻塞主流程)。
    """
    try:
        SCREENSHOT_DIR.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        path = SCREENSHOT_DIR / f"{ts}-{tag}.png"
        page.screenshot(
            path=str(path), full_page=False, timeout=perf.ms("screenshot_ms")
        )
        log(f"[debug] 截图已保存: {path}")
        for old in sorted(SCREENSHOT_DIR.glob("*.png"))[:-_SCREENSHOT_KEEP]:
            try:
                old.unlink()
            except OSError:
                pass
        return str(path)
    except Exception as exc:
        log(f"[debug] 截图保存失败: {exc}")
        return ""


def dump_page(page: Page, limit: int = 300) -> str:
    """dump 页面文本信息到日志, 并保存截图; 返回截图路径(失败为空串)。"""
    try:
        log(f"[debug] 页面标题: {page.title()}")
    except Exception:
        log("[debug] 页面标题: <无法读取>")
    try:
        log(f"[debug] 页面 URL: {page.url}")
    except Exception:
        pass
    try:
        body = page.evaluate("() => document.body ? document.body.innerText : ''")
        text = " ".join(str(body).split())[:limit] if body else "(正文为空)"
        log(f"[debug] body 前 {limit} 字: {text}")
    except Exception as exc:
        log(f"[debug] 无法读取正文: {exc}")
    return save_screenshot(page)
