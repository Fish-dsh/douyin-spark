"""douyin-spark 核心模块包。

注意: 本文件刻意保持为空(不再导出子模块符号)。早期版本在包初始化时
急切再导出 scheduler/session 等, 而当时的根 `login.py` 需要导入 `src.config`,
形成循环: login → src.config → src/__init__ → scheduler → session → login。
2026-09-30 登录逻辑已下沉为 `src/login.py`(根 `login.py` 仅剩 CLI 壳), 该环
已消除; 本文件仍保持为空, 各模块请直接 `from src.xxx import ...` 或包内相对
导入。
"""
