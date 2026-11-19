#!/usr/bin/env python3
"""strat_stats.py · 策略健康度统计(离线日志分析, ChatGPT 外部评审 round-2 ③)。

回答一个问题: 「哪一层在养活整个系统」。发送链路是多层的(表情→文字A→文字B;
定位是策略1..N 逐个试), 但删哪层、留哪层过去靠印象。本工具只解析生产日志
runtime/spark.log 里**已经存在**的行, 不要求热路径埋任何计数器:

    [定位] 策略『X』命中并点击了...          → 定位层命中
    [定位] 第N轮 策略『X』未命中: ...        → 定位层未命中
    [发送] 生效策略: 表情已发送...            → 发送层: 表情 赢
    [发送] 生效策略: 表情已发送(降级 ...      → 发送层: 表情赢但**无新增证据**(见下)
    [发送] 生效策略: 表情面板已关闭但无任何新增…判未发送 → 表情点了个空(旧代码会算成功)
    [发送] 生效策略: 表情失败...              → 发送层: 表情 输
    [发送] 生效策略: 策略A 生效... / 策略B... → 发送层: 文字A/文字B 赢
    [确认] 确认成功: 新增气泡 3->4 ...         → 文字层: 有「这条消息是新出现的」硬证据
    [确认] 确认成功(降级 ...                   → 文字层: 证据读不到, 按旧口径放行
    [确认] 未能确认发送 (...)                  → 文字层: 判失败(会走视觉兜底)
    [确认] 文字层基线: 气泡数=None              → 基线都没读到, 这一晚的确认全是降级
    [核对] 身份核对: 目标『X』在当前会话面板确认 → 闸门真读到了面板
    [核对] 身份核对: 面板未渲染...             → 闸门读不到(放行, 等于这一秒没闸门)
    [核对] 身份核对冲突: ...                   → 闸门拦下了发送
    [cv_fallback] ... detail=xxx              → 视觉兜底这一层的失败归因

按天分桶统计每层「出场/命中/最终赢」, 出场多而从不养活下游的层 = 删除候选;
首层赢下 95% 的仗 = 后面几层是纯保险, 改动它们要格外谨慎。
闸门单独成一段: 它只需要回答「昨天那 N 次核对里, 有几次是真判出来的」——
盲放行非零就说明 root 判据对当前布局还有漏配, 冲突拦截非零就得逐个去手机端复核。
确认单独成一段(2026-10-07 同型教训): 「成功」里有多少是证据判出来的、多少是读不到
证据放行的, 必须分开看。降级比例持续 100% = 这一层等于没验证, 与闸门恒盲同罪。

用法:
    .venv/bin/python tools/strat_stats.py                    # 默认 runtime/spark.log
    .venv/bin/python tools/strat_stats.py --days 7 a.log     # 只看最近 7 天
    .venv/bin/python tools/strat_stats.py --all --days 30    # 连轮转出的 .gz 一起读(月度回看)
只读工具, 不写任何状态; 测试日志会混在同一文件里(好友名带「测试」), 需自行 grep 排除或留意量级。
"""

from __future__ import annotations

import argparse
import gzip
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

_TS = re.compile(r"^\[(\d{4}-\d{2}-\d{2}) ")
_ROTATED_NAME = re.compile(r"spark\.log(?:\.\d+)?(?:\.gz)?")
_LOC_HIT = re.compile(r"\[定位\] 策略『(.+?)』命中")
_LOC_MISS = re.compile(r"\[定位\] .*策略『(.+?)』未命中")
_EFF = re.compile(r"\[发送\] 生效策略: (.+)")
_CV = re.compile(r"\[cv_fallback\] used=(\d+) found=(\d+) clicked=(\d+) verified=(\d+)")
_CV_DETAIL = re.compile(r"\[cv_fallback\] .*?detail=([A-Za-z_][A-Za-z0-9_]*)")
_GATE = re.compile(r"\[核对\] (.*)")
_FAIL = re.compile(r"\[失败\] (.*)")
_CONFIRM = re.compile(r"\[确认\] (.*)")


