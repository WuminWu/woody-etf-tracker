# register_task.ps1
# ------------------------------------------------------------
# 在 Windows 工作排程器註冊「ETF 每日更新」任務。
# 週一至週五，台灣傍晚 18:00 起每 30 分鐘觸發一次，到 21:00 結束（共 7 次）。
# 透過 wscript.exe + run_hidden.vbs 隱藏啟動 → 觸發時不會閃黑色命令視窗。
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
$vbs      = "D:\Self_Tools\ETF_Tracker\run_hidden.vbs"

if (-not (Test-Path $script)) {
    Write-Error "找不到 $script"
    exit 1
}
if (-not (Test-Path $vbs)) {
    Write-Error "找不到 $vbs（隱藏啟動器）"
    exit 1
}

# 動作：用 wscript 執行隱藏啟動器 → run_update.ps1 完全不閃視窗
$action = New-ScheduledTaskAction `
    -Execute "wscript.exe" `
    -Argument "`"$vbs`""

# 觸發：週一～週五 18:00，之後每 30 分鐘重複，持續 3 小時（涵蓋到 21:00）
# 週末不觸發；週五休市導致週報漏發時，改由下週一的 weekly_digest.py 自動補發
$trigger = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At 18:00
# RepetitionDuration 用分鐘 (180) 確保涵蓋到 18:00+3h = 21:00 的最後一次觸發。
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At 18:00 `
    -RepetitionInterval (New-TimeSpan -Minutes 30) `
    -RepetitionDuration (New-TimeSpan -Minutes 180)).Repetition

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
Write-Host "  觸發：週一～五 18:00–21:00，每 30 分鐘一次"
Write-Host "  立即測試： Start-ScheduledTask -TaskName `"$taskName`""
Write-Host "  查看狀態： Get-ScheduledTask -TaskName `"$taskName`" | Get-ScheduledTaskInfo"
Write-Host "  日誌： D:\Self_Tools\ETF_Tracker\logs\run_<日期>.log"
