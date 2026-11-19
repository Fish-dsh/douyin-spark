#!/usr/bin/env python3
"""把所有源码里像 JS 的字符串常量抓出来, 交给 node --check 验语法。

动机 (2026-11-02 板端真实事故):
    生产日志一晚出现 6 次
        [发送] 策略A JS注入异常: Page.evaluate: SyntaxError: Invalid or unexpected token
    真相是 src/sender.py 的 _JS_INJECT_AND_SEND 里, 注释中写了字面量 "\\n续火花",
    Python 解析三引号时把它变成真换行, JS 收到"注释里断行" → 语法错误。
    因为下面还有三层回落, 结果只是"慢一点", 主路径坏了整整一晚没人发现。

    JS 语法错误必须是构建期就炸的东西, 不能靠生产日志碰运气。
    这个脚本同时被 tests/test_js_syntax.py 调用(没有 node 时跳过)。
"""

from __future__ import annotations

import ast
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
TARGETS = (
    "src/sender.py",
    "src/discovery.py",
    "src/finder.py",
    "src/vision.py",
    "src/session.py",
    "src/scheduler.py",
    "src/self_heal.py",
)

# 拼接片段不是完整程序, 单独校验必然假报错
FRAGMENTS = {"_JS_HEAD", "JS_HEAD", "_PY"}
MARKERS = (
    "=>",
    "function",
    "document.",
    "querySelector",
    "const ",
    "let ",
    "return ",
    "addEventListener",
    "getBoundingClientRect",
)


def looks_like_js(value: str) -> bool:
    if len(value) < 40 or "(" not in value:
        return False
    return sum(1 for m in MARKERS if m in value) >= 2


def iter_js(root: pathlib.Path = ROOT, targets=TARGETS):
    """产出 (相对路径, 常量名, 源码文本)。"""
    for rel in targets:
        path = root / rel
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for tgt in node.targets:
                if not isinstance(tgt, ast.Name) or tgt.id in FRAGMENTS:
                    continue
                try:
                    value = ast.literal_eval(node.value)
                except (ValueError, SyntaxError):
                    continue
                if isinstance(value, str) and looks_like_js(value):
                    yield rel, tgt.id, value


def node_available() -> bool:
    return shutil.which("node") is not None


def check_all(root: pathlib.Path = ROOT, targets=TARGETS) -> list[str]:
    """返回失败描述列表; 空列表 = 全部通过。没有 node 时抛 RuntimeError。"""
    if not node_available():
        raise RuntimeError("找不到 node, 无法校验 JS 语法")
    failures: list[str] = []
    for rel, name, value in iter_js(root, targets):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".js", delete=False, encoding="utf-8"
        ) as fh:
            fh.write(value)
            tmp = pathlib.Path(fh.name)
        try:
            proc = subprocess.run(
                ["node", "--check", str(tmp)], capture_output=True, text=True, check=False
            )
            if proc.returncode != 0:
                first = (proc.stderr or "").strip().splitlines()
                failures.append(f"{rel}::{name}  {first[0] if first else '?'}")
        finally:
            tmp.unlink(missing_ok=True)
    return failures


def main() -> int:
    total = sum(1 for _ in iter_js())
    failures = check_all()
    for line in failures:
        print(f"  [FAIL] {line}")
    print(f"检查 {total} 段 JS, {len(failures)} 段语法错误")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