# 闸门只回答一个问题: 这次核对是**真判出来的**, 还是读不到而放行。
# 2026-10-07 的事故教训: root 判据错 → 17/17 恒判「无法判定, 放行」= 闸门自上线即瞎,
# 而当时的日志里这个事实是可见的, 只是没人把它统计出来。
def _classify_gate(detail: str) -> str | None:
    if detail.startswith("身份核对: 目标"):
        return "确认"
    if detail.startswith("身份核对冲突"):
        return "冲突拦截(复检后)" if "复检后仍判冲突" in detail else "冲突拦截(单次)"
    if detail.startswith("身份核对: 面板未渲染"):
        return "盲放行(复检后)" if "已复检" in detail else "盲放行(首读即盲)"
    if detail.startswith(("身份核对不可用", "身份核对返回异常")):
        return "核对自身异常"
    if detail.startswith("可信点击未打开面板"):
        return "抢救定位也失败" if "合成点击定位失败" in detail else "需合成点击抢救"
    return None


# [失败] 行按关键短语归到结局类别; 注意「转文字兜底/转视觉兜底」不是终态, 只是下一层的入口,
# 放在一起看是为了算「每一层各接了多少活」。
_FAIL_KINDS = (
    ("未能定位到好友", "定位失败"),
    ("会话面板两次等待", "面板未挂载"),
    ("发送前身份核对未通过", "核对拦截跳过"),
    ("表情发送失败", "转文字兜底"),
    ("发送未确认成功", "转视觉兜底"),
    ("视觉兜底也未成功", "视觉兜底失败"),
)


def _classify_fail(detail: str) -> str | None:
    for kw, name in _FAIL_KINDS:
        if kw in detail:
            return name
    return None


def _classify_send(detail: str) -> str | None:
    # 「表情已发送」有两种成色: 带尾部新增证据的硬确认, 和读不到证据只凭面板关闭的
    # 降级确认(2026-10-07 审计: 旧代码把后者当成前者, 于是「表情层成功」从不被验证过)。
    # 降级比例必须单独看得见, 否则这一层的失证会一直伪装成 100% 赢率。
    if detail.startswith("表情已发送(降级"):
        return "表情(降级)"
    if detail.startswith("表情已发送"):
        return "表情"
    if detail.startswith("表情面板已关闭但"):
        # 面板关了但列表一条没多: 表情点了个空 —— 以前这条路会返回「成功」并写 state
        return "表情判空"
    if detail.startswith("表情失败"):
        return "表情!"
    if detail.startswith("策略A 生效"):
        return "文字A"
    if detail.startswith("策略B 生效"):
        return "文字B"
    if detail.startswith(("策略A/B均失败", "未定位到输入框")):
        return "文字全败"
    return None


# [确认] 只回答一个问题: 这次「发送成功」是**证据判出来的**, 还是读不到证据放行的。
# 与身份闸门同一类失效形态(判据恒默认 = 恒「成功」), 所以同样单独成段。
def _classify_confirm(detail: str) -> str | None:
    if detail.startswith("确认成功: "):
        return "硬证据确认"
    if detail.startswith("确认成功(降级"):
        return "降级确认(证据不可读)"
    if detail.startswith("未能确认发送"):
        return "未能确认"
    if detail.startswith("确认发送时出错"):
        return "确认异常"
    if detail.startswith("文字层基线"):
        return "基线读不到" if "气泡数=None" in detail else "基线可读"
    return None


def _open_log(f: Path):
    """logrotate 的 compress 会把隔天日志压成 .gz; 不认它等于白留那些历史。"""
    if f.suffix == ".gz":
        return gzip.open(f, "rt", encoding="utf-8", errors="replace")
    return f.open(encoding="utf-8", errors="replace")


