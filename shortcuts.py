"""
T-M-B Shortcut Commands — direct PowerShell execution, bypass Claude.

Extracted from telegram_bridge_claude_v3.1.py (Phase 1 module split, 2026-05-06).
"""

import subprocess
import logging

from telegram import Update
from telegram.ext import ContextTypes

import bridge_core

logger = logging.getLogger(__name__)


async def _run_ps_shortcut(update, command, timeout=30):
    if not bridge_core.bridge.is_authorized(update.effective_user.id):
        await update.message.reply_text("未授權")
        return
    try:
        result = subprocess.run(
            ["powershell", "-Command", command],
            capture_output=True, text=True, timeout=timeout,
            encoding='utf-8', errors='replace'
        )
        output = result.stdout or result.stderr or "(無輸出)"
        if len(output) > 8000:
            output = output[:8000] + "\n...(已截斷)"
        await update.message.reply_text(output)
    except subprocess.TimeoutExpired:
        await update.message.reply_text("執行超時")
    except Exception as e:
        await update.message.reply_text(f"錯誤：{e}")


async def ps_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmd = r"""
$targets = @('python', 'claude', 'node', 'Claude')
$procs = Get-Process | Where-Object {
    $name = $_.ProcessName
    $targets | ForEach-Object { if ($name -like "*$_*") { $true } }
} | Select-Object ProcessName,
    @{N='CPU(s)';E={[math]::Round($_.CPU,1)}},
    @{N='Mem(MB)';E={[math]::Round($_.WorkingSet64/1MB,1)}},
    @{N='PID';E={$_.Id}},
    @{N='Runtime';E={
        $ts = (Get-Date) - $_.StartTime
        if ($ts.TotalHours -ge 1) { '{0:0}h{1:00}m' -f [math]::Floor($ts.TotalHours), $ts.Minutes }
        else { '{0}m{1:00}s' -f $ts.Minutes, $ts.Seconds }
    }}

if ($procs) {
    Write-Output "📊 關鍵進程狀態："
    Write-Output "─────────────────────────────"
    $procs | Format-Table -AutoSize | Out-String
} else {
    Write-Output "⚠️ 未偵測到關鍵進程"
}

$uptime = (Get-Date) - (Get-CimInstance Win32_OperatingSystem).LastBootUpTime
Write-Output ("💻 系統已運行: {0}天 {1}時 {2}分" -f $uptime.Days, $uptime.Hours, $uptime.Minutes)
"""
    await _run_ps_shortcut(update, cmd)


async def cclog_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    n = 30
    if context.args:
        try:
            n = min(int(context.args[0]), 100)
        except ValueError:
            pass

    cmd = f"""
$logDir = "$env:USERPROFILE\\.claude\\logs"
if (Test-Path $logDir) {{
    $latest = Get-ChildItem $logDir -Filter "*.log" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($latest) {{
        Write-Output "📋 CC Log: $($latest.Name)"
        Write-Output "📅 最後修改: $($latest.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss'))"
        Write-Output "─────────────────────────────"
        Get-Content $latest.FullName -Tail {n} -Encoding UTF8 -ErrorAction SilentlyContinue
    }} else {{
        Write-Output "⚠️ logs 目錄存在但無 .log 檔"
    }}
}} else {{
    Write-Output "⚠️ Claude Code log 目錄不存在: $logDir"
}}
"""
    await _run_ps_shortcut(update, cmd)


async def tasklog_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmd = r"""
Write-Output "📅 Task Scheduler 近期執行結果："
Write-Output "─────────────────────────────"

$tasks = Get-ScheduledTask | Where-Object { $_.State -ne 'Disabled' } |
    ForEach-Object {
        $info = Get-ScheduledTaskInfo -TaskName $_.TaskName -TaskPath $_.TaskPath -ErrorAction SilentlyContinue
        if ($info -and $info.LastRunTime -gt (Get-Date).AddDays(-3)) {
            [PSCustomObject]@{
                Name     = $_.TaskName
                LastRun  = $info.LastRunTime.ToString('MM-dd HH:mm')
                Result   = if ($info.LastTaskResult -eq 0) { '✅' }
                           elseif ($info.LastTaskResult -eq 267009) { '⏳ 執行中' }
                           else { "❌ ($($info.LastTaskResult))" }
                NextRun  = if ($info.NextRunTime) { $info.NextRunTime.ToString('MM-dd HH:mm') } else { '-' }
            }
        }
    } | Sort-Object LastRun -Descending | Select-Object -First 10

if ($tasks) {
    $tasks | Format-Table -AutoSize | Out-String
} else {
    Write-Output "最近 3 天內沒有排程任務執行紀錄"
}
"""
    await _run_ps_shortcut(update, cmd, timeout=45)


async def bridge_log_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    n = 30
    if context.args:
        try:
            n = min(int(context.args[0]), 100)
        except ValueError:
            pass

    log_dir = str(bridge_core.CONFIG["LOG_DIR"]).replace("\\", "\\\\")
    cmd = f"""
$logDir = "{log_dir}"
$latest = Get-ChildItem $logDir -Filter "bridge.log*" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($latest) {{
    Write-Output "📋 T-M-B Log: $($latest.Name)"
    Write-Output "📅 最後修改: $($latest.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss'))"
    Write-Output "─────────────────────────────"
    Get-Content $latest.FullName -Tail {n} -Encoding UTF8 -ErrorAction SilentlyContinue
}} else {{
    Write-Output "⚠️ 找不到 T-M-B log 檔案"
}}
"""
    await _run_ps_shortcut(update, cmd)


async def uptime_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmd = r"""
$tmb = Get-Process python -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*telegram_bridge*' -or $_.CommandLine -like '*bridge*' } |
    Select-Object -First 1

if ($tmb) {
    $runtime = (Get-Date) - $tmb.StartTime
    $startStr = $tmb.StartTime.ToString('yyyy-MM-dd HH:mm:ss')
    Write-Output "🤖 T-M-B 狀態: ✅ 運行中"
    Write-Output ("   啟動時間: " + $startStr)
    Write-Output ("   已運行: {0}天 {1}時 {2}分" -f $runtime.Days, $runtime.Hours, $runtime.Minutes)
    Write-Output ("   記憶體: {0:N0} MB" -f ($tmb.WorkingSet64/1MB))
} else {
    $pyProcs = Get-Process python -ErrorAction SilentlyContinue
    if ($pyProcs) {
        Write-Output "🤖 T-M-B 狀態: ⚠️ 無法精確識別（找到 $($pyProcs.Count) 個 python 進程）"
        $pyProcs | ForEach-Object {
            $rt = (Get-Date) - $_.StartTime
            Write-Output ("   PID $($_.Id): 已運行 {0}天{1}時{2}分, {3:N0}MB" -f $rt.Days, $rt.Hours, $rt.Minutes, ($_.WorkingSet64/1MB))
        }
    } else {
        Write-Output "🤖 T-M-B 狀態: ❌ 未偵測到 python 進程"
    }
}

Write-Output ""

$os = Get-CimInstance Win32_OperatingSystem
$boot = $os.LastBootUpTime
$uptime = (Get-Date) - $boot
Write-Output ("💻 系統開機: " + $boot.ToString('yyyy-MM-dd HH:mm:ss'))
Write-Output ("   已運行: {0}天 {1}時 {2}分" -f $uptime.Days, $uptime.Hours, $uptime.Minutes)
"""
    await _run_ps_shortcut(update, cmd)
