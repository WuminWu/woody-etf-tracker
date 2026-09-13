# run_watchdog.ps1
# ------------------------------------------------------------
# 每日資料過期檢查（看門狗）。與 run_update.ps1 獨立的排程，
# 目的是在「主排程整個沒跑」時仍能發出警報（2026-09-11 事故）。
# 因此這支不做週末/假日跳過 —— 週末若發現資料落後最後一個交易日，一樣要通知。
# ------------------------------------------------------------

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$logDir = Join-Path $root "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = Join-Path $logDir ("watchdog_{0}.log" -f (Get-Date -Format "yyyyMMdd"))
function Write-Log($msg) {
    Add-Content -Path $log -Value ("{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg) -Encoding utf8
}

Write-Log "===== watchdog start ====="

# 載入 .env（TELEGRAM_*）
$envFile = Join-Path $root ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*([^#=]+)\s*=\s*(.*)\s*$') {
            Set-Item -Path ("Env:{0}" -f $matches[1].Trim()) -Value $matches[2].Trim()
        }
    }
} else {
    Write-Log "WARNING: .env 不存在，Telegram 警報會失效"
}

$out = & python "check_data_freshness.py" 2>&1
$out | ForEach-Object { Write-Log "  $_" }
Write-Log "===== watchdog done (exit=$LASTEXITCODE) ====="
