"""浏览器自动化特征伪装: 在页面脚本执行前注入 stealth 补丁。

背景(2026-10-05): 抖音风控读到板子的 SwiftShader 软件渲染 + 空 plugins +
navigator.webdriver 后, 判定为自动化, 静默隐藏了登录弹窗里的「密码登录」
tab(同一账号在真机桌面 UA 下可见)。同类项目 MediaCrawler 同样靠
stealth.min.js 打补丁后接入。故此处做同层伪装。

红线: 只做「特征伪装」, 绝不触碰滑块/行为验证码。
"""

from __future__ import annotations

from playwright.sync_api import BrowserContext

from logutil import log

# 参考 puppeteer-extra-plugin-stealth 的经典补丁, 逐项覆盖抖音会读的特征。
# 每一项独立 try/catch: 单项失败不影响其余, 也不抛给页面。
_STEALTH_JS = r"""
(() => {
  // 1) navigator.webdriver: WebDriver 控制的照妖镜
  try {
    Object.defineProperty(navigator, 'webdriver', {
      get: () => false, configurable: true
    });
  } catch (e) {}

  // 2) plugins / mimeTypes: 空数组是 headless 的强特征, 补成真实 Chrome 的形态
  try {
    const plugin = {
      name: 'Chrome PDF Viewer',
      filename: 'internal-pdf-viewer',
      description: 'Portable Document Format',
    };
    Object.defineProperty(navigator, 'plugins', {
      get: () => [plugin, plugin, plugin], configurable: true
    });
    Object.defineProperty(navigator, 'mimeTypes', {
      get: () => [{ type: 'application/pdf' }], configurable: true
    });
  } catch (e) {}

  // 3) languages: 板子 locale 为 zh-CN
  try {
    Object.defineProperty(navigator, 'languages', {
      get: () => ['zh-CN', 'zh'], configurable: true
    });
  } catch (e) {}

  // 4) window.chrome: 真 Chrome 一定有, 自动化环境常缺失
  try {
    if (!window.chrome) { window.chrome = {}; }
    if (!window.chrome.runtime) { window.chrome.runtime = {}; }
    if (!window.chrome.app) { window.chrome.app = { isInstalled: false }; }
    if (!window.chrome.csi) { window.chrome.csi = function () { return {}; }; }
    if (!window.chrome.loadTimes) {
      window.chrome.loadTimes = function () { return {}; };
    }
  } catch (e) {}

  // 5) WebGL 厂商/渲染器: 板子软件渲染会暴露 "SwiftShader", 是最致命的一条。
  //    37445=UNMASKED_VENDOR_WEBGL, 37446=UNMASKED_RENDERER_WEBGL。
  try {
    const patch = (proto) => {
      if (!proto) { return; }
      const original = proto.getParameter;
      proto.getParameter = function (p) {
        if (p === 37445) { return 'Google Inc. (Intel)'; }
        if (p === 37446) {
          return 'ANGLE (Intel, Intel(R) UHD Graphics, OpenGL 4.5)';
        }
        return original.call(this, p);
      };
    };
    patch(window.WebGLRenderingContext && WebGLRenderingContext.prototype);
    patch(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype);
  } catch (e) {}

  // 6) permissions.query: headless 下 notifications 状态与真实浏览器不符
  try {
    const originalQuery = window.navigator.permissions.query.bind(
      window.navigator.permissions
    );
    window.navigator.permissions.query = (parameters) =>
      parameters && parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : originalQuery(parameters);
  } catch (e) {}
})();
"""


def apply_stealth(context: BrowserContext) -> None:
    """给 context 注入伪装脚本(每个页面加载前执行); 失败不影响主链路。"""
    try:
        context.add_init_script(_STEALTH_JS)
    except Exception as exc:
        log(f"[stealth] 注入伪装脚本失败(忽略): {exc}")
