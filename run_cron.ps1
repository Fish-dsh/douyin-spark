# run_cron.ps1 —— Windows 计划任务入口, 与 run_cron.sh 一一对应。
#
# 为什么要有这一份: 板子上是 Linux(有头 chromium 跑在 Xvfb :99), 但开发机和
# 一部分用户的机器是 Windows —— 那边没有 bash / date -d / timeout, 直接把
# run_cron.sh 塞进计划任务只会得到「找不到命令」。这里用同一套判据重写一遍,
# 保证两边的退出码、硬截止、重试矩阵完全一致, 否则同一晚在两台机器上会得出
# 不同的结论, 而告警口径(01:45 巡检)只有一套。
#
# 注册(每天 00:00):
#   schtasks /Create /TN douyin-spark /SC DAILY /ST 00:00 /TR ^
#     "powershell -NoProfile -ExecutionPolicy Bypass -File C:\path\run_cron.ps1"
#
# 退出码语义(与 run_cron.sh 相同, 外部契约不可改):
#   0 = 成功/无待续好友/窗口外      3 = 本进程重试仍失败(由本脚本冷启动重跑, 最多 3 轮)
#   1 = 一般异常(重试 1 次)          2 = 未登录(直接退出, 不重试)
#   4 = 未获得 PID 锁(另一实例持锁, 本次跳过; **不当成功**)
#   124 = 超时被本脚本杀掉(显式可见, 不静默)      130 = 人工中断
#
# 时间预算: 首轮 90 分钟; 硬截止 01:35(00:00 起算 95 分钟)。health_check 在
# 01:45 判这一晚, 且「主任务还在跑就不判定」, 所以整晚必须有硬截止 —— 否则
# rc=3 的冷启动重跑会把收尾推到 01:53, 那一晚谁都没判, 漏发静默发生。

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
New-Item -ItemType Directory -Force -Path runtime | Out-Null

# 生产档位焊死(理由同 run_cron.sh): 慢在抖音侧渲染而不是本机算力,
# 高档位只是更快失败。本机开发走 run_dev.sh。
$env:SPARK_PERF_PROFILE = "low"

function Write-RunLog([string]$Message) {
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath (Join-Path $PSScriptRoot "runtime\spark.log") -Value "$stamp $Message" -Encoding utf8
}

$HardStopMin = 95      # 01:35 = 从 00:00 起算 95 分钟
$Tmo = 90              # 首轮超时(分钟)
$MinLeft = 20          # 距硬截止不足 20 分钟就不再起新一轮

# 分钟数(00:00=0)。23:30~23:59 记成负数: 发送窗口跨午夜, 夜里手动补跑同样在截止之前。
function Get-NowMin {
    $hm = Get-Date -Format "HH:mm"
    $v = [int]$hm.Substring(0, 2) * 60 + [int]$hm.Substring(3, 2)
    if ($v -ge 1380) { $v -= 1440 }
    return $v
}

# 硬截止时刻(逻辑运行日的 00:00 + 95 分钟)导给 spark.py: 补扫阶段据此判断
# 「还有没有余量再多开一人」—— 烧穿超时会得到 124 而本脚本不重试, 比少补扫一个更糟。
# 逻辑运行日 ≠ 今天: 窗口跨午夜(23:30-00:30), 23 点后手动补跑的那一次属于**明天**
# 那一晚。照「今天零点」算, 硬截止就成了 22 小时前的过去时刻, 补扫余量恒为负。
# 判据直接用 Get-NowMin 的负数语义(23:30 → -30), 与 Linux 侧同一个口径。
$runDate = (Get-Date).Date
if ((Get-NowMin) -lt 0) { $runDate = $runDate.AddDays(1) }
$deadline = [DateTimeOffset]::new($runDate).ToUnixTimeSeconds() + $HardStopMin * 60
$env:SPARK_DEADLINE_EPOCH = [string]$deadline

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $python) {
        Write-RunLog "[run_cron] 找不到 python(.venv\Scripts\python.exe 与 PATH 都没有), 退出"
        exit 1
    }
}

$rc = $null
$rc1Retried = $false
$outFile = Join-Path $PSScriptRoot "runtime\.run_cron.out"
$errFile = Join-Path $PSScriptRoot "runtime\.run_cron.err"

