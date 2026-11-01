"""性能档位: 把「等待/超时/轮询/polling/预算」按机器档次分开。

背景(2026-11-01): 项目最初只为一台 OrangePi Zero3(4 核 / 1.48G, Xvfb 软渲染)准备,
所有等待值都是照着它的下限手调的。同一份代码在本机 WSL(18 核 / 15.7G)跑开发与
dry-run 时, 每个 6 秒上限、每次 500ms 空等全是白烧时间。原 perf_profile 只切
chromium 启动参数(session.build_launch_args), 49 处 timeout/wait_for_timeout 一概
没跟 —— 本模块补上这一层, 让「档位」真正决定节奏。

三档语义:
    low  = 板端档(4 核 / 1.5G, 软件渲染): 上限给足、轮询放宽、停顿留够
    mid  = 中间档(4-8 核 / 8G 笔记本, 无独显): 折中
    high = 桌面档(>=8 核且内存充裕 / 有 GPU): 上限收紧、轮询加快、停顿缩短

关键原则(决定这张表怎么填):
  1. **超时是上限不是固定开销** —— 轮询循环里一命中就 break, 把上限调大对正常路径
     零成本, 真正的差别在「轮询间隔」与「无条件 wait_for_timeout」。故 low 档把上限
     调大(容忍慢渲染), high 档把固定空等和间隔调小(不等没必要的)。
  2. **预算类(budget)按「谁的慢」分**: 慢在抖音侧/网络侧的(list_budget_ms)三档统一
     给足 —— 收窄只会让列表没填充完就开扫, 换不来速度只换来漏发; 只有真正由本机
     算力决定的轮询粒度(list_step_ms / streak_poll_ms)才跟档位缩。
  3. low 列必须**逐项等于 2026-11-01 之前的散落字面量** —— 切到本模块后板端行为
     零变化, 这是能不能安全上生产的前提, tests/test_perf.py 用表逐项卡住。

每一项都能用环境变量单独覆盖(SPARK_PERF_<KNOB 大写>), 用于真机实测微调, 不必改
代码。非法值/超区间一律回落到档位默认值, 不抛异常(生产夜跑不能因为手滑而崩)。
"""

from __future__ import annotations

import os

PROFILES = ("low", "mid", "high")
DEFAULT_PROFILE = "low"

