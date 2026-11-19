"""vision_cv.py 单测：红色↑按钮合成图定位、过滤条件、ROI、降级。"""

from __future__ import annotations

import unittest

from src import vision_cv

# opencv 是可选依赖: 运行主链路时不装也能跑, 所以最小 CI 里不装它。
# 这里不能裸 import cv2 —— 那会让整个测试文件在收集阶段就 ImportError,
# 把"没装 opencv"变成"CI 挂了"。改成按需跳过, 只跳需要真的画图的用例。
try:
    import cv2
    import numpy as np

    _CV_OK = True
except ImportError:  # pragma: no cover - 取决于运行环境是否装 opencv
    cv2 = None  # type: ignore[assignment]
    np = None  # type: ignore[assignment]
    _CV_OK = False


def _png(shape=(600, 800, 3), bg=(45, 42, 40), circles=(), rects=()):
    img = np.zeros(shape, dtype=np.uint8)
    img[:, :] = bg
    for cx, cy, r in circles:
        cv2.circle(img, (cx, cy), r, (0, 40, 230), -1)
    for x0, y0, x1, y1 in rects:
        cv2.rectangle(img, (x0, y0), (x1, y1), (0, 40, 230), -1)
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


class TestFindSendButton(unittest.TestCase):
    @unittest.skipUnless(_CV_OK, "需要 opencv(可选依赖, 最小 CI 不装)")
    def test_finds_red_circle(self):
        cand = vision_cv.find_send_button(_png(circles=[(755, 561, 14)]))
        self.assertIsNotNone(cand)
        self.assertAlmostEqual(cand["x"], 755, delta=2)
        self.assertAlmostEqual(cand["y"], 561, delta=2)
        self.assertGreater(cand["area"], 200)

    @unittest.skipUnless(_CV_OK, "需要 opencv(可选依赖, 最小 CI 不装)")
    def test_rejects_small_dot(self):
        # 未读角标尺寸级别: 面积低于下限应被过滤
        self.assertIsNone(vision_cv.find_send_button(_png(circles=[(100, 100, 5)])))

    @unittest.skipUnless(_CV_OK, "需要 opencv(可选依赖, 最小 CI 不装)")
    def test_rejects_elongated_rect(self):
        # 宽高比越界(红色横幅/文字块)
        self.assertIsNone(
            vision_cv.find_send_button(_png(rects=[(600, 500, 780, 520)]))
        )

    @unittest.skipUnless(_CV_OK, "需要 opencv(可选依赖, 最小 CI 不装)")
    def test_rejects_black_page(self):
        self.assertIsNone(
            vision_cv.find_send_button(_png(bg=(0, 0, 0), circles=[(755, 561, 14)]))
        )

    @unittest.skipUnless(_CV_OK, "需要 opencv(可选依赖, 最小 CI 不装)")
    def test_roi_excludes_outside(self):
        img = _png(circles=[(150, 120, 14)])
        self.assertIsNotNone(vision_cv.find_send_button(img))  # 全帧可见
        roi = (500, 480, 300, 120)  # 输入框右侧条带, 不含 (150,120)
        self.assertIsNone(vision_cv.find_send_button(img, roi=roi))

    @unittest.skipUnless(_CV_OK, "需要 opencv(可选依赖, 最小 CI 不装)")
    def test_prefers_bottom_right_on_ties(self):
        img = _png(circles=[(200, 150, 14), (700, 550, 14)])
        cand = vision_cv.find_send_button(img)
        self.assertGreater(cand["x"], 600)
        self.assertGreater(cand["y"], 400)

    def test_degrades_when_cv2_missing(self):
        orig = vision_cv._AVAILABLE
        vision_cv._AVAILABLE = False
        try:
            self.assertIsNone(vision_cv.find_send_button(b"garbage"))
        finally:
            vision_cv._AVAILABLE = orig

    @unittest.skipUnless(_CV_OK, "需要 opencv(可选依赖, 最小 CI 不装)")
    def test_garbage_bytes(self):
        self.assertIsNone(vision_cv.find_send_button(b"not-a-png"))


if __name__ == "__main__":
    unittest.main()
