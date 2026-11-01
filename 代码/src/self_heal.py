"""自愈模块(2026-10-05 降级): 错误分类 → 本地恢复策略表 → 告警。

历史: 本模块曾用 LLM agent(pi / GLM / AGNES)在运行时**改源码**来"修 bug"。
板子审计(2026-10-05)结论: 自愈**从未成功过一次** —— git 里只有 5 条
`heal: 修复前基线` 与 1 条 `heal: 回滚 (验证未通过)`, 没有一条"修复完成";
`.heal_cache` 三个签名全是 `failed`。根因不是引擎不好, 而是**工具与问题错配**:
真实故障形态(选择器失效 / 渲染挂死 / 限流 / OOM / 登录过期)没有一个是源码 bug;
而每次触发最多要烧 20 分钟预算, 还会把发送窗口挤爆(2026-10-04 事故: 一晚烧穿
timeout → 5 个好友漏发)。同类无人值守项目(Playwright 自愈库 / 运维 watchdog)
的共识做法是「错误分类 → 确定性恢复动作 → 告警」, 故本模块照此降级:

    classify(错误) → policy(类别) → 动作(重试 / reload / 重开会话 / 跳过)
                                    + 无本地手段时告警

零 API 调用、毫秒级返回、完全可预测。**具体动作由调用方执行** —— 只有它手里
有页面与会话句柄(见 `scheduler._send_phase`、`discovery.discover_pending_friends`)。

对外接口(未变):
    self_heal(error_type, error_msg, extra="", screenshot_path="") -> bool
    返回 True = "建议调用方按策略重试"; False = "跳过, 别浪费发送窗口"。
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import time
from pathlib import Path

from logutil import log

try:
    from . import recovery
except ImportError:  # 以顶层模块方式加载时(src/ 在 sys.path 上)
    import recovery  # type: ignore[no-redef]

_PROJECT_DIR = Path(__file__).resolve().parent.parent
# 告警去重账本: 同一错误签名每自然日最多告警 1 次 —— 告警疲劳是无人值守的大忌,
# 每晚重复的同类故障不能淹没告警通道(与旧版冷却同一思路, 只是现在只挡告警)。
_ALERT_FILE = _PROJECT_DIR / "runtime" / ".heal_alerted"

# 动作 → 中文提示(仅用于日志可读性)
_ACTION_HINT = {
    recovery.RETRY: "原样重试",
    recovery.RELOAD_RETRY: "reload 页面后重试",
    recovery.RESTART_SESSION: "重开会话后重试",
    recovery.SKIP: "跳过该对象",
    recovery.ALERT: "跳过并告警(需人工处理)",
}


def _config() -> dict:
    """读 config.json(含机器私有配置, 不入 git); 读取/解析失败返回空 dict。"""
    try:
        return json.loads((_PROJECT_DIR / "config.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _self_heal_enabled() -> bool:
    """本地恢复总开关: env `SPARK_SELF_HEAL=1/0` > `config.json` 的 `self_heal` > 默认开。

    关闭时只把错误写进日志、等人工处理(与降级前语义一致)。第 1 层运行时自愈
    (浏览器崩溃重启、渲染挂死重开会话)与 `run_cron.sh` 的重跑不受本开关影响。
    """
    env = os.environ.get("SPARK_SELF_HEAL", "")
    if env == "1":
        return True
    if env == "0":
        return False
    cfg = _config()
    if "self_heal" in cfg:
        return bool(cfg["self_heal"])
    return True


def _today() -> str:
    return datetime.date.today().isoformat()


def _error_signature(error_type: str, error_msg: str) -> str:
    """错误签名(短指纹) —— 仅作告警去重键, 非安全用途; sha256 以免被弱哈希检查拦下。"""
    raw = f"{error_type}:{error_msg[:100]}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def _alerted_today(sig: str) -> bool:
    try:
        data = json.loads(_ALERT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return False
    return (data.get(sig) or {}).get("day") == _today()


def _record_alert(sig: str) -> None:
    """记账(只保留今日条目, 防文件无限增长); 先写 tmp 再 replace, 与 ctl/ 同一套路。"""
    data: dict = {}
    if _ALERT_FILE.is_file():
        try:
            data = json.loads(_ALERT_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    data = {k: v for k, v in data.items() if v.get("day") == _today()}
    data[sig] = {"day": _today(), "ts": time.time()}
    try:
        _ALERT_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _ALERT_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(_ALERT_FILE)
    except Exception:
        pass


def _alert(error_type: str, error_msg: str, category: str, extra: str) -> None:
    """本地无恢复手段 → 复用 health_check 的 notify_cmd 通道告警(尽力而为)。

    无人值守最坏的结果不是没修好, 而是**没人知道没修好**, 故必须确定性告警;
    但同一签名每日只响一次。未配置 notify_cmd 或执行失败都只记日志, 不影响主流程。
    """
    sig = _error_signature(error_type, error_msg)
    if _alerted_today(sig):
        log(f"[heal] 同类故障 {sig} 今日已告警过, 不重复通知")
        return
    _record_alert(sig)
    msg = (
        "[spark-heal] 故障无本地恢复手段, 需人工处理\n"
        f"错误类型: {error_type}\n类别: {category}\n"
        f"摘要: {error_msg[:200]}\n"
        + (f"补充: {extra[:200]}\n" if extra else "")
        + "下一步: 看 runtime/spark.log; 选择器/页面改版需人工更新 "
        "src/spark_status.py, 修好后 `python spark.py --force` 补跑"
    )
    try:
        try:
            from .health_check import notify
        except ImportError:  # 以顶层模块方式加载 self_heal 时
            from health_check import notify  # type: ignore[no-redef]

        notify(msg)
    except Exception as exc:
        log(f"[heal] 告警发送失败(不影响主流程): {exc}")


def self_heal(
    error_type: str, error_msg: str, extra: str = "", screenshot_path: str = ""
) -> bool:
    """错误分类 → 本地恢复策略 → (无手段时)告警。返回 True 表示建议调用方重试。

    与降级前相比: 不再备份 / 改码 / 验证 / 回滚 / 烧预算, 因此也**不会再拖垮
    发送窗口**。`screenshot_path` 保留仅为兼容调用方签名, 策略判定不需要它。
    """
    if not _self_heal_enabled():
        log(
            "[heal] 本地恢复未启用(config.json 的 self_heal 非 true), 跳过: "
            "仅记录错误等待人工处理"
        )
        return False

    category = recovery.classify(error_type, error_msg)
    action = recovery.policy(category)
    log(
        f"[heal] 分类完成: type={error_type} 类别={category} 动作={action}"
        f"({_ACTION_HINT.get(action, action)})"
    )
    if action == recovery.ALERT:
        _alert(error_type, error_msg, category, extra)
        return False
    return recovery.should_retry(action)