def parse(files: list[Path]) -> dict[str, dict[str, int]]:
    """→ {("2026-10-05", "定位/get_by_text(exact)"): n, ...} 计数表。"""
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for f in files:
        try:
            fh = _open_log(f)
        except OSError as exc:
            print(f"[strat_stats] 跳过 {f}: {exc}", file=sys.stderr)
            continue
        day = "?"
        with fh:
            for line in fh:
                m = _TS.match(line)
                if m:
                    day = m.group(1)
                m = _CV.search(line)
                if m:
                    used, found, clicked, verified = (int(g) for g in m.groups())
                    counts[(day, "cv/used")] += used
                    counts[(day, "cv/found")] += found
                    counts[(day, "cv/clicked")] += clicked
                    if verified:
                        counts[(day, "发送/视觉兜底✓")] += 1
                        counts[(day, "发送/总会话数")] += 1
                    elif used:
                        counts[(day, "发送/视觉兜底✗")] += 1
                    dm = _CV_DETAIL.search(line)
                    if dm:
                        counts[(day, f"cv归因/{dm.group(1)}")] += 1
                    continue
                m = _LOC_HIT.search(line)
                if m:
                    counts[(day, f"定位/{m.group(1)}✓")] += 1
                    counts[(day, "定位/总会话数")] += 1
                    continue
                m = _LOC_MISS.search(line)
                if m:
                    counts[(day, f"定位/{m.group(1)}!")] += 1
                    continue
                m = _EFF.search(line)
                if m:
                    cls = _classify_send(m.group(1).strip())
                    if cls in ("表情", "表情(降级)", "文字A", "文字B"):
                        counts[(day, f"发送/{cls}✓")] += 1
                        counts[(day, "发送/总会话数")] += 1
                    elif cls in ("表情!", "表情判空"):
                        counts[(day, f"发送/{cls}✗")] += 1
                    elif cls == "文字全败":
                        counts[(day, "发送/文字兜底也失败✗")] += 1
                    continue
                m = _GATE.search(line)
                if m:
                    name = _classify_gate(m.group(1).strip())
                    if name:
                        counts[(day, f"核对/{name}")] += 1
                        if not name.startswith(("需合成点击", "抢救定位")):
                            counts[(day, "核对/总核对数")] += 1
                    continue
                m = _FAIL.search(line)
                if m:
                    name = _classify_fail(m.group(1))
                    if name:
                        counts[(day, f"失败/{name}")] += 1
                    continue
                m = _CONFIRM.search(line)
                if m:
                    name = _classify_confirm(m.group(1).strip())
                    if name:
                        counts[(day, f"确认/{name}")] += 1
    return counts


