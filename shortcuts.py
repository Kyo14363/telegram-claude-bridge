"""
T-M-B Shortcut Commands — direct PowerShell execution, bypass Claude.

Extracted from telegram_bridge_claude_v3.1.py (Phase 1 module split, 2026-05-06).
"""

import re
import os
import json
import asyncio
import subprocess
import logging
from datetime import datetime

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
            ["powershell", "-NoProfile", "-Command", command],
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


def _read_cclog(n: int, kw: str) -> str:
    """同步讀取 + 解析 transcript（在 executor thread 跑）。用 Python json.loads，
    比 PS 5.1 ConvertFrom-Json 快兩個數量級，大行（抓取正文/工具結果）也只要毫秒。"""
    proj = os.path.join(os.environ.get("USERPROFILE") or os.path.expanduser("~"),
                        ".claude", "projects")
    if not os.path.isdir(proj):
        return f"⚠️ 找不到 Claude Code transcripts 目錄: {proj}"

    best = None  # (mtime, path, slug)
    kw_l = kw.lower()
    for dirpath, _dirs, names in os.walk(proj):
        slug = os.path.basename(dirpath)
        if kw and kw_l not in slug.lower():
            continue
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            fp = os.path.join(dirpath, name)
            try:
                mt = os.path.getmtime(fp)
            except OSError:
                continue
            if best is None or mt > best[0]:
                best = (mt, fp, slug)
    if best is None:
        return "⚠️ 找不到符合的 session" + (f"（篩選: {kw}）" if kw else "")

    mtime, fp, slug = best
    try:
        with open(fp, "r", encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-400:]
    except OSError as e:
        return f"讀取失敗：{e}"

    found = []
    for line in reversed(tail):
        if len(found) >= n:
            break
        # 便宜預篩：assistant 文字行同時含這兩個標記；跳過巨大的 tool_result/user 行
        if '"type":"assistant"' not in line or '"type":"text"' not in line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get("type") != "assistant":
            continue
        content = (o.get("message") or {}).get("content")
        if isinstance(content, str):
            txt = content.strip()
        elif isinstance(content, list):
            txt = "\n".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ).strip()
        else:
            txt = ""
        if txt:
            found.append(txt)

    age_min = (datetime.now().timestamp() - mtime) / 60.0
    if age_min < 5:
        age = "🟢 可能仍在進行"
    elif age_min < 60:
        age = f"🕐 {int(age_min)} 分鐘前"
    else:
        age = f"🕐 {age_min / 60:.1f} 小時前"
    kw_note = f"（篩選: {kw}）" if kw else ""
    header = (
        f"📋 Claude Code session{kw_note}\n"
        f"📁 專案: {slug}\n"
        f"📅 最後活動: {datetime.fromtimestamp(mtime).strftime('%m-%d %H:%M')} ({age})\n"
        "─────────────────────────────"
    )
    if not found:
        return header + "\n(最近 400 行內沒有 assistant 文字訊息 — 可用 /cclog <專案關鍵字> 換一個 session，如 /cclog workspace)"
    found.reverse()
    body = "\n\n--- (較早訊息) ---\n\n".join(found)
    if len(body) > 2500:
        body = "...(前略)\n" + body[-2500:]
    return header + "\n" + body


async def cclog_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """讀 ~/.claude/projects/<slug>/<session>.jsonl 的最後 N 則 assistant 文字訊息。
    v3.2.6 改寫（2026-07-09）：改用 Python 解析，不再走 PowerShell。
    起因：PS 5.1 的 ConvertFrom-Json 對 transcript 大記錄（抓取正文/工具結果/dump）
    解析極慢，單行就可能 20~30s → 整個 /cclog 撞 subprocess timeout（「這兩天失效」）。
    - 預設抓「全域最新」的 transcript；可加專案關鍵字鎖定，避免被長跑 dev/桌面 session
      蓋過（例：/cclog workspace = bridge 派發任務的 session、/cclog x = x-bookmarks）。
      參數任意順序：數字=訊息數(預設1上限3)、非數字=專案關鍵字。
    用途：手機確認「上次交代的任務/最近 session 最後說了什麼」。"""
    if not bridge_core.bridge.is_authorized(update.effective_user.id):
        await update.message.reply_text("未授權")
        return
    n = 1
    kw = ""
    for a in (context.args or []):
        if a.isdigit():
            n = min(max(int(a), 1), 3)
        elif not kw:
            kw = re.sub(r"[^A-Za-z0-9_-]", "", a)[:40]
    try:
        out = await asyncio.get_event_loop().run_in_executor(None, _read_cclog, n, kw)
    except Exception as e:
        out = f"錯誤：{e}"
    if len(out) > 8000:
        out = out[:8000] + "\n...(已截斷)"
    await update.message.reply_text(out)


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
    # v3.2.1：PS 5.1 的 Get-Process 沒有 CommandLine 屬性（PS7 才有），舊版
    # Where-Object 永遠比不中 → 一律落入「無法精確識別」分支。改用 CIM
    # （有 CommandLine），且過濾字串收緊為 telegram_bridge_claude —— 同機的
    # telegram_bridge_codex.py 是別的專案，舊的 '*bridge*' 會誤抓。
    cmd = r"""
$tmb = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*telegram_bridge_claude*' } |
    Select-Object -First 1

if ($tmb) {
    $proc = Get-Process -Id $tmb.ProcessId -ErrorAction SilentlyContinue
    if ($proc) {
        $runtime = (Get-Date) - $proc.StartTime
        Write-Output "🤖 T-M-B 狀態: ✅ 運行中 (PID $($proc.Id))"
        Write-Output ("   啟動時間: " + $proc.StartTime.ToString('yyyy-MM-dd HH:mm:ss'))
        Write-Output ("   已運行: {0}天 {1}時 {2}分" -f $runtime.Days, $runtime.Hours, $runtime.Minutes)
        Write-Output ("   記憶體: {0:N0} MB" -f ($proc.WorkingSet64/1MB))
    } else {
        Write-Output "🤖 T-M-B 狀態: ⚠️ 進程剛消失（競態），請再查一次"
    }
} else {
    Write-Output "🤖 T-M-B 狀態: ❌ 未偵測到 bridge 進程"
    Write-Output "   （TMB_Watchdog 每 5 分鐘檢查，若非手動停用將自動重啟）"
}

Write-Output ""

$os = Get-CimInstance Win32_OperatingSystem
$boot = $os.LastBootUpTime
$uptime = (Get-Date) - $boot
Write-Output ("💻 系統開機: " + $boot.ToString('yyyy-MM-dd HH:mm:ss'))
Write-Output ("   已運行: {0}天 {1}時 {2}分" -f $uptime.Days, $uptime.Hours, $uptime.Minutes)
"""
    await _run_ps_shortcut(update, cmd)
