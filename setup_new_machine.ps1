# setup_new_machine.ps1
# ------------------------------------------------------------
# 換機 / 災難復原：在一台新的 Windows 電腦上重建本機排程環境。
#
# 前置（手動，本腳本不做）：
#   1. 安裝 Git 與 Python 3.11+（勾選 Add to PATH）
#   2. git clone https://github.com/WuminWu/woody-etf-tracker.git
#   3. 把兩個機密檔放回 repo 根目錄（不在 git 裡，需自行保管或重新產生）：
#        .env            （Telegram 金鑰；遺失可向 @BotFather 取回 token）
#        google_sa.json  （Google 服務帳戶金鑰；遺失可到 Google Cloud Console
#                          → IAM → 服務帳戶 etf-tracker-service@etf-tracker-494113
#                          → 金鑰 → 新增金鑰 JSON 重新下載）
#   4. 設定 git push 認證（git credential manager 或 PAT）
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File setup_new_machine.ps1
# ------------------------------------------------------------

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
Write-Host "=== ETF Tracker 換機設定 ($root) ===" -ForegroundColor Cyan

# 1. 檢查 Python / Git
foreach ($cmd in @("python", "git")) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        Write-Error "找不到 $cmd，請先安裝並加入 PATH。"; exit 1
    }
}
python --version; git --version

# 2. 安裝 Python 套件
Write-Host "`n--- 安裝 Python 套件 ---" -ForegroundColor Cyan
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install google-api-python-client google-auth   # requirements 未列，需另裝

# 3. 安裝 Playwright Chromium（部分 ETF 爬蟲需要）
Write-Host "`n--- 安裝 Playwright Chromium ---" -ForegroundColor Cyan
python -m playwright install chromium

# 4. 檢查機密檔
Write-Host "`n--- 檢查機密檔 ---" -ForegroundColor Cyan
$ok = $true
if (Test-Path ".env")           { Write-Host "  .env 存在" -ForegroundColor Green }
else { Write-Host "  缺少 .env（Telegram 推播會失效）" -ForegroundColor Yellow; $ok = $false }
if (Test-Path "google_sa.json") { Write-Host "  google_sa.json 存在" -ForegroundColor Green }
else { Write-Host "  缺少 google_sa.json（Sheets 寫入/歷史/快照會被跳過）" -ForegroundColor Yellow; $ok = $false }

# 5. 註冊工作排程
Write-Host "`n--- 註冊工作排程 ---" -ForegroundColor Cyan
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root "register_task.ps1")

Write-Host "`n=== 完成 ===" -ForegroundColor Cyan
if (-not $ok) {
    Write-Host "⚠️ 機密檔不齊，補齊後再執行一次 run_update.ps1 測試：" -ForegroundColor Yellow
    Write-Host "   powershell -ExecutionPolicy Bypass -File run_update.ps1"
} else {
    Write-Host "可立即測試：Start-ScheduledTask -TaskName 'ETF_Tracker_Daily_Update'" -ForegroundColor Green
}
Write-Host "別忘了：在舊機器上 Unregister-ScheduledTask，避免兩台同時 push。"