def _render(
    counts: dict, day: str, stage: str, layers: list[str], win_s: str, lose_s: str
) -> list[str]:
    total = counts.get((day, f"{stage}/总会话数"), 0)
    lines = [f"  {stage}层 (赢下会话数 {total})"]
    for name in layers:
        win = counts.get((day, f"{stage}/{name}{win_s}"), 0)
        lose = counts.get((day, f"{stage}/{name}{lose_s}"), 0)
        appear = win + lose
        if appear == 0:
            continue
        lines.append(
            f"    {name:<28} 出场{appear:>5}  赢{win:>4}  输{lose:>4}"
            f"  {win / appear * 100:5.1f}%赢"
        )
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("logs", nargs="*", type=Path, default=None)
    ap.add_argument("--days", type=int, default=0, help="只看最近 N 天(按日志内日期)")
    ap.add_argument(
        "--all",
        action="store_true",
        help="读 runtime/spark.log*(含 logrotate 压出的 .gz), 按时间由旧到新",
    )
    args = ap.parse_args()
    if args.all:
        runtime = REPO / "runtime"
        # 只认轮转命名(spark.log / .1 / .40 / .40.gz): 手工备份与探针日志不算历史
        candidates = [
            p for p in runtime.glob("spark.log*") if _ROTATED_NAME.fullmatch(p.name)
        ]
        # 由旧到新: 没有时间戳的续行会继承上一行的日期, 顺序反了就把续行算进错误的日子
        files = sorted(candidates, key=lambda p: p.stat().st_mtime)
        if not files:
            print(f"[strat_stats] {runtime} 下没有 spark.log*", file=sys.stderr)
            return 1
    else:
        files = args.logs or [REPO / "runtime" / "spark.log"]
    counts = parse(files)
    if not counts:
        print("[strat_stats] 日志里没有可解析的策略行(文件对不对? 格式漂移了?)")
        return 1

    days = sorted({d for d, _ in counts if d != "?"})
    if args.days:
        cutoff = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")
        days = [d for d in days if d >= cutoff]
    loc_layers = sorted(
        {k.split("/", 1)[1].rstrip("!✓") for (_, k) in counts if k.startswith("定位/")},
    )
    snd_layers = [
        "表情",
        "表情(降级)",
        "表情判空",
        "文字A",
        "文字B",
        "文字兜底也失败",
        "视觉兜底",
    ]
    if len(files) > 3:
        # 40 天的 --all 会把文件名刷成一堵墙, 只报范围
        print(
            f"策略健康度 · {len(days)} 天 · {len(files)} 个日志文件"
            f"(旧→新 {files[0].name} … {files[-1].name})"
        )
    else:
        print(f"策略健康度 · {len(days)} 天 · {' '.join(str(f) for f in files)}")
    for d in days:
        print(f"\n== {d} ==")
        for line in _render(counts, d, "定位", loc_layers, "✓", "!"):
            print(line)
        for line in _render(counts, d, "发送", snd_layers, "✓", "✗"):
            print(line)
        conf_keys = sorted(
            {k.split("/", 1)[1] for (_, k) in counts if k.startswith("确认/")}
        )
        if conf_keys:
            hard = counts.get((d, "确认/硬证据"), 0)
            degraded = counts.get((d, "确认/降级确认(证据不可读)"), 0)
            failed = counts.get((d, "确认/未能确认"), 0)
            errored = counts.get((d, "确认/确认异常"), 0)
            base_ok = counts.get((d, "确认/基线可读"), 0)
            base_bad = counts.get((d, "确认/基线读不到"), 0)
            sticker_degraded = counts.get((d, "发送/表情(降级)✓"), 0)
            print(
                f"  发送确认: 硬证据 {hard} / 降级 {degraded} / 未确认 {failed} / "
                f"异常 {errored} (文字层)"
            )
            print(
                f"    基线可读 {base_ok} / 读不到 {base_bad}; "
                f"表情层无新增证据仍算成功 {sticker_degraded} 次"
            )
            if hard == 0 and degraded:
                print("    → ⚠ 当晚**没有一次**发送是被证据确认的(等于发送层无验证)")
            if base_bad and base_bad == base_ok + base_bad:
                print(
                    "    → ⚠ 消息列表尾部一直读不到(类名失效或面板未挂载), 确认全在降级跑"
                )
        used = counts.get((d, "cv/used"), 0)
        if used:
            found = counts.get((d, "cv/found"), 0)
            clicked = counts.get((d, "cv/clicked"), 0)
            verified = counts.get((d, "发送/视觉兜底✓"), 0)
            print(
                f"  视觉兜底漏斗: used={used} found={found} clicked={clicked} "
                f"verified={verified} (found高而verified低 → 禁用本层)"
            )
            keys = sorted(
                {k.split("/", 1)[1] for (_, k) in counts if k.startswith("cv归因/")}
            )
            print(
                "    归因: "
                + "  ".join(f"{k}={counts[(d, f'cv归因/{k}')]}" for k in keys)
            )
        total = counts.get((d, "核对/总核对数"), 0)
        if total:
            rescue_keys = ("需合成点击抢救", "抢救定位也失败")
            gate_keys = sorted(
                {
                    k.split("/", 1)[1]
                    for (_, k) in counts
                    if k.startswith("核对/")
                    and k != "核对/总核对数"
                    and k.split("/", 1)[1] not in rescue_keys
                }
            )
            print(f"  身份闸门(裁决 {total} 次):")
            for k in gate_keys:
                n = counts[(d, f"核对/{k}")]
                print(f"    {k:<18} {n:>4}  {n / total * 100:5.1f}%")
            blind = sum(
                counts.get((d, f"核对/{k}"), 0)
                for k in ("盲放行(首读即盲)", "盲放行(复检后)", "核对自身异常")
            )
            blocked = sum(
                counts.get((d, f"核对/{k}"), 0)
                for k in ("冲突拦截(复检后)", "冲突拦截(单次)")
            )
            if blind:
                print(f"    → ⚠ 盲放行 {blind} 次: root 判据对当前布局仍有漏配")
            if blocked:
                print(
                    f"    → ⚠ 拦截 {blocked} 次: 逐个去手机端确认是否真没发(误拦=漏发)"
                )
            if blind == 0 and blocked == 0:
                print("    → ✓ 全部裁决都是读到位后判出来的")
        rescue = counts.get((d, "核对/需合成点击抢救"), 0)
        rescue_bad = counts.get((d, "核对/抢救定位也失败"), 0)
        if rescue or rescue_bad:
            # 抢救是「面板有没有挂上」的事实, 与闸门裁决分母不同, 单独成行。
            # 分母 = 走到面板这一步的会话数(每次要么直接出裁决, 要么先抢救一次再出裁决)。
            # 2026-10-07 实测: 白天可信点击 4/4 打不开面板, 全靠这一层 —— 占比要看得见。
            opened = total + rescue
            if opened == 0:
                print(f"  面板挂载抢救: 抢救定位失败 {rescue_bad} (未走到闸门裁决)")
            else:
                share = (opened - rescue) / opened * 100
                print(
                    f"  面板挂载抢救: 需合成点击 {rescue}/{opened} "
                    f"(可信点击直接可用 {share:.1f}%), 抢救定位失败 {rescue_bad}"
                )
        fail_keys = sorted(
            {k.split("/", 1)[1] for (_, k) in counts if k.startswith("失败/")}
        )
        if fail_keys:
            print(
                "  失败/交接行: "
                + "  ".join(f"{k}={counts[(d, f'失败/{k}')]}" for k in fail_keys)
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
