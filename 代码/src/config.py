"""配置加载、状态持久化、发送窗口判定、环境变量读取。

时间口径: 全项目按「机器本地时」判定(窗口/state 日期/健康检查/日志)。
板子时区为 Asia/Shanghai(CST), cron 00:00 本地时命中窗口 23:30-00:30。
历史版本曾误用 UTC(重构时引入), 2026-08-15 核对板子后按用户决定改回本地时。

测试/生产分离(2026-11-01): SPARK_CONFIG / SPARK_RUNTIME_DIR 两个环境变量
只改**指针**, 不设时 CONFIG_FILE/RUNTIME_DIR/STATE_FILE/LOG_FILE 与旧版逐字节相同
(浏览器参数档位另有 low/high 两套, 见 session.build_launch_args);
本机开发见 config.dev.json + run_dev.sh。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

from logutil import log

from . import perf

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_path(name: str, default: Path) -> Path:
    """读取路径类环境变量: 相对路径按项目根解析, 未设置时用默认值。

    SPARK_CONFIG=config.dev.json   本机开发配置(extends 生产配置 + 桌面档参数)
    SPARK_RUNTIME_DIR=runtime-dev  状态/日志/截图全落开发目录, 不污染 runtime/
    """
    raw = os.environ.get(name)
    if not raw or not raw.strip():
        return default
    path = Path(raw.strip())
    return path if path.is_absolute() else BASE_DIR / path


CONFIG_FILE = _env_path("SPARK_CONFIG", BASE_DIR / "config.json")
RUNTIME_DIR = _env_path("SPARK_RUNTIME_DIR", BASE_DIR / "runtime")
STATE_FILE = RUNTIME_DIR / "state.json"
ENV_FILE = BASE_DIR / ".env"
CHAT_URL = "https://www.douyin.com/chat"

SEND_STICKER = "续火花"

# 复用浏览器(多好友共享同一 chromium 实例, 省冷启动)与其回收阈值。
# 复用实例内存会随好友数单调上涨, 故按剩余内存回收: MemAvailable 低于
# browser_recycle_mem_mb 时关掉实例, 下个好友冷启动重建。
DEFAULT_BROWSER_REUSE = True
DEFAULT_BROWSER_RECYCLE_MB = 300

# 性能档位(2026-11-01): 一张表管两件事 —— ① 浏览器资源上限(见
# session.build_launch_args) ② 全流程等待/超时/轮询/预算(见 perf.py)。
# low=板端(4 核/1.5G, Xvfb 软渲染) mid=普通笔记本 high=桌面(多核/大内存/有 GPU)。
# 默认 low, 生产不写这个字段也仍是板端档位, 保证既有部署零变化。
PERF_PROFILES = perf.PROFILES
DEFAULT_PERF_PROFILE = perf.DEFAULT_PROFILE


def _read_config(path: Path) -> dict:
    """读取单个 JSON 配置文件; 缺失/损坏时按旧口径 sys.exit(1)。"""
    if not path.is_file():
        log(f"错误: 未找到配置文件 {path}")
        log("请先 cp config.example.json config.json 并填写好友与窗口")
        sys.exit(1)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log(f"错误: {path.name} 解析失败: {exc}")
        sys.exit(1)
    if not isinstance(data, dict):
        log(f"错误: {path.name} 顶层必须是 JSON 对象")
        sys.exit(1)
    return data


def load_config() -> dict:
    cfg = _read_config(CONFIG_FILE)
    # extends: 只支持一层(开发配置 extends 生产配置), 避免变成配置继承框架。
    base_name = cfg.pop("extends", None)
    if isinstance(base_name, str) and base_name.strip():
        merged = _read_config(CONFIG_FILE.parent / base_name.strip())
        merged.update(cfg)
        cfg = merged
    if not isinstance(cfg.get("send_window"), dict):
        log("警告: config.json 缺少 send_window 或格式不正确，使用默认 23:30-00:30")
        cfg["send_window"] = {"start": "23:30", "end": "00:30"}
    if "block_resources" not in cfg:
        cfg["block_resources"] = True
    if "browser_reuse" not in cfg:
        cfg["browser_reuse"] = DEFAULT_BROWSER_REUSE
    if "browser_recycle_mem_mb" not in cfg:
        cfg["browser_recycle_mem_mb"] = DEFAULT_BROWSER_RECYCLE_MB
    profile = str(cfg.get("perf_profile") or DEFAULT_PERF_PROFILE).strip().lower()
    if profile not in PERF_PROFILES:
        log(f"警告: perf_profile={profile!r} 不认识, 回落到 {DEFAULT_PERF_PROFILE}")
        profile = DEFAULT_PERF_PROFILE
    cfg["perf_profile"] = profile
    # 档位在此一次性落到 perf 模块, 之后全流程的等待/超时都读它(不再各处写死)。
    perf.set_profile(profile)
    if "accounts" not in cfg:
        log("警告: config.json 缺少 accounts 字段，使用空列表")
        cfg["accounts"] = []
    # 为每个账号补全默认文件名
    for i, acc in enumerate(cfg["accounts"]):
        if "storage_file" not in acc:
            acc["storage_file"] = f"storage_{acc.get('name', f'account{i}')}.json"
        if "state_file" not in acc:
            acc["state_file"] = f"state_{acc.get('name', f'account{i}')}.json"
    return cfg


def get_account_state_file(account: dict) -> Path:
    """获取指定账号的 state 文件路径(优先用配置的 state_file)。"""
    name = account.get("name", "default")
    return RUNTIME_DIR / account.get("state_file", f"state_{name}.json")


def get_account_storage_file(account: dict) -> Path:
    """获取指定账号的 storage 文件路径(优先用配置的 storage_file)。"""
    name = account.get("name", "default")
    return RUNTIME_DIR / account.get("storage_file", f"storage_{name}.json")


def load_state(account: dict | str | None = None) -> dict:
    if account is None:
        state_file = STATE_FILE
    elif isinstance(account, str):
        state_file = RUNTIME_DIR / f"state_{account}.json"
    else:
        state_file = get_account_state_file(account)
    if state_file.is_file():
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "sent" in data:
                return data
        except Exception as exc:
            log(f"[warn] state 文件 {state_file} 损坏, 重置: {exc}")
    return {"date": "", "sent": []}


def write_state(state_or_name: dict | str, state: dict | None = None) -> None:
    """双形态: write_state(state) 旧单账号(写默认 STATE_FILE);
    write_state(account, state) 多账号。"""
    if isinstance(state_or_name, dict) and state is None:
        state_file, sdata = STATE_FILE, state_or_name
    else:
        state_file, sdata = get_account_state_file(state_or_name), state
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sdata, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(state_file))


def in_window(now: datetime, start: str, end: str) -> bool:
    try:
        s = datetime.strptime(start, "%H:%M").time()
        e = datetime.strptime(end, "%H:%M").time()
    except ValueError:
        log(f"[warn] send_window 时间格式不正确: {start}-{end}，按整天处理")
        return True
    t = now.time()
    if s <= e:
        return s <= t <= e
    return t >= s or t <= e


def load_env() -> tuple:
    """从同目录 .env 读取 DOUYIN_ACCOUNT / DOUYIN_PASSWORD。"""
    if not ENV_FILE.is_file():
        return None, None
    account = password = None
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip().upper()
            val = val.strip().strip('"').strip("'")
            if key == "DOUYIN_ACCOUNT" and not account:
                account = val
            elif key == "DOUYIN_PASSWORD" and not password:
                password = val
    except Exception as exc:
        log(f"[登录] 读取 .env 失败: {exc}")
    return (account or None), (password or None)
