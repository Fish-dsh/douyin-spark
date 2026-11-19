"""本地恢复策略表: 把「自愈」从「AI 改源码」降级为「错误分类 → 确定性恢复动作」。

借鉴同类无人值守项目(Playwright 自愈库 / 运维 watchdog)的共识: 真实故障形态
(选择器失效 / 渲染挂死 / 限流 / OOM / 登录过期)里**没有一个是源码 bug**, 拿
"改代码"去解属于工具错配 —— 正确做法是「分类 → 策略表 → 重试/降级/跳过 → 告警」。
本项目连最后一档(选择器相似度打分)都用不上: 抖音改版只能人工更新选择器。

本模块是**纯函数**, 不依赖 playwright / 网络 / 文件系统, 便于单测与复用。
调用方(self_heal facade / scheduler / discovery)拿到动作后自己执行 —— 只有它
手里有页面与会话句柄。
"""

from __future__ import annotations

# ── 故障类别 ──────────────────────────────────────────────────────────────
TRANSIENT = "transient"  # 瞬时: 超时 / 连接重置 / target closed / 未确认
SELECTOR = "selector"  # 选择器失效: 页面有内容但定位不到元素(疑似改版)
RENDER_HANG = "render_hang"  # 渲染挂死: body 全空
RESOURCE = "resource"  # 资源压力: OOM / 内存不足
LOGIN = "login"  # 登录态失效
UNKNOWN = "unknown"  # 未识别

# ── 恢复动作 ──────────────────────────────────────────────────────────────
RETRY = "retry"  # 原样重试一次
RELOAD_RETRY = "reload_retry"  # reload 页面后重试
RESTART_SESSION = "restart_session"  # 丢弃会话/浏览器后重建再试
SKIP = "skip"  # 跳过该对象, 不重试
ALERT = "alert"  # 跳过 + 人工告警

# 类别 → 动作。**这里就是"运维策略"** —— 调它不需要碰任何业务代码。
_POLICY: dict[str, str] = {
    TRANSIENT: RETRY,
    SELECTOR: RELOAD_RETRY,
    RENDER_HANG: RESTART_SESSION,
    RESOURCE: RESTART_SESSION,
    LOGIN: ALERT,
    UNKNOWN: ALERT,
}

# 判定规则(顺序即优先级): 越"具体/危险"的越先判, 避免 OOM 日志里恰好出现
# "未找到" 就被误判为选择器。标记一律小写, 匹配前文本也转小写。
_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (RENDER_HANG, ("渲染挂死", "body 全空", "blank", "page_render_state")),
    (LOGIN, ("未登录", "登录态", "logged_out", "login")),
    (
        RESOURCE,
        ("out of memory", "oom", "cannot allocate", "内存不足", "memoryerror", "内存"),
    ),
    (
        TRANSIENT,
        (
            "timeout",
            "timed out",
            "超时",
            "target closed",
            "net::",
            "econnreset",
            "connection",
            "broken pipe",
            "发送未确认",
            "not confirmed",
        ),
    ),
    (
        SELECTOR,
        (
            "未找到",
            "定位",
            "选择器",
            "未命中",
            "找不到",
            "按钮",
            "表情",
            "失效",
            "覆盖缺失",
            "漏检",
            "改版",
        ),
    ),
)

# 需要调用方重试的动作
_RETRY_ACTIONS = frozenset({RETRY, RELOAD_RETRY, RESTART_SESSION})


def classify(error_type: str, error_msg: str) -> str:
    """把 (错误类型, 错误摘要) 归入一个故障类别; 认不出时返回 `UNKNOWN`。"""
    text = f"{error_type} {error_msg}".lower()
    for category, markers in _RULES:
        if any(m in text for m in markers):
            return category
    return UNKNOWN


def policy(category: str) -> str:
    """类别 → 恢复动作; 未知类别一律告警(不猜测、不硬试)。"""
    return _POLICY.get(category, ALERT)


def should_retry(action: str) -> bool:
    """该动作是否意味着"值得让调用方重试一次"。"""
    return action in _RETRY_ACTIONS