# 各档位数值(毫秒; send_gap_ms 为二元组)。low 列 = 2026-11-01 之前的生产字面量。
_SPECS: dict[str, dict[str, object]] = {
    "low": {
        # —— 会话面板(板端最脆弱一环: 软渲染下 >3s 才渲染出工具栏) ——
        "panel_open_ms": 6000,  # sender.py 原「12 × 500ms」
        "panel_poll_ms": 500,
        "panel_items_ms": 5000,  # 表情项出现(wait_for_selector)
        "panel_wait_ms": 2500,  # scheduler 可信点击后的首段预算
        "contenteditable_ms": 15000,  # scheduler 首次等输入框
        "contenteditable_retry_ms": 8000,  # 合成点击后复检
        # —— 点击 ——
        "click_small_ms": 2000,  # 小控件(发送按钮/登录入口)
        "click_friend_rounds": 4,
        "click_friend_round_ms": 2500,
        "friend_attach_ms": 3000,  # 好友行 wait_for(attached)
        "friend_click_ms": 4000,
        "input_click_ms": 3000,  # 点输入框/登录按钮
        "btn_visible_ms": 1500,  # 发送按钮 wait_for(visible)
        # —— 页面加载 ——
        "goto_ms": 60000,  # 打开聊天页(wait_until=commit, 首次)
        "goto_commit_ms": 120000,  # login 打开聊天页(wait_until=commit)
        "page_dom_ms": 30000,  # domcontentloaded
        "reload_ms": 20000,
        "url_stable_ms": 2500,  # finder 等 URL 落到 /chat
        "login_panel_ms": 8000,  # 登录弹层
        "login_tab_deadline_ms": 60000,
        # —— 固定空等(wait_for_timeout, 真开销) ——
        "settle_ms": 800,  # 打开页面后等 SPA 渲染
        "ui_quiet_ms": 700,  # 点击后等 UI 稳定
        "escape_settle_ms": 300,  # Esc 收起浮层后
        "type_settle_ms": 200,  # 点完输入框、开始键入前
        "btn_ready_ms": 600,  # 注入文本后等按钮由灰变红
        "short_settle_ms": 500,  # 通用短停顿
        "scroll_settle_ms": 600,  # 火花列表滚一屏后等新行渲染
        "post_reload_settle_ms": 1000,
        "heal_settle_ms": 2000,  # 自愈 reload 后
        "login_step_wait_ms": 2000,  # 登录流程每步之间
        # —— 送达确认轮询 ——
        "confirm_first_ms": 300,  # verify_send 首拍
        "confirm_poll_ms": 1000,
        "tail_first_ms": 250,  # 表情面板关闭后首拍
        "tail_poll_ms": 500,
        "identity_retry_ms": 4000,  # 发送前身份核对复检
        # —— 登录页轮询 ——
        "login_content_ms": 12000,  # 等页面出现实质内容(原 24 × 500ms)
        "login_poll_ms": 500,
        "pwd_field_poll_ms": 1000,
        # 等密码框渲染的总预算(轮数 = 预算 // 轮询间隔)。三档统一: 这里是「等抖音
        # 渲染」不是「等本机算力」, 且登录失败代价是当晚锁死。
        "pwd_field_budget_ms": 15000,
        "login_state_poll_ms": 1500,  # 密码登录后等登录态
        # 登录态等待上限。**三档统一**: 收窄会误判「未登录」→ 写 last_fail →
        # 3600s 退避锁死当晚(rc=2), 属账号级不可逆代价, 不该省这 25 秒。
        "login_state_deadline_ms": 45000,
        "manual_poll_ms": 3000,  # 人工登录轮询
        # —— 列表/预算 ——
        "selector_wait_ms": 1500,  # finder.find 单候选等待
        # 会话列表填充总预算。**三档统一, 不随档位缩**: 板端实测最慢 75s 是「抖音侧
        # 渲染慢」不是本机算力, 高档位收窄会让列表没填充完就开扫 → 只扫半张列表 →
        # 静默漏发(2026-09-30 同类事故)。这是超时上限, 给大对正常路径零成本。
        "list_budget_ms": 150000,  # 板端实测最慢 75s, 留一倍余量
        "list_step_ms": 250,
        "streak_wait_ms": 20000,  # 火花图标出现
        "streak_poll_ms": 1000,  # 火花图标轮询间隔
        "streak_scan_ms": 60000,
        # —— 视觉兜底 ——
        "vision_budget_ms": 45000,
        "vision_http_ms": 20000,
        "screenshot_ms": 15000,
        # —— 拟人节奏(好友之间的随机停顿, 毫秒) ——
        "send_gap_ms": (500, 1200),
    },
    "mid": {
        "panel_open_ms": 4500,
        "panel_poll_ms": 300,
        "panel_items_ms": 3500,
        "panel_wait_ms": 2000,
        "contenteditable_ms": 10000,
        "contenteditable_retry_ms": 6000,
        "click_small_ms": 1500,
        "click_friend_rounds": 3,
        "click_friend_round_ms": 1800,
        "friend_attach_ms": 2000,
        "friend_click_ms": 2500,
        "input_click_ms": 2500,
        "btn_visible_ms": 1200,
        "goto_ms": 45000,
        "goto_commit_ms": 90000,
        "page_dom_ms": 25000,
        "reload_ms": 15000,
        "url_stable_ms": 2000,
        "login_panel_ms": 6000,
        "login_tab_deadline_ms": 45000,
        "settle_ms": 500,
        "ui_quiet_ms": 400,
        "escape_settle_ms": 200,
        "type_settle_ms": 150,
        "btn_ready_ms": 400,
        "short_settle_ms": 400,
        "scroll_settle_ms": 450,
        "post_reload_settle_ms": 700,
        "heal_settle_ms": 1500,
        "login_step_wait_ms": 1500,
        "confirm_first_ms": 200,
        "confirm_poll_ms": 700,
        "tail_first_ms": 180,
        "tail_poll_ms": 350,
        "identity_retry_ms": 3000,
        "login_content_ms": 8000,
        "login_poll_ms": 350,
        "pwd_field_poll_ms": 800,
        "pwd_field_budget_ms": 15000,
        "login_state_poll_ms": 1000,
        "login_state_deadline_ms": 45000,
        "manual_poll_ms": 2000,
        "selector_wait_ms": 1000,
        "list_budget_ms": 150000,
        "list_step_ms": 150,
        "streak_wait_ms": 15000,
        "streak_poll_ms": 700,
        "streak_scan_ms": 40000,
        "vision_budget_ms": 30000,
        "vision_http_ms": 15000,
        "screenshot_ms": 12000,
        "send_gap_ms": (400, 1000),
    },
    "high": {
        "panel_open_ms": 3000,
        "panel_poll_ms": 150,
        "panel_items_ms": 2500,
        "panel_wait_ms": 1500,
        "contenteditable_ms": 8000,
        "contenteditable_retry_ms": 4000,
        "click_small_ms": 1000,
        "click_friend_rounds": 3,
        "click_friend_round_ms": 1200,
        "friend_attach_ms": 1500,
        "friend_click_ms": 2000,
        "input_click_ms": 2000,
        "btn_visible_ms": 800,
        "goto_ms": 30000,
        "goto_commit_ms": 60000,
        "page_dom_ms": 20000,
        "reload_ms": 12000,
        "url_stable_ms": 1500,
        "login_panel_ms": 4000,
        "login_tab_deadline_ms": 30000,
        "settle_ms": 250,
        "ui_quiet_ms": 200,
        "escape_settle_ms": 120,
        "type_settle_ms": 100,
        "btn_ready_ms": 300,
        "short_settle_ms": 300,
        "scroll_settle_ms": 300,
        "post_reload_settle_ms": 500,
        "heal_settle_ms": 1000,
        "login_step_wait_ms": 1000,
        "confirm_first_ms": 150,
        "confirm_poll_ms": 500,
        "tail_first_ms": 120,
        "tail_poll_ms": 250,
        "identity_retry_ms": 2500,
        "login_content_ms": 6000,
        "login_poll_ms": 250,
        "pwd_field_poll_ms": 600,
        "pwd_field_budget_ms": 15000,
        "login_state_poll_ms": 800,
        "login_state_deadline_ms": 45000,
        "manual_poll_ms": 1500,
        "selector_wait_ms": 800,
        "list_budget_ms": 150000,
        "list_step_ms": 100,
        "streak_wait_ms": 12000,
        "streak_poll_ms": 500,
        "streak_scan_ms": 30000,
        "vision_budget_ms": 20000,
        "vision_http_ms": 10000,
        "screenshot_ms": 10000,
        "send_gap_ms": (300, 800),
    },
}

