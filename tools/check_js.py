#!/usr/bin/env python3
"""把所有源码里像 JS 的字符串常量抓出来, 交给 node --check 验语法。

动机 (2026-11-02 板端真实事故):
    生产日志一晚出现 6 次
        [发送] 策略A JS注入异常: Page.evaluate: SyntaxError: Invalid or unexpected token
    真相是 src/sender.py 的 _JS_INJECT_AND_SEND 里, 注释中写了字面量 反斜杠n续火花,
    Python 解析三引号时把它变成真换行, JS 收到注释里断行 → 语法错误。
    因为下面还有三层回落, 结果只是慢一点, 主路径坏了整整一晚没人发现。

    JS 语法错误必须是构建期就炸的东西, 不能靠生产日志碰运气。
    这个脚本同时被 tests/test_js_syntax.py 调用(没有 node 时跳过)。

覆盖范围 (2026-11-19 扩大):
    早先这里写死 7 个文件名, 于是 src/spark_status.py / src/stealth.py /
    src/login.py 里的 JS 从来没被校验过 —— 而 spark_status 的四条发现策略和
    stealth 的注入脚本, 恰恰是「跑在浏览器里、坏了只在生产日志现形」的那一类。
    现在改成扫描 src/ 下所有 .py 加上仓库根的几个入口模块, 新加的模块自动纳入,
    不需要再回来改这份清单。

    同时补了一类以前抓不到的东西: 直接写在调用里的内联脚本, 例如
        page.evaluate 三引号包住的 () => document.title
    它既不是赋值常量, 也不经过任何 _JS_ 变量, 旧扫描只看 ast.Assign, 完全漏掉。
"""

from __future__ import annotations

import ast
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent

# 目录: 递归收里面的所有 .py。
SOURCE_DIRS = ("src",)
# 根目录下的模块: 只收这几个, 免得把临时脚本/测试也卷进来。
ROOT_FILES = ("spark.py", "login.py", "logutil.py", "self_heal.py")

# 拼接片段不是完整程序, 单独校验必然假报错
FRAGMENTS = {"_JS_HEAD", "JS_HEAD", "_PY", "__doc__"}
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

# 这些方法的第一位参数就是要在浏览器里跑的脚本
JS_CALL_ATTRS = {"evaluate", "evaluate_handle", "add_init_script", "add_script_tag"}


def discover(root: pathlib.Path = ROOT) -> list[str]:
    """返回要扫描的相对路径列表(排序后), 只含真实存在的文件。"""
    found: list[str] = []
    for d in SOURCE_DIRS:
        base = root / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.py")):
            rel = p.relative_to(root).as_posix()
            if "__pycache__" in rel:
                continue
            found.append(rel)
    for name in ROOT_FILES:
        if (root / name).is_file():
            found.append(name)
    return found


def looks_like_js(value: str) -> bool:
    if len(value) < 40 or "(" not in value:
        return False
    return sum(1 for m in MARKERS if m in value) >= 2


def _const_str(node: ast.AST):
    """尽量把节点还原成字符串常量; 不是常量就返回 None。"""
    try:
        value = ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError):
        return None
    return value if isinstance(value, str) else None


def iter_js(root: pathlib.Path = ROOT, targets=None):
    """产出 (相对路径, 名字, 源码文本)。名字形如 _JS_XXX 或 evaluate@123。"""
    for rel in targets if targets is not None else discover(root):
        path = root / rel
        if not path.exists():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            print("  [FAIL] " + rel + " 无法解析: " + str(exc), file=sys.stderr)
            continue
        for node in ast.walk(tree):
            # 一类: 赋值给名字的 JS 常量
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if not isinstance(tgt, ast.Name) or tgt.id in FRAGMENTS:
                        continue
                    value = _const_str(node.value)
                    if value is not None and looks_like_js(value):
                        yield rel, tgt.id, value
            # 二类: 直接写在调用里的内联脚本 page.evaluate(三引号...)
            elif isinstance(node, ast.Call):
                func = node.func
                if not isinstance(func, ast.Attribute):
                    continue
                if func.attr not in JS_CALL_ATTRS or not node.args:
                    continue
                value = _const_str(node.args[0])
                if value is None or not looks_like_js(value):
                    continue
                yield rel, func.attr + "@" + str(node.lineno), value


def node_available() -> bool:
    return shutil.which("node") is not None


def check_all(root: pathlib.Path = ROOT, targets=None) -> list[str]:
    """返回失败描述列表; 空列表 = 全部通过。没有 node 时抛 RuntimeError。"""
    if not node_available():
        raise RuntimeError("找不到 node, 无法校验 JS 语法")
    failures: list[str] = []
    for rel, name, value in iter_js(root, targets):
        if not value:
            continue
        with tempfile.NamedTemporaryFile(
            "w", suffix=".js", delete=False, encoding="utf-8"
        ) as fh:
            fh.write(value)
            tmp = pathlib.Path(fh.name)
        try:
            proc = subprocess.run(
                ["node", "--check", str(tmp)],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode != 0:
                first = (proc.stderr or "").strip().splitlines()
                detail = first[0] if first else "node --check 失败但没输出 stderr"
                failures.append(f"{rel}::{name}  {detail}")
        finally:
            tmp.unlink(missing_ok=True)
    return failures


def main() -> int:
    targets = discover()
    segments = list(iter_js(targets=targets))
    failures = check_all(targets=targets)
    for line in failures:
        print("  [FAIL] " + line)
    print(
        "扫描 "
        + str(len(targets))
        + " 个文件, 检查 "
        + str(len(segments))
        + " 段 JS, "
        + str(len(failures))
        + " 段语法错误"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
