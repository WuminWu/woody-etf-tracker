# register_task.ps1
# ------------------------------------------------------------
# 在 Windows 工作排程器註冊「ETF 每日更新」任務。
# 週一至週五，台灣傍晚 17:00 起每 30 分鐘觸發一次，到 21:30 結束（共 10 次）。
# run_update.ps1 內含 idempotent 防護，多次觸發安全。
#
# 用法（請用「系統管理員」開 PowerShell 後執行一次）：
#   powershell -ExecutionPolicy Bypass -File D:\Self_Tools\ETF_Tracker\register_task.ps1
#
# 移除任務：
#   Unregister-ScheduledTask -TaskName "ETF_Tracker_Daily_Update" -Confirm:$false
# ------------------------------------------------------------

$taskName = "ETF_Tracker_Daily_Update"
$script   = "D:\Self_Tools\ETF_Tracker\run_update.ps1"

if (-not (Test-Path $script)) {
    Write-Error "找不到 $script"
    exit 1
}

# 動作：以隱藏視窗執行 run_update.ps1
$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`""

# 觸發：週一～週五 17:00，之後每 30 分鐘重複，持續 4.5 小時（涵蓋到 21:30）
$trigger = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At 17:00
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At 17:00 `
    -RepetitionInterval (New-TimeSpan -Minutes 30) `
    -RepetitionDuration (New-TimeSpan -Hours 4.5)).Repetition

# 設定：電腦使用電池時也執行；錯過排程時間（剛開機）盡快補跑
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20) `
    -MultipleInstances IgnoreNew

# 以目前使用者身分執行（需登入；可讀取 .env / google_sa.json）
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName `
    -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "每日抓取主動式 ETF 持股並推送至 GitHub Pages（取代 GitHub Actions cron）" `
    -Force

Write-Host ""
Write-Host "已註冊工作排程：$taskName"
Write-Host "  觸發：週一～五 17:00–21:30，每 30 分鐘一次"
Write-Host "  立即測試： Start-ScheduledTask -TaskName `"$taskName`""
Write-Host "  查看狀態： Get-ScheduledTask -TaskName `"$taskName`" | Get-ScheduledTaskInfo"
Write-Host "  日誌： D:\Self_Tools\ETF_Tracker\logs\run_<日期>.log"
