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

# --- 週末守門：週六/日一律整個跳過（不跑爬蟲、不發任何報告）---
# 註：週五休市導致週報漏發的情況，改由下一個交易日（週一）的 weekly_digest.py 自動補發，
#     不再需要週六觸發（排程觸發日已改回週一~五）。
$dow = (Get-Date).DayOfWeek
if ($dow -eq "Saturday" -or $dow -eq "Sunday") {
    Write-Log "今日為週末（$dow），台股休市，跳過更新。"
    Write-Log "===== run_update skipped (weekend) ====="
    exit 0
}

# --- 台股休市日守門：休市日整個跳過（不爬蟲、不發 Telegram、不 commit）---
# 日期 → 休市原因（僅列平日；週末排程本就不跑）。每年初更新固定假日；
# 颱風假等臨時休市依中央氣象署/證交所公告隨時加入。
# 來源：臺灣證券交易所 https://www.twse.com.tw/zh/trading/holiday.html
$TW_HOLIDAYS_2026 = @{
    "2026-01-01" = "元旦"
    "2026-02-12" = "春節(封關結算)"; "2026-02-13" = "春節(封關結算)"
    "2026-02-16" = "春節"; "2026-02-17" = "春節"; "2026-02-18" = "春節"
    "2026-02-19" = "春節"; "2026-02-20" = "春節"
    "2026-02-27" = "和平紀念日補假"
    "2026-04-03" = "兒童節補假"; "2026-04-06" = "清明節補假"
    "2026-05-01" = "勞動節"
    "2026-06-19" = "端午節"
    "2026-07-10" = "颱風假（臨時休市）"
    "2026-09-25" = "中秋節"; "2026-09-28" = "教師節"
    "2026-10-09" = "國慶日補假"; "2026-10-26" = "光復節補假"
    "2026-12-25" = "行憲紀念日"
}
$todayStr = Get-Date -Format "yyyy-MM-dd"
if ($TW_HOLIDAYS_2026.ContainsKey($todayStr)) {
    $reason = $TW_HOLIDAYS_2026[$todayStr]
    Write-Log "今日 $todayStr 台股休市（$reason），跳過更新（不爬蟲、不發 Telegram、不 commit）。"
    Write-Log "===== run_update skipped (holiday: $reason) ====="
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
# weekly_digest.py 自我守門：週五發本週週報；週一~四若上週週報漏發（週五休市）則補發
$scripts = @(
    "check_and_update_00981A.py",
    "check_and_update_00400A.py",
    "check_and_update_00403A.py",
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
    "check_and_update_00997A.py",   # 海外（美股為主），capitalfund
    "check_and_update_index.py",
    "update_cost_basis.py",
    "record_common_actions.py",
    "export_history.py",
    "export_snapshots.py",
    "daily_digest.py",
    "weekly_digest.py"
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
if (Test-Path "last_digest_overseas.txt"){ git add last_digest_overseas.txt 2>&1 | Out-Null }
if (Test-Path "digests.json")   { git add digests.json   2>&1 | Out-Null }
if (Test-Path "digests_overseas.json") { git add digests_overseas.json 2>&1 | Out-Null }
if (Test-Path "cost_basis.json") { git add cost_basis.json 2>&1 | Out-Null }
if (Test-Path "last_weekly.txt") { git add last_weekly.txt 2>&1 | Out-Null }
if (Test-Path "digests_weekly.json") { git add digests_weekly.json 2>&1 | Out-Null }
if (Test-Path "digests_weekly_overseas.json") { git add digests_weekly_overseas.json 2>&1 | Out-Null }

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