for ($attempt = 1; $attempt -le 3; $attempt++) {
    $left = $HardStopMin - (Get-NowMin)
    if ($left -lt $MinLeft) {
        $prev = if ($null -ne $rc) { $rc } else { 1 }
        Write-RunLog "[run_cron] 距硬截止(01:35)只剩 $left 分钟, 不再起第 $attempt 轮; 这一晚交给 01:45 的健康检查告警(rc=$prev)"
        exit $prev
    }
    $tmo = $Tmo
    if ($left -lt $tmo) { $tmo = $left }
    if ($tmo -ne $Tmo) {
        Write-RunLog "[run_cron] 第 $attempt 轮按硬截止收窄 timeout: ${tmo}m(距 01:35 剩 $left 分钟)"
    }
    # 超时兜住「进程挂死长驻持锁」: 无上限时一次挂死会让后续每天的定时任务都撞锁退出。
    $proc = Start-Process -FilePath $python -ArgumentList "spark.py" -NoNewWindow -PassThru `
        -RedirectStandardOutput $outFile -RedirectStandardError $errFile
    $finished = $true
    try {
        Wait-Process -Id $proc.Id -Timeout ($tmo * 60) -ErrorAction Stop
    } catch {
        $finished = $false
    }
    if (-not $finished) {
        # 只杀 python 不够: chromium 是它的子进程, 父进程死后浏览器照样活着吃内存,
        # 下一轮再开新实例 —— 一整晚下来本机被上一轮的浏览器吃掉。/T 连子进程一起杀,
        # 与 Linux 侧 timeout 结束整条链的意图对齐。taskkill 不在时才退回 Stop-Process。
        $killer = Start-Process -FilePath "taskkill.exe" -ArgumentList "/PID", $proc.Id, "/T", "/F" -NoNewWindow -PassThru -Wait -ErrorAction SilentlyContinue
        if ($null -eq $killer -or $killer.ExitCode -ne 0) {
            Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        }
        $rc = 124
    } else {
        $rc = $proc.ExitCode
    }
    if (Test-Path -LiteralPath $errFile) {
        $stderr = Get-Content -LiteralPath $errFile -Raw -Encoding utf8
        if ($stderr) { Add-Content -LiteralPath (Join-Path $PSScriptRoot "runtime\spark.log") -Value $stderr.TrimEnd() -Encoding utf8 }
        Remove-Item -LiteralPath $errFile -Force
    }
    if (Test-Path -LiteralPath $outFile) { Remove-Item -LiteralPath $outFile -Force }

    if ($rc -eq 130) { exit 130 }
    if ($rc -eq 0) { exit 0 }
    if ($rc -eq 4) {
        Write-RunLog "[run_cron] 未获得 PID 锁(另一实例持锁), 本次跳过(rc=4)"
        Write-RunLog "[run_cron][warn] rc=4: 若连续多天出现, 说明有实例挂死未释放锁, 检查 runtime/.spark.pid"
        exit 4
    }
    if ($rc -ne 1 -and $rc -ne 3) {
        Write-RunLog "[run_cron] 运行失败(rc=$rc), 非重试退出码, 直接退出"
        exit $rc
    }
    # 重试只看「距硬截止还剩多少」: 循环开头已按余量拦截, 并按余量收窄超时。
    # 窗口只约束**开始时刻**, 跑到凌晨一点不违规, 故正确判据是余量而非墙上时钟。
    if ($rc -eq 1) {
        if ($rc1Retried) {
            Write-RunLog "[run_cron] rc=1 已重试过 1 次仍失败, 放弃"
            exit 1
        }
        $rc1Retried = $true
        Write-RunLog "[run_cron] 运行失败(rc=1), 冷却 60s 后重试 1/1 次"
        Start-Sleep -Seconds 60
    } else {
        Write-RunLog "[run_cron] 本进程重试失败(rc=3), $attempt/3 次, 冷却 180s 后冷启动重跑"
        Start-Sleep -Seconds 180
    }
}
Write-RunLog "[run_cron] 已重试 3 次均失败(rc=3), 放弃"
exit 1
