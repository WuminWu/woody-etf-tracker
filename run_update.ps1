# run_update.ps1
# ------------------------------------------------------------
# 本機版每日 ETF 更新（取代 GitHub Actions cron，避免排程延遲）。
# 由 Windows 工作排程器（Task Scheduler）在台灣傍晚時段每 30 分鐘呼叫一次。
# 腳本本身具 idempotent 防護：抓到當日資料才更新、digest 有 marker 防重送，
# 因此一天被呼叫多次也安全。
#
# 需要的本機檔案：
#   .env            -> TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
#   google_sa.json  -> Google 服務帳戶金鑰（整份 JSON，內容會塞進 GOOGLE_SHEETS_CREDENTIALS）
# 兩者皆已在 .gitignore，不會被推上 GitHub。
# ------------------------------------------------------------

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# --- 日誌 ---
$logDir = Join-Path $root "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = Join-Path $logDir ("run_{0}.log" -f (Get-Date -Format "yyyyMMdd"))
function Write-Log($msg) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -Path $log -Value $line -Encoding utf8
    Write-Host $line
}

Write-Log "===== run_update start ====="

# --- 跨午夜守門：只允許台灣 14:00–23:59 執行，避免把隔日誤判為今天 ---
$hour = (Get-Date).Hour
if ($hour -lt 14) {
    Write-Log "Current hour $hour < 14 — 超出允許時段（電腦可能剛從睡眠喚醒），跳過本次。"
    Write-Log "===== run_update skipped ====="
    exit 0
}

# --- 載入 .env 到行程環境變數 ---
$envFile = Join-Path $root ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*([^#=]+)\s*=\s*(.*)\s*$') {
            $name = $matches[1].Trim()
            $val  = $matches[2].Trim()
            Set-Item -Path ("Env:{0}" -f $name) -Value $val
        }
    }
    Write-Log ".env 已載入"
} else {
    Write-Log "WARNING: .env 不存在，Telegram 推播可能失效"
}

# --- 載入 Google Sheets 憑證 ---
$saFile = Join-Path $root "google_sa.json"
if (Test-Path $saFile) {
    $env:GOOGLE_SHEETS_CREDENTIALS = (Get-Content $saFile -Raw)
    Write-Log "google_sa.json 已載入 GOOGLE_SHEETS_CREDENTIALS"
} else {
    Write-Log "WARNING: google_sa.json 不存在 — Sheets 寫入/匯出（共同加減碼、歷史、快照）會被跳過"
}

# --- 先同步遠端，避免 push 衝突 ---
git pull --rebase 2>&1 | ForEach-Object { Write-Log "git: $_" }

# --- 依序執行所有更新腳本（單支失敗不中斷整體）---
$scripts = @(
    "check_and_update_00981A.py",
    "check_and_update_00400A.py",
    "check_and_update_00403A.py",
    "check_and_update_00404A.py",
    "check_and_update_00980A.py",
    "check_and_update_00985A.py",
    "check_and_update_00991A.py",
    "check_and_update_00992A.py",
    "check_and_update_00982A.py",
    "check_and_update_00987A.py",
    "check_and_update_00993A.py",
    "check_and_update_00995A.py",
    "check_and_update_00996A.py",
    "check_and_update_00405A.py",
    "check_and_update_00988A.py",   # 海外 T+1，一併在本機跑
    "check_and_update_index.py",
    "record_common_actions.py",
    "export_history.py",
    "export_snapshots.py",
    "daily_digest.py"
)
foreach ($s in $scripts) {
    Write-Log "--- 執行 $s ---"
    try {
        $out = & python $s 2>&1
        $out | ForEach-Object { Write-Log "  $_" }
    } catch {
        Write-Log "  ERROR: $_"
    }
}

# --- 提交並推送（有變動才做）---
git add data_*.json holdings/ data_index.json 2>&1 | Out-Null
if (Test-Path "history.json")   { git add history.json   2>&1 | Out-Null }
if (Test-Path "snapshots")      { git add snapshots/     2>&1 | Out-Null }
if (Test-Path "last_digest.txt"){ git add last_digest.txt 2>&1 | Out-Null }
if (Test-Path "digests.json")   { git add digests.json   2>&1 | Out-Null }

git diff --quiet; $unstaged = $LASTEXITCODE
git diff --staged --quiet; $staged = $LASTEXITCODE
if ($unstaged -ne 0 -or $staged -ne 0) {
    $msg = "Auto-update ETF holdings {0} (local)" -f (Get-Date -Format "yyyy-MM-dd")
    git commit -m $msg 2>&1 | ForEach-Object { Write-Log "git: $_" }
    git push 2>&1 | ForEach-Object { Write-Log "git: $_" }
    Write-Log "已提交並推送"
} else {
    Write-Log "無變動，略過提交"
}

Write-Log "===== run_update done ====="
