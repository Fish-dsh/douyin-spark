#!/usr/bin/env python3
"""import_browser_cookies.py · 把浏览器里已登录的抖音会话导成 Playwright storage 文件。

为什么需要它(2026-11-20):
    在此之前唯一拿到登录态的办法是 login.py --manual, 也就是在板子/桌面上弹一个
    浏览器让人扫码。想复用「自己日常浏览器里已经登好的抖音」则一路都是死路:
      · 直接读 Cookies SQLite: Chrome/Edge 127+ 起是 ABE(v20) 加密, 密钥被 DPAPI
        包着, 解包必须调 IElevator COM, 而 IElevator 只认浏览器安装目录内的调用方;
      · 开 --remote-debugging-port 连现有浏览器: 官方硬拒, 报
        "DevTools remote debugging requires a non-default data directory";
      · 复制整个 profile 到临时目录再开: 浏览器启动时会把解不开的 v20 cookie
        直接删掉, 复制品里 douyin cookie 归零。
    唯一活路是让**浏览器自己**把明文交出来: 临时加载一个 MV3 扩展, 用
    chrome.cookies API 读(解密发生在浏览器地址空间内, 与 ABE 无关), POST 回本地
    端口。本脚本就是这条路的成品。

它做什么:
    1. 在临时目录写一个只申请 cookies 权限的扩展;
    2. 用**真实的** user-data-dir 启动 Edge/Chrome 并 --load-extension;
    3. 收下扩展 POST 回来的 cookie, 转成 Playwright storage_state 结构;
    4. 写进 runtime/storage_<name>.json —— 主链路(login.restore_state)直接就能吃,
       不需要改任何发送代码。

它不做什么:
    · 不读、不解、不写 cookie 密文, 不碰 Local State;
    · 不复制 profile, 不动用户的书签/历史/扩展;
    · 不绕验证码、不做协议逆向(与 AGENTS.md 的硬约束一致)。

用法:
    python tools/import_browser_cookies.py --account main      # 导到 main 账号的 storage
    python tools/import_browser_cookies.py --browser chrome    # 指定浏览器(默认 edge)
    python tools/import_browser_cookies.py --output /tmp/s.json

前置条件(踩过坑, 会直接报错):
    · 目标浏览器必须**完全退出** —— 只要有残留进程, 新启动会移交给老进程,
      --load-extension 不生效, 于是收不到任何 cookie;
    · 用的是浏览器日常 profile, 所以 cookie 里的会话就是那个人工登录的会话。

退出码: 0 导出成功 / 1 找不到浏览器或没拿到 cookie / 2 参数或环境不对
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import CHAT_URL, RUNTIME_DIR, get_account_storage_file, load_config

# 判定「真的登进去了」的几个 cookie。缺 sessionid 的导出没有意义。
AUTH_COOKIES = ("sessionid", "sessionid_ss", "sid_tt", "uid_tt")

# Chrome 的 cookie 库 sameSite 取值 → Playwright 认识的写法。
_SAMESITE = {
    "no_restriction": "None",
    "lax": "Lax",
    "strict": "Strict",
    "unspecified": "Lax",
    "none": "None",
}


def base_domain(host: str) -> str:
    """把 www.douyin.com 收敛成 douyin.com —— cookie 域的锚点。"""
    parts = [p for p in host.split(".") if p]
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def cookie_domains(host: str) -> list[str]:
    """扩展要逐个问的域名。带点与不带点各问一次, 否则会漏掉 host-only cookie。"""
    base = base_domain(host)
    out = [base, f".{base}"]
    if host not in out:
        out.insert(0, host)
    return out


def browser_candidates(browser: str) -> tuple[list[Path], list[Path]]:
    """返回 (可执行文件候选, user-data-dir 候选)。按平台给, 不做 which 探测。"""
    home = Path.home()
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local"))
        pf = Path(os.environ.get("PROGRAMFILES", "C:/Program Files"))
        pf86 = Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)"))
        if browser == "edge":
            exes = [
                pf86 / "Microsoft/Edge/Application/msedge.exe",
                pf / "Microsoft/Edge/Application/msedge.exe",
            ]
            udds = [local / "Microsoft/Edge/User Data"]
        else:
            exes = [
                pf / "Google/Chrome/Application/chrome.exe",
                pf86 / "Google/Chrome/Application/chrome.exe",
                local / "Google/Chrome/Application/chrome.exe",
            ]
            udds = [local / "Google/Chrome/User Data"]
        return exes, udds
    if sys.platform == "darwin":
        if browser == "edge":
            return (
                [
                    Path(
                        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
                    )
                ],
                [home / "Library/Application Support/Microsoft Edge"],
            )
        return (
            [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")],
            [home / "Library/Application Support/Google Chrome"],
        )
    if browser == "edge":
        return (
            [Path("/usr/bin/microsoft-edge"), Path("/usr/bin/microsoft-edge-stable")],
            [home / ".config/microsoft-edge"],
        )
    return (
        [Path("/usr/bin/google-chrome"), Path("/usr/bin/google-chrome-stable")],
        [home / ".config/google-chrome"],
    )


def find_browser(
    browser: str, exe_override: str | None, udd_override: str | None
) -> tuple[Path, Path]:
    """挑一个真实存在的浏览器 + profile 目录; 找不到就报清楚该给什么。"""
    exes, udds = browser_candidates(browser)
    exe = (
        Path(exe_override)
        if exe_override
        else next((p for p in exes if p.is_file()), None)
    )
    udd = (
        Path(udd_override)
        if udd_override
        else next((p for p in udds if p.is_dir()), None)
    )
    if exe is None or not Path(exe).is_file():
        raise SystemExit(
            f"未找到 {browser} 可执行文件, 试过: {', '.join(str(p) for p in exes)}\n"
            "用 --exe 指定路径, 例如 --exe 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe'"
        )
    if udd is None or not Path(udd).is_dir():
        raise SystemExit(
            f"未找到 {browser} 的 user-data-dir, 试过: {', '.join(str(p) for p in udds)}\n"
            "用 --profile 指定, 例如 --profile '%LOCALAPPDATA%\\Microsoft\\Edge\\User Data'"
        )
    return Path(exe), Path(udd)


def build_manifest(domains: list[str]) -> dict:
    """只申请 cookies 一个权限 —— 权限越大越容易被浏览器拦下。"""
    base = base_domain(domains[0])
    return {
        "manifest_version": 3,
        "name": "spark cookie export",
        "version": "1.0",
        "permissions": ["cookies"],
        "host_permissions": [
            f"*://*.{base}/*",
            "http://127.0.0.1/*",
        ],
        "background": {"service_worker": "bg.js"},
    }


def build_worker(domains: list[str], port: int) -> str:
    """service worker: 逐域读 cookie, 去重, POST 回本地端口。"""
    return (
        "async function grab() {\n"
        "  const out = [];\n"
        f"  for (const d of {json.dumps(domains)}) {{\n"
        "    try {\n"
        "      for (const c of await chrome.cookies.getAll({ domain: d })) out.push(c);\n"
        "    } catch (e) {}\n"
        "  }\n"
        "  const seen = new Set();\n"
        "  const uniq = [];\n"
        "  for (const c of out) {\n"
        '    const k = c.domain + "|" + c.name + "|" + c.path;\n'
        "    if (seen.has(k)) continue;\n"
        "    seen.add(k);\n"
        "    uniq.push(c);\n"
        "  }\n"
        "  for (let i = 0; i < 3; i++) {\n"
        "    try {\n"
        f'      await fetch("http://127.0.0.1:{port}/cookies", {{\n'
        '        method: "POST",\n'
        '        headers: { "Content-Type": "application/json" },\n'
        "        body: JSON.stringify(uniq),\n"
        "      });\n"
        "      return;\n"
        "    } catch (e) {\n"
        "      await new Promise((r) => setTimeout(r, 500));\n"
        "    }\n"
        "  }\n"
        "}\n"
        "grab();\n"
    )


def running_count(exe: Path) -> int:
    """数一下目标浏览器还有几个进程活着。

    为什么要在起浏览器之前问这一句: 同一个 user-data-dir 已经有实例时, 新进程只会
    把命令行参数移交给老进程然后自己退出 —— --load-extension 静默不生效, 于是要干等
    到超时才报错。提前问一句, 报错就变成"先把浏览器关干净", 而不是"90 秒后超时"。
    数不出来时返回 0(宁可放过, 不要因为 tasklist/pgrep 缺失就挡住正常导出)。
    """
    name = exe.name
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH"],
                capture_output=True,
                text=True,
                # 中文 Windows 的 tasklist 会输出本地化提示语, 按 cp936 硬解会炸
                errors="replace",
                timeout=15,
                check=False,
            ).stdout
            return sum(1 for line in out.splitlines() if name.lower() in line.lower())
        out = subprocess.run(
            ["pgrep", "-fc", str(exe)],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=15,
            check=False,
        ).stdout.strip()
        return int(out or 0)
    except Exception:
        return 0


def free_port() -> int:
    """让内核挑一个空闲端口, 避免和板子上的服务撞车。"""
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def to_storage_state(cookies: list[dict]) -> dict:
    """Chrome 的 cookie 对象 → Playwright storage_state。

    字段名不一样(expirationDate vs expires), sameSite 取值也不一样, 这里只做映射,
    不猜缺失值: 没给 expirationDate 的按会话 cookie(-1)处理。
    """
    out = []
    for ck in cookies:
        name = ck.get("name")
        domain = ck.get("domain")
        if not name or not domain:
            continue
        # 会话 cookie 的 expirationDate 可能是 null 而不是缺键 —— 直接 .get 默认值
        # 抓不到这种情况, 会写出 "expires": null 让 Playwright 拒收整个文件。
        expires = ck.get("expirationDate")
        out.append(
            {
                "name": name,
                "value": ck.get("value", ""),
                "domain": domain,
                "path": ck.get("path") or "/",
                "expires": -1 if expires is None else expires,
                "httpOnly": bool(ck.get("httpOnly")),
                "secure": bool(ck.get("secure")),
                "sameSite": _SAMESITE.get(
                    str(ck.get("sameSite", "unspecified")).lower(), "Lax"
                ),
            }
        )
    return {"cookies": out, "origins": []}


def resolve_output(account: str | None, explicit: str | None) -> Path:
    """输出路径优先级: --output > 配置里同名账号的 storage_file > runtime/storage_<name>.json。

    跟着配置走的好处: 账号改了 storage_file, 这里不用改。
    """
    if explicit:
        return Path(explicit)
    name = account or "imported"
    try:
        cfg = load_config()
    except SystemExit:
        cfg = {}
    for acc in cfg.get("accounts") or []:
        if acc.get("name") == name:
            return get_account_storage_file(acc)
    return RUNTIME_DIR / f"storage_{name}.json"


def collect(exe: Path, udd: Path, domains: list[str], timeout: float) -> list[dict]:
    """起浏览器 → 等扩展回传 → 杀进程树。返回原始 cookie 列表。"""
    ext_dir = Path(tempfile.mkdtemp(prefix="spark-cookie-ext-"))
    port = free_port()
    (ext_dir / "manifest.json").write_text(
        json.dumps(build_manifest(domains), ensure_ascii=False), encoding="utf-8"
    )
    (ext_dir / "bg.js").write_text(build_worker(domains, port), encoding="utf-8")

    got: dict = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            try:
                got["cookies"] = json.loads(raw)
            except Exception as exc:
                got["error"] = str(exc)
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b"ok")
            done.set()

        def do_OPTIONS(self):
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    args = [
        str(exe),
        f"--user-data-dir={udd}",
        f"--load-extension={ext_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--lang=zh-CN",
    ]
    kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(args, **kwargs)
    try:
        done.wait(timeout)
    finally:
        kill_tree(proc)
        server.shutdown()
        shutil.rmtree(ext_dir, ignore_errors=True)
    if not done.is_set():
        raise SystemExit(
            f"{timeout:.0f} 秒内没收到扩展回传。最常见的原因是浏览器已经在运行 ——\n"
            "同一个 user-data-dir 只会移交参数给老进程, --load-extension 不生效。\n"
            "把目标浏览器完全退出(任务管理器里也要干净)后重跑。"
        )
    return got.get("cookies") or []


def kill_tree(proc: subprocess.Popen) -> None:
    """按进程树杀 —— 浏览器会派生一堆子进程, 只杀父进程会留下孤儿。"""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
        return
    try:
        os.killpg(os.getpgid(proc.pid), 15)
    except Exception:
        proc.terminate()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把浏览器里已登录的抖音会话导成 Playwright storage 文件",
    )
    parser.add_argument(
        "--browser",
        choices=("edge", "chrome"),
        default="edge",
        help="从哪个浏览器取(默认 edge)",
    )
    parser.add_argument(
        "--account",
        help="写到这个账号的 storage 文件(默认 runtime/storage_imported.json)",
    )
    parser.add_argument("--output", help="直接指定输出路径")
    parser.add_argument("--profile", help="浏览器的 user-data-dir(默认按平台推断)")
    parser.add_argument("--exe", help="浏览器可执行文件(默认按平台推断)")
    parser.add_argument(
        "--timeout", type=float, default=90.0, help="等扩展回传的秒数(默认 90)"
    )
    parser.add_argument(
        "--force", action="store_true", help="浏览器还在跑也硬来(通常只会白等)"
    )
    args = parser.parse_args(argv)

    exe, udd = find_browser(args.browser, args.exe, args.profile)
    alive = running_count(exe)
    if alive and not args.force:
        print(f"检测到 {alive} 个 {exe.name} 进程还在运行。")
        print(
            "同一个配置档已有实例时, 新窗口只会把参数移交给它, --load-extension 不生效。"
        )
        print(
            "请把那个浏览器完全退出(任务管理器里也要干净)后重跑; 确认要试就加 --force。"
        )
        return 2
    host = urlparse(CHAT_URL).hostname or "www.douyin.com"
    domains = cookie_domains(host)
    print(f"浏览器: {exe}")
    print(f"配置档: {udd}")
    print(f"域名  : {', '.join(domains)}")

    cookies = collect(exe, udd, domains, args.timeout)
    state = to_storage_state(cookies)
    names = {c["name"] for c in state["cookies"]}
    missing = [n for n in AUTH_COOKIES if n not in names]

    out = resolve_output(args.account, args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"cookie: {len(state['cookies'])} 条 → {out}")
    print(
        f"登录态: {', '.join(n for n in AUTH_COOKIES if n in names) or '（一个都没读到）'}"
    )
    if missing:
        print(f"缺少  : {', '.join(missing)} —— 那个浏览器里可能没登录抖音")
        return 1
    print("下一步: 直接跑 spark.py, 它启动时会 restore_state 读这个文件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