# 合法区间(闭区间), 挡掉手滑写错的环境变量。
_BOUNDS: dict[str, tuple[int, int]] = {
    "panel_open_ms": (1000, 30000),
    "panel_poll_ms": (50, 2000),
    "panel_items_ms": (1000, 30000),
    "panel_wait_ms": (500, 15000),
    "contenteditable_ms": (3000, 60000),
    "contenteditable_retry_ms": (2000, 60000),
    "click_small_ms": (500, 15000),
    "click_friend_rounds": (1, 10),
    "click_friend_round_ms": (300, 10000),
    "friend_attach_ms": (300, 15000),
    "friend_click_ms": (300, 15000),
    "input_click_ms": (300, 15000),
    "btn_visible_ms": (300, 15000),
    "goto_ms": (10000, 180000),
    "goto_commit_ms": (10000, 300000),
    "page_dom_ms": (5000, 120000),
    "reload_ms": (5000, 120000),
    "url_stable_ms": (500, 20000),
    "login_panel_ms": (1000, 30000),
    "login_tab_deadline_ms": (5000, 180000),
    "settle_ms": (0, 5000),
    "ui_quiet_ms": (0, 5000),
    "escape_settle_ms": (0, 5000),
    "type_settle_ms": (0, 5000),
    "btn_ready_ms": (0, 5000),
    "short_settle_ms": (0, 5000),
    "scroll_settle_ms": (0, 5000),
    "post_reload_settle_ms": (0, 5000),
    "heal_settle_ms": (0, 20000),
    "login_step_wait_ms": (0, 20000),
    "confirm_first_ms": (0, 5000),
    "confirm_poll_ms": (0, 10000),
    "tail_first_ms": (0, 5000),
    "tail_poll_ms": (0, 10000),
    "identity_retry_ms": (0, 20000),
    "login_content_ms": (2000, 60000),
    "login_poll_ms": (100, 5000),
    "pwd_field_poll_ms": (200, 10000),
    "pwd_field_budget_ms": (5000, 60000),
    "login_state_poll_ms": (200, 10000),
    "login_state_deadline_ms": (30000, 180000),
    "manual_poll_ms": (500, 10000),
    "selector_wait_ms": (200, 20000),
    # 下限卡在 120s: 板端实测最慢 75s, 低于它必然扫半张列表(见 _SPECS 注释)
    "list_budget_ms": (120000, 600000),
    "list_step_ms": (50, 5000),
    "streak_wait_ms": (2000, 120000),
    "streak_poll_ms": (100, 5000),
    "streak_scan_ms": (5000, 300000),
    "vision_budget_ms": (5000, 180000),
    "vision_http_ms": (3000, 60000),
    "screenshot_ms": (3000, 60000),
}

