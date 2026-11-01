#!/bin/sh
# net_watchdog.sh —— 板子网络假死看门狗
#
# 背景: OrangePi Zero3 板载千兆 PHY Motorcomm YT8531 有已知缺陷 —— 低负载/空闲后
# 会"假死"(网口灯正常、carrier 仍 UP, 但数据全断), **软件无法恢复, 官方称只能断电/重启**。
# 现状缓解: end0 固定 100M (`nmcli 802-3-ethernet.speed 100`), 但只降低概率、不根治。
# 本脚本由 cron 每 5 分钟调用, 探测外网; 断网时按 [软复位 -> 切 WiFi 备线 -> 重启] 分级恢复。
#
# 恢复阶梯(连续失败次数, ~5min/次):
#   >= SOFT_AFTER     (默认3, ~15min): nmcli 复位有线连接 + ip down/up
#   >= FAILOVER_AFTER (默认4, ~20min): 若仍有线不通, 关掉有线改走 wlan0 备线(不重启)
#   >= REBOOT_AFTER   (默认6, ~30min): 重启板子(最后手段; 生产窗口内/刚重启过则跳过)
#
# 干跑/测试: NETWD_DRYRUN=1 只打印不执行; GW/EXT/WIRED/... 均可用环境变量覆盖。
# 留痕: 失败/恢复/心跳都写 LOG; 健康时每日仅一枚「心跳」行(便于确认看门狗还活着)。
#
set -u

GW=${GW:-192.0.2.1}
EXT=${EXT:-223.5.5.5}
WIRED=${WIRED:-end0}
WIRED_CON=${WIRED_CON:-wired-main}
STATE_DIR=${NETWD_STATE_DIR:-/var/lib/net-watchdog}
LOG=${NETWD_LOG:-/var/log/net-watchdog.log}
DRYRUN=${NETWD_DRYRUN:-0}

SOFT_AFTER=${SOFT_AFTER:-3}
FAILOVER_AFTER=${FAILOVER_AFTER:-4}
REBOOT_AFTER=${REBOOT_AFTER:-6}
REBOOT_COOLDOWN=${REBOOT_COOLDOWN:-3600}
PROD_START=${PROD_START:-2345}   # 生产窗口起点 HHMM(此时起不重启, 怕打断续火花)
PROD_END=${PROD_END:-0145}       # 生产窗口终点 HHMM

STATE="$STATE_DIR/fail.count"
LASTREBOOT="$STATE_DIR/last.reboot"

mkdir -p "$STATE_DIR" "$(dirname "$LOG")" 2>/dev/null

log() { echo "$(date '+%F %T') $*" >>"$LOG"; command -v logger >/dev/null 2>&1 && logger -t net-watchdog "$*"; }
run() { if [ "$DRYRUN" = 1 ]; then log "[dryrun] $*"; else log "+ $*"; "$@" >>"$LOG" 2>&1; fi; }
probe() { ping -c 2 -W 2 "$1" >/dev/null 2>&1; }

hhmm=$(date +%H%M)
in_prod() { [ "$hhmm" -ge "$PROD_START" ] || [ "$hhmm" -le "$PROD_END" ]; }

# 外网或网关任一可达 => 网络正常
if probe "$EXT" || probe "$GW"; then
    [ "$(cat "$STATE" 2>/dev/null || echo 0)" != "0" ] && log "网络已恢复(外网/网关可达), 失败计数清零"
    echo 0 >"$STATE"
    # 每日一枚心跳: 健康时本脚本原本毫无留痕, 无法一眼判断它是否还在跑
    # (cron 静默失败的教训)。心跳也能让事后确认某天看门狗确实活着。
    hb="$STATE_DIR/last.hb"
    today=$(date '+%F')
    if [ "$(cat "$hb" 2>/dev/null)" != "$today" ]; then
        log "心跳: 看门狗存活, 网络正常(每日一则)"
        echo "$today" >"$hb"
    fi
    exit 0
fi

n=$(cat "$STATE" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" >"$STATE"
log "探测失败(第 $n 次): $GW / $EXT 均不可达"

# ---- 最后手段: 重启(护栏: 生产窗口内不重启; 冷却期内不重启) ----
if [ "$n" -ge "$REBOOT_AFTER" ]; then
    if in_prod; then
        log "连续 $n 次失败, 但处于生产窗口($PROD_START-$PROD_END), 不重启(仅做软恢复)"
    else
        last=$(cat "$LASTREBOOT" 2>/dev/null || echo 0)
        now=$(date +%s)
        if [ $((now - last)) -ge "$REBOOT_COOLDOWN" ]; then
            log "连续 $n 次失败且冷却已过, 触发重启(最后手段)"
            echo "$now" >"$LASTREBOOT"
            run sync
            run /sbin/reboot
            exit 0
        fi
        log "本可重启, 但距上次重启不足 ${REBOOT_COOLDOWN}s, 跳过"
    fi
fi

# ---- 切 WiFi 备线: 有线不通时改走 wlan0, 不重启也能恢复 ----
if [ "$n" -ge "$FAILOVER_AFTER" ]; then
    log "尝试切至 WiFi 备线(关掉有线连接 $WIRED_CON)"
    run nmcli con down "$WIRED_CON"
    sleep 5
    if probe "$EXT" || probe "$GW"; then
        log "已切至 WiFi 备线, 网络恢复"
        echo 0 >"$STATE"
        exit 0
    fi
    log "切备线后仍未恢复"
fi

# ---- 软复位有线连接 ----
if [ "$n" -ge "$SOFT_AFTER" ]; then
    log "软复位有线 $WIRED (ip down/up + nmcli up)"
    run ip link set "$WIRED" down
    sleep 3
    run ip link set "$WIRED" up
    run nmcli con up "$WIRED_CON"
    sleep 5
    if probe "$EXT" || probe "$GW"; then
        log "软复位后网络恢复"
        echo 0 >"$STATE"
        exit 0
    fi
fi

exit 0
