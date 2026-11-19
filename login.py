"""CLI 入口薄壳: 转发到 src/login.py(唯一实现)。

2026-09-30 依赖方向单向化: 登录逻辑下沉到 src/login.py(属 src 包内逻辑),
本文件仅保留 CLI 入口与向后兼容 re-export, 供 `python login.py
--password/--manual` 及 `import login` 使用。

历史: 旧 login.py 位于根目录却反向 `from src.config import`, 与
src/session.py → `from login import` 构成双向交叉依赖(src/__init__.py 曾据
此为保持为空)。现入口 → 包 → 根叶模块(logutil/self_heal/spark_status)单向,
无环。
"""

from __future__ import annotations

from src.login import *
from src.login import main

if __name__ == "__main__":
    main()