_active = DEFAULT_PROFILE


def normalize(profile: object) -> str:
    """把任意输入规范成合法档位名; 不认识的一律回落到 low(生产档)。"""
    name = str(profile or DEFAULT_PROFILE).strip().lower()
    return name if name in PROFILES else DEFAULT_PROFILE


def set_profile(profile: object) -> str:
    """设置当前进程生效的档位(由 config.load_config 调用), 返回规范名。"""
    global _active
    _active = normalize(profile)
    return _active


def active_profile() -> str:
    """当前生效档位; 环境变量 SPARK_PERF_PROFILE 优先(便于不改配置就试档)。"""
    raw = os.environ.get("SPARK_PERF_PROFILE", "").strip()
    return normalize(raw) if raw else _active


def profile_mismatch(cfg_profile: object) -> str:
    """档位错配提示: SPARK_PERF_PROFILE 只让 perf.get() 走高档, 浏览器资源仍按
    config 的 perf_profile 走(session.build_launch_args 读 cfg)。两者不一致时会得到
    「超时按 high、浏览器仍按 low」这种最坏组合, 必须显式告警而不是静默。

    返回空串表示一致(或没有环境变量覆盖); 否则返回一句可读警告。
    """
    raw = os.environ.get("SPARK_PERF_PROFILE", "").strip()
    if not raw:
        return ""
    env_name = normalize(raw)
    cfg_name = normalize(cfg_profile)
    if env_name == cfg_name:
        return ""
    return (
        f"SPARK_PERF_PROFILE={env_name} 但 config 的 perf_profile={cfg_name}: "
        f"等待/超时按 {env_name}, 浏览器资源仍按 {cfg_name}(session.build_launch_args)。"
        f"要整体试档请改 config 的 perf_profile, 别只设环境变量。"
    )


def knobs() -> tuple[str, ...]:
    """当前档位提供的全部 knob 名(send_gap_ms 另计), 供单测遍历与三档一致性校验。"""
    return tuple(k for k in _SPECS[DEFAULT_PROFILE] if k != "send_gap_ms")


def get(knob: str):
    """取当前档位下的 knob 值; 环境变量 SPARK_PERF_<KNOB> 可覆盖(带区间校验)。

    knob 名不存在 -> KeyError: 编码错误要立刻暴露, 不静默给默认值。
    """
    if knob == "send_gap_ms":
        return _gap()
    if knob not in _SPECS[DEFAULT_PROFILE]:
        raise KeyError(f"未知的性能档位项: {knob}")
    default = _SPECS[active_profile()][knob]
    raw = os.environ.get("SPARK_PERF_" + knob.upper(), "").strip()
    if not raw:
        return default
    lo, hi = _BOUNDS[knob]
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if lo <= value <= hi else default


def ms(knob: str) -> int:
    """取毫秒值并转 int(Playwright 的 timeout= / wait_for_timeout 接受 int)。"""
    return int(get(knob))


def sec(knob: str) -> float:
    """取毫秒值并转秒(供 time.sleep / time.monotonic 预算用)。"""
    return ms(knob) / 1000.0


