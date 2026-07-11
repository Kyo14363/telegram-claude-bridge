# T-M-B process watchdog (v3.2.3) — run from Task Scheduler every 5 min, e.g.:
#   schtasks /Create /TN TMB_Watchdog /SC MINUTE /MO 5 ^
#     /TR "wscript.exe \"C:\path\to\tmb_watchdog.vbs\""
# (the .vbs wrapper runs this script without a console flash)
#
# Two checks:
#   1. existence: bridge python process gone -> restart
#   2. liveness:  process exists but logs/heartbeat.txt stale (> $staleMin) ->
#      event loop wedged; kill the process tree, then restart.
#      (the bridge touches heartbeat.txt every 5 min from its main asyncio loop)
# Known intended behavior: after a long machine sleep the heartbeat is stale on
# wake -> one clean restart. That is desirable (connections are dead anyway).
# The match string 'telegram_bridge_claude' is deliberately specific so other
# python processes (including other bridges) are never counted or touched.
# Maintenance: disable before a manual kill/restart, or the watchdog will race you:
#   schtasks /Change /TN TMB_Watchdog /DISABLE   (re-enable with /ENABLE)

$ErrorActionPreference = "SilentlyContinue"
$base = $PSScriptRoot
$hbFile = "$base\logs\heartbeat.txt"
$staleMin = 15

function Write-WdLog([string]$msg) {
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Add-Content -Path "$base\logs\watchdog.log" -Value "$stamp $msg" -Encoding utf8
}

function Kill-BridgeTree($proc) {
    # kill the parent cmd (bat console) tree when present, else the python tree
    $target = $proc.ProcessId
    $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($proc.ParentProcessId)"
    if ($parent -and $parent.Name -eq 'cmd.exe') { $target = $parent.ProcessId }
    taskkill /PID $target /T /F | Out-Null
    Start-Sleep -Seconds 2
    return $target
}

$alive = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*telegram_bridge_claude*' } |
    Select-Object -First 1

$restart = $false

if (-not $alive) {
    Write-WdLog "bridge not running - restart issued"
    $restart = $true
}
elseif (Test-Path $hbFile) {
    $ageMin = ((Get-Date) - (Get-Item $hbFile).LastWriteTime).TotalMinutes
    if ($ageMin -gt $staleMin) {
        $killed = Kill-BridgeTree $alive
        Write-WdLog "heartbeat stale ($([math]::Round($ageMin)) min) - wedged tree killed (PID $killed), restart issued"
        $restart = $true
    }
}
else {
    # heartbeat file missing: treat as wedged only after 20 min uptime --
    # guards the startup window and pre-v3.2.3 bridge builds (avoid kill loop;
    # a reverted build still gets caught, just every ~20 min and visibly logged)
    $proc = Get-Process -Id $alive.ProcessId
    if ($proc -and ((Get-Date) - $proc.StartTime).TotalMinutes -gt 20) {
        $killed = Kill-BridgeTree $alive
        Write-WdLog "heartbeat file missing after 20+ min uptime - tree killed (PID $killed), restart issued"
        $restart = $true
    }
}

if ($restart) {
    Start-Process -FilePath "$base\start_bridge.bat" -WorkingDirectory $base
}
