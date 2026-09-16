# verify_tasks.ps1 — 檢視 ETF Tracker 兩個排程的狀態
# 單獨成檔的原因：在 .bat 內用 powershell -Command 內嵌這段時，
# 引號會被批次檔剝掉，導致 @{N='LogonType';...} 被誤判成指令。
$names = 'ETF_Tracker_Daily_Update', 'ETF_Tracker_Freshness_Watchdog'
$tasks = Get-ScheduledTask -TaskName $names -ErrorAction SilentlyContinue
if (-not $tasks) {
    Write-Host "找不到任何 ETF Tracker 排程（尚未註冊）" -ForegroundColor Red
    exit 1
}
$tasks | Select-Object TaskName,
                       @{N='LogonType'; E={$_.Principal.LogonType}},
                       State |
    Format-Table -AutoSize
foreach ($t in $tasks) {
    $info = $t | Get-ScheduledTaskInfo
    Write-Host ("  {0}  下次執行：{1}" -f $t.TaskName, $info.NextRunTime)
}
$bad = $tasks | Where-Object { $_.Principal.LogonType -ne 'S4U' }
if ($tasks.Count -eq 2 -and -not $bad) {
    Write-Host "`n[成功] 兩個排程都已設為 S4U（不需登入即可執行）" -ForegroundColor Green
} else {
    Write-Host "`n[注意] 尚未全部設定完成，請以系統管理員身分重新執行註冊。" -ForegroundColor Yellow
}
