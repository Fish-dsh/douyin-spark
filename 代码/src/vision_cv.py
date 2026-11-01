"""vision_cv.py · Emergency Visual Fallback 的定位层(纯经典视觉, 无 AI/OCR/网络)。

职责边界: **只从截图里找红色↑发送按钮, 绝不点击、绝不判断该不该点**。
点击与安全验证在 sender.cv_send_fallback(坐标 ROI 限定 + 身份复核在调用方)。

算法(2026-10-06 板端独立实验实测后选定, 数据见 WORKLOG 会话「OpenCV 视觉定位」):
  HSV 红色双区间掩码 → 形态学闭 → findContours →
  面积 200~5000px + 圆形度≥0.7 + 宽高比 0.8~1.25 联合过滤。
  800x600 全帧 13ms/帧, 32/32 命中零漂移; 红色未读角标/火焰图标/聊天区
  单独扫描均不误报; 5 尺度模板匹配 670ms 太慢已弃用。

降级: cv2 不可用(未装/损坏)时 available()=False、find_send_button 返回 None,
主链路完全不受影响 —— 本层只出现在"其他层已全失败"的路径上。
"""

from __future__ import annotations

try:
    import cv2
    import numpy as np

    _AVAILABLE = True
except ImportError:  # pragma: no cover - 取决于运行环境是否装 opencv
    cv2 = None  # type: ignore[assignment]
    np = None  # type: ignore[assignment]
    _AVAILABLE = False

from logutil import log

# 红色↑按钮的圆形特征参数(板端截图实测: 直径约 27px, 面积 ~566px)
_MIN_AREA = 200
_MAX_AREA = 5000
_MIN_CIRCULARITY = 0.7
_MIN_ASPECT = 0.8
_MAX_ASPECT = 1.25


def available() -> bool:
    """cv2/numpy 是否可用; False 时本层直接放弃, 不做任何截图解码。"""
    return _AVAILABLE


def _decode(png_bytes: bytes):
    arr = np.frombuffer(png_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return None
    # 渲染异常守卫: 纯黑/近黑页(加载失败、截图竞态)没有可信的视觉信号
    if float(img.mean()) < 8.0:
        log(f"[cv定位] 页面近黑(mean={img.mean():.1f}), 拒绝在渲染异常状态上定位")
        return None
    return img


def _in_roi(cx: int, cy: int, roi: tuple[int, int, int, int]) -> bool:
    rx, ry, rw, rh = roi
    return rx <= cx <= rx + rw and ry <= cy <= ry + rh


def find_send_button(png_bytes: bytes, roi: tuple[int, int, int, int] | None = None):
    """在 PNG 截图字节里找红色↑发送按钮。

    roi=(x,y,w,h) 为截图坐标系下的限定扫描区(输入框右侧条带), None=全帧。
    返回 {"x","y","area","conf"} (中心坐标) 或 None(未找到/不可用/渲染异常)。
    多个候选时取圆形度最高、其次最靠右下者——按钮语义位置在输入区右端。
    """
    if not _AVAILABLE:
        return None
    img = _decode(png_bytes)
    if img is None:
        return None
    h, w = img.shape[:2]
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    m1 = cv2.inRange(hsv, (0, 120, 120), (10, 255, 255))
    m2 = cv2.inRange(hsv, (170, 120, 120), (180, 255, 255))
    mask = cv2.morphologyEx(m1 | m2, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best = None  # (score, candidate)
    for c in contours:
        area = cv2.contourArea(c)
        if not _MIN_AREA <= area <= _MAX_AREA:
            continue
        per = cv2.arcLength(c, True)
        if per <= 0:
            continue
        circ = 4 * np.pi * area / (per * per)
        _, _, bw, bh = cv2.boundingRect(c)
        aspect = bw / bh if bh else 0.0
        if circ < _MIN_CIRCULARITY or not _MIN_ASPECT <= aspect <= _MAX_ASPECT:
            continue
        M = cv2.moments(c)
        if not M["m00"]:
            continue
        cx, cy = int(M["m10"] / M["m00"]), int(M["m01"] / M["m00"])
        if roi is not None and not _in_roi(cx, cy, roi):
            continue
        score = float(circ) + (cx / w) * 0.1 + (cy / h) * 0.1
        if best is None or score > best[0]:
            best = (
                score,
                {"x": cx, "y": cy, "area": int(area), "conf": round(float(circ), 3)},
            )
    return best[1] if best else None
