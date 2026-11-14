# run_institutional.ps1
# ------------------------------------------------------------
# 晚間補抓「三大法人買賣超」分頁的資料（主要是融資融券：證交所約 22:00 才公布，
# 主排程 run_update.ps1 最後一輪是 21:00，抓不到）。
# 只跑 institutional.py（最近 5 個交易日缺的部分補齊），有變動才 commit institutional/ 並 push。
# 由工作排程器 ETF_Tracker_Institutional_Late 在週一～五 22:15、22:45、23:15 呼叫（register_task.ps1 註冊）。
# 日誌：logs\institutional_<日期>.log
# ------------------------------------------------------------

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
if (-not $env:PYTHONIOENCODING) { $env:PYTHONIOENCODING = ":replace" }

$logDir = Join-Path $root "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = Join-Path $logDir ("institutional_{0}.log" -f (Get-Date -Format "yyyyMMdd"))
function Write-Log($msg) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -Path $log -Value $line -Encoding utf8
    Write-Host $line
}
function Write-Out($lines) {
    foreach ($l in @($lines)) {
        $t = if ($l -is [System.Management.Automation.ErrorRecord]) { $l.Exception.Message } else { "$l" }
        if ($t -and $t.Trim() -and $t -ne "System.Management.Automation.RemoteException") { Write-Log "  $t" }
    }
}

Write-Log "===== run_institutional start ====="

# 主排程還在跑（例如卡住被拖到很晚）就不要同時動 git
$marker = Join-Path $logDir "run_in_progress.json"
if (Test-Path $marker) {
    try {
        $m = Get-Content $marker -Raw -Encoding UTF8 | ConvertFrom-Json
        $p = Get-Process -Id ([int]$m.pid) -ErrorAction Stop
        if ($p.ProcessName -match '^(powershell|pwsh)$') {
            Write-Log "run_update.ps1（PID $($m.pid)）還在執行，本輪跳過。"
            exit 0
        }
    } catch {}
}

# git：不跳出任何互動視窗（S4U 沒有桌面）；傳輸停住 60 秒就放棄
$env:GIT_TERMINAL_PROMPT = "0"
$env:GCM_INTERACTIVE = "never"
$gitNet = @("-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=60")
$step = Join-Path $root "run_step.py"

Write-Out (& python $step --timeout 120 -- git @gitNet pull --rebase 2>&1)
if ($LASTEXITCODE -ne 0) {
    $gitDir = (git rev-parse --git-dir 2>$null)
    if ($gitDir -and ((Test-Path (Join-Path $gitDir "rebase-merge")) -or (Test-Path (Join-Path $gitDir "rebase-apply")))) {
        Write-Out (git rebase --abort 2>&1)
    }
    Write-Log "  !! git pull 失敗，本輪跳過（下一輪或明天主排程會再同步）"
    exit 1
}

Write-Out (& python $step --timeout 300 institutional.py 2>&1)

git add institutional/ 2>&1 | Out-Null
git diff --staged --quiet -- institutional/
if ($LASTEXITCODE -ne 0) {
    $msg = "Auto-update institutional data {0} (local, late)" -f (Get-Date -Format "yyyy-MM-dd")
    # 只提交 institutional/，不會把暫存區裡其他東西一起帶進去
    Write-Out (git commit -m $msg -- institutional/ 2>&1)
    if ($LASTEXITCODE -eq 0) {
        Write-Out (& python $step --timeout 180 -- git @gitNet push 2>&1)
        if ($LASTEXITCODE -ne 0) { Write-Log "  !! git push 失敗（明天主排程會一併推上去）" }
    }
} else {
    Write-Log "無變動，略過提交"
}
Write-Log "===== run_institutional done ====="
