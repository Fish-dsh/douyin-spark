#!/bin/bash
cd /opt/douyin-spark 2>>/tmp/run_cron.err || {
    echo "$(date '+%F %T') run_cron.sh: cd /opt/douyin-spark 失败" >> /tmp/run_cron.err
    exit 1
}
mkdir -p runtime
# 有头 chromium 跑在 Xvfb 虚拟显示上(规避抖音 headless 拦截, 2026-09-24)
# Xvfb 由 xvfb.service 常驻 :99; 可用 SPARK_DISPLAY 覆盖
export DISPLAY="${SPARK_DISPLAY:-:99}"
# 生产档位焊死(2026-11-01): 板子只有 4 核/1.5G + Xvfb 软渲染, 慢在抖音侧渲染而不是
# 本机算力, 高档位收窄超时都是「更快失败」而不是「更快成功」。这里显式落 low,
# 挡住 config.json / SPARK_PERF_PROFILE / 环境残留任何一条把档位带偏的路。
# 本机开发不要改这行, 走 run_dev.sh(它用 config.dev.json 的 perf_profile)。
export SPARK_PERF_PROFILE=low
# 退出码语义:
#   0 = 成功/无待续好友/窗口外
#   3 = 本进程内重试仍失败 → 冷却180s冷启动重跑, 最多3次
#   1 = 一般异常 → 重试 1 次(2026-09-30 事故: main 失败后 好友F 根本没跑;
#       但 rc=1 重试前必须检查剩余窗口, 00:20 后不重试, 避免耗尽整窗)
#   2 = 未登录 → 直接退出, 不重试
#   4 = 未获得 PID 锁(另一实例持锁) → 本次跳过, 不重试; **不当成功**(2026-10-01 评审:
#       原先裸 return 得 rc=0, 若锁被挂死实例长期持有, cron 视角天天"成功"、静默断火)
# 124 = timeout 包裹杀掉的挂死进程(同 rc=4 思路: 显式可见, 不静默)
# 130(Ctrl+C)人工中断, 不重跑
#
# 时间预算(2026-10-04 事故重排):
#   45m → **90m**。两账号实测串行发送需 ~60 分钟(单账号发现 40s + 逐好友 1.3~3.5min),
#   再叠加 ±5min 抖动, 45 分钟**在无故障的正常夜里都不够** —— 10-04 就是
#   发到第 4/8 个被 timeout 杀掉(rc=124)、后 5 个好友漏发。90m 留足余量。
#   发送窗口只约束**开始时刻**(23:30-00:30), 跑到凌晨一点不违规。
#
# 硬截止(2026-10-04 评审补上, 这才是"90m 够用"能成立的前提):
#   health_check 在 01:45 判这一晚, 而它有条豁免——"主任务还在跑就不判定、直接退 0"
#   (判定过早=假告警=告警疲劳)。可 rc=3 的冷启动重跑会把最晚收尾推到 00:23+90m=01:53,
#   **那一晚就谁都没判过**, 漏发静默发生。所以整晚要有硬截止:
#   距 01:35 不足 20 分钟不再起新一轮, 每轮 timeout 按剩余时间收窄。
#   00:00 起的第一轮仍有完整 90m, 正常一晚不受任何影响; 被收窄的只是"已经烧过一轮"的晚上。
_HARD_STOP_MIN=95  # 01:35(= 95 分钟, 从 00:00 起算)
# 分钟数(00:00=0)。23:30~23:59 记成负数: 发送窗口跨午夜, 夜里手动补跑同样在截止之前。
_now_min() {
    local hm h m v
    hm=$(date +%H:%M)
    h=${hm%:*}
    m=${hm#*:}
    v=$((10#$h * 60 + 10#$m))
    [ "$v" -ge 1380 ] && v=$((v - 1440))
    echo "$v"
}
_rc1_retried=0
for attempt in 1 2 3; do
    _left=$((_HARD_STOP_MIN - $(_now_min)))
    if [ "$_left" -lt 20 ]; then
        echo "$(date '+%F %T') [run_cron] 距硬截止(01:35)只剩 ${_left} 分钟, 不再起第 ${attempt} 轮; 这一晚交给 01:45 的健康检查告警(rc=${rc:-1})" >> runtime/spark.log
        exit "${rc:-1}"
    fi
    _tmo=90
    [ "$_left" -lt "$_tmo" ] && _tmo="$_left"
    # 把硬截止时刻(今天 00:00 + 95 分钟)导给 spark.py: 补扫阶段据此判断「还有没有
    # 余量再多开一人」—— 烧穿 timeout 会得到 rc=124 而 run_cron 对它直接退出不重试,
    # 比「少补扫一个」更糟。跨午夜时 00:00 已过, date 自然给出今天零点。
    export SPARK_DEADLINE_EPOCH=$(( $(date -d "$(date +%F) 00:00" +%s) + _HARD_STOP_MIN * 60 ))
    if [ "$_tmo" -ne 90 ]; then
        echo "$(date '+%F %T') [run_cron] 第 ${attempt} 轮按硬截止收窄 timeout: ${_tmo}m(距 01:35 剩 ${_left} 分钟)" >> runtime/spark.log
    fi
    # timeout 兜住"进程挂死长驻持锁": 无上限时一次挂死会让后续每天的 cron 都撞锁退出
    timeout ${_tmo}m .venv/bin/python spark.py >/dev/null 2>>runtime/spark.log
    rc=$?
    if [ "$rc" -eq 130 ]; then
        exit 130
    fi
    if [ "$rc" -eq 0 ]; then
        exit 0
    fi
    if [ "$rc" -eq 4 ]; then
        echo "$(date '+%F %T') [run_cron] 未获得 PID 锁(另一实例持锁), 本次跳过(rc=4)" >> runtime/spark.log
        echo "$(date '+%F %T') [run_cron][warn] rc=4: 若连续多天出现, 说明有实例挂死未释放锁, 检查 runtime/.spark.pid" >> runtime/spark.log
        exit 4
    fi
    if [ "$rc" -ne 1 ] && [ "$rc" -ne 3 ]; then
        echo "$(date '+%F %T') [run_cron] 运行失败(rc=${rc}), 非重试退出码, 直接退出" >> runtime/spark.log
        exit "$rc"
    fi
    # 重试只看「距硬截止还剩多少」: 循环开头已按 _left<20 拦截, 并按余量收窄 timeout。
    # 原先这里另有一条墙上时钟截止(00:20 后不重试), 两账号串行时首轮跑到 00:21 就会
    # 让整晚放弃重试——而那次实际只用了 18 分钟, 余量充足(2026-10-07: main 账号
    # 被覆盖门禁熔断后无人补发, 整晚 0 发送)。窗口只约束**开始时刻**, 跑到凌晨一点
    # 不违规, 故正确判据是余量而非时钟。
    if [ "$rc" -eq 1 ]; then
        if [ "$_rc1_retried" -ge 1 ]; then
            echo "$(date '+%F %T') [run_cron] rc=1 已重试过 1 次仍失败, 放弃" >> runtime/spark.log
            exit 1
        fi
        _rc1_retried=1
        echo "$(date '+%F %T') [run_cron] 运行失败(rc=1), 冷却 60s 后重试 1/1 次" >> runtime/spark.log
        sleep 60
    else
        echo "$(date '+%F %T') [run_cron] 本进程重试失败(rc=3), ${attempt}/3 次, 冷却 180s 后冷启动重跑" >> runtime/spark.log
        sleep 180
    fi
done
echo "$(date '+%F %T') [run_cron] 已重试 3 次均失败(rc=3), 放弃" >> runtime/spark.log
exit 1