def _gap() -> tuple[int, int]:
    """好友间停顿区间(毫秒); SPARK_PERF_SEND_GAP_MS="min,max" 可覆盖。"""
    lo, hi = _SPECS[active_profile()]["send_gap_ms"]
    raw = os.environ.get("SPARK_PERF_SEND_GAP_MS", "").strip()
    if raw:
        parts = [p.strip() for p in raw.split(",")]
        if len(parts) == 2:
            try:
                a, b = int(parts[0]), int(parts[1])
                if 0 <= a <= b <= 60000:
                    return (a, b)
            except ValueError:
                pass
    return (int(lo), int(hi))


def send_gap_range_sec() -> tuple[float, float]:
    """好友间停顿区间(秒), 供 scheduler 直接用于 random.uniform。"""
    lo, hi = _gap()
    return (lo / 1000.0, hi / 1000.0)


# 「真睡」knob: 代码里无条件付出的固定等待(不含超时天花板 —— 那些元素一出现
# 就返回, 只有机器慢到超时才吃满)。只有这几个的数字差 = 每档真正省下的时间。
_SLEEP_KEYS = (
    "type_settle_ms",
    "ui_quiet_ms",
    "escape_settle_ms",
    "btn_ready_ms",
    "confirm_first_ms",
    "tail_first_ms",
)
# 单好友一遍固定真睡里各 knob 出现次数(对照 src/sender.py 实际调用链)。
_SLEEP_REPEAT = {
    "type_settle_ms": 1,
    "ui_quiet_ms": 2,
    "escape_settle_ms": 2,
    "btn_ready_ms": 1,
    "confirm_first_ms": 1,
    "tail_first_ms": 1,
}


def friend_sleep_ms(profile: str | None = None) -> int:
    """一个好友走完「找到→打开→输入→发送→确认→收尾」的固定 sleep 合计(ms)。

    这是三档之间**唯一**可以确定省下来的时间; 网络/渲染耗时不在任何档位的控制内。
    """
    s = _SPECS[normalize(profile) if profile else active_profile()]
    return sum(int(s[k]) * _SLEEP_REPEAT[k] for k in _SLEEP_KEYS)


def describe() -> str:
    """一行摘要, 用于启动日志(一眼看出这台机器在跑哪一档、差多少)。"""
    name = active_profile()
    s = _SPECS[name]
    saved = friend_sleep_ms(DEFAULT_PROFILE) - friend_sleep_ms(name)
    return (
        f"perf_profile={name} (panel_open={s['panel_open_ms']}ms/"
        f"poll={s['panel_poll_ms']}ms, settle={s['settle_ms']}ms, "
        f"list_budget={int(s['list_budget_ms']) / 1000:.0f}s, "
        f"gap={s['send_gap_ms'][0]}-{s['send_gap_ms'][1]}ms; "
        f"单好友固定等待 {friend_sleep_ms(name) / 1000:.1f}s"
        f"(板端档 {friend_sleep_ms(DEFAULT_PROFILE) / 1000:.1f}s, 省 {saved / 1000:.1f}s))"
    )


def summary_rows() -> list:
    """三档对照表(供 describe-all / 文档生成): [(knob, low, mid, high), ...]。"""
    rows = []
    for knob in knobs():
        rows.append(
            (knob, _SPECS["low"][knob], _SPECS["mid"][knob], _SPECS["high"][knob])
        )
    rows.append(
        (
            "send_gap_ms",
            _SPECS["low"]["send_gap_ms"],
            _SPECS["mid"]["send_gap_ms"],
            _SPECS["high"]["send_gap_ms"],
        )
    )
    return rows


def describe_all() -> str:
    """三档对照表(纯文本), 供命令行与文档直接引用。"""
    rows = summary_rows()
    width = max(len(str(r[0])) for r in rows) + 2
    out = ["knob".ljust(width) + "low".rjust(10) + "mid".rjust(10) + "high".rjust(10)]
    out.append("-" * (width + 30))
    for knob, lo, mid, hi in rows:

        def fmt(v):
            return f"{v[0]}-{v[1]}" if isinstance(v, tuple) else str(v)

        out.append(
            knob.ljust(width)
            + fmt(lo).rjust(10)
            + fmt(mid).rjust(10)
            + fmt(hi).rjust(10)
        )
    return "\n".join(out)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] in PROFILES:
        set_profile(sys.argv[1])
        print(describe())
    else:
        print(describe_all())
