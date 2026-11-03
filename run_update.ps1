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
#
# 卡住保護（2026-10-14）：每個步驟（含 git pull/push）都經 run_step.py 執行並有逾時；
# 爬蟲階段有時間預算；上一輪卡住超過 40 分鐘就結束它；沒跑完的那一輪，下一輪與 22:30 看門狗都會通知。
# ------------------------------------------------------------

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$runStart = Get-Date

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
# 唯一來源為 tw_calendar.py（Python 端各爬蟲直接 import，這裡讀它匯出的 holidays.json），
# 避免假日表在兩處各存一份、更新年度假日時漏改其中一邊。
# Python 在 cp950 主控台印出 emoji 會丟 UnicodeEncodeError 當掉（run_summary、看門狗都是先印再發，
# 警報就會在 print 那一步中斷）。:replace 只改「印不出來的字元換成 ?」，編碼維持不變、日誌中文照舊。
if (-not $env:PYTHONIOENCODING) { $env:PYTHONIOENCODING = ":replace" }

$TW_HOLIDAYS = @{}
try {
    & python (Join-Path $root "tw_calendar.py") | Out-Null    # 重新匯出，確保與 Python 端一致
} catch {
    Write-Log "WARNING: 匯出 holidays.json 失敗：$_"
}
$holidayFile = Join-Path $root "holidays.json"
if (Test-Path $holidayFile) {
    (Get-Content $holidayFile -Raw -Encoding UTF8 | ConvertFrom-Json).PSObject.Properties |
        ForEach-Object { $TW_HOLIDAYS[$_.Name] = $_.Value }
    Write-Log ("休市日表已載入 {0} 天（來源：tw_calendar.py）" -f $TW_HOLIDAYS.Count)
} else {
    Write-Log "WARNING: 找不到 holidays.json，本次略過休市日判斷"
}
$todayStr = Get-Date -Format "yyyy-MM-dd"
if ($TW_HOLIDAYS.ContainsKey($todayStr)) {
    $reason = $TW_HOLIDAYS[$todayStr]
    Write-Log "今日 $todayStr 台股休市（$reason），跳過更新（不爬蟲、不發 Telegram、不 commit）。"
    Write-Log "===== run_update skipped (holiday: $reason) ====="
    exit 0
}

# --- 執行中標記：避免兩輪同時跑；上一輪卡住就結束它並通知 ---
# 2026-10-08 21:00 那一輪的 git push 在等登入視窗（S4U 沒有桌面，永遠等不到），
# 到 10/14 都還掛著：工作排程器的時間上限管不到它，失敗通知又排在最後一步，所以完全沒人知道。
$results = @()
$marker = Join-Path $logDir "run_in_progress.json"
$staleMin = 40     # 正常一輪 15 分鐘內；超過 40 分鐘還在跑就當作卡住
if (Test-Path $marker) {
    $m = $null
    try { $m = Get-Content $marker -Raw -Encoding UTF8 | ConvertFrom-Json } catch {}
    $p = $null
    if ($m -and $m.pid -and [int]$m.pid -ne $PID) {
        try {
            $cand = Get-Process -Id ([int]$m.pid) -ErrorAction Stop
            # PID 可能被別的程式重用：要是 PowerShell，而且啟動時間吻合，才算同一輪
            if ($cand.ProcessName -match '^(powershell|pwsh)$' -and
                [math]::Abs(($cand.StartTime - [datetime]$m.start).TotalMinutes) -lt 2) { $p = $cand }
        } catch {}
    }
    $since = if ($m -and $m.start) { $m.start } else { "不明時間" }
    if ($p) {
        $age = ((Get-Date) - $p.StartTime).TotalMinutes
        if ($age -lt $staleMin) {
            Write-Log "另一輪（PID $($p.Id)，$since 開始）還在執行，本輪跳過，避免兩輪同時寫入。"
            Write-Log "===== run_update skipped (another run in progress) ====="
            exit 0
        }
        taskkill /PID $p.Id /T /F 2>&1 | Out-Null      # 連同卡住的 git、Python、Chromium 一起結束
        $note = "{0} 開始，卡住 {1:N0} 分鐘，已強制結束" -f $since, $age
        Write-Log "  !! 上一輪（$note）"
        $results += [pscustomobject]@{ script = "上一輪卡住"; exit = 1; sec = 0; note = $note }
    } else {
        Write-Log "  !! 上一輪（$since 開始）沒有跑完就中斷了"
        $results += [pscustomobject]@{ script = "上一輪未完成"; exit = 1; sec = 0; note = "$since 開始的那一輪沒有跑完" }
    }
}
@{ pid = $PID; start = $runStart.ToString("yyyy-MM-dd HH:mm:ss") } | ConvertTo-Json |
    Set-Content -Path $marker -Encoding UTF8

# --- 執行期間不讓電腦睡著 ---
# 21:00 前後的一般睡眠可能打斷正在 push 的這一輪（日後若啟用 WakeToRun，被叫醒的電腦
# 沒人操作約 2 分鐘也會再睡回去）。程序結束時 Windows 會自動解除。
try {
    Add-Type -Namespace EtfTracker -Name Power -ErrorAction Stop -MemberDefinition `
        '[DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint esFlags);'
    [EtfTracker.Power]::SetThreadExecutionState([uint32]2147483649) | Out-Null   # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
} catch {
    Write-Log "WARNING: 無法設定執行期間防睡眠：$_"
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

# --- 以逾時保護執行一個步驟（run_step.py：逾時就結束整個程序樹，結束碼 124）---
function Invoke-Step([string[]]$cmd, [int]$timeout, [string]$label, [string]$prefix = "  ") {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $out = & python (Join-Path $root "run_step.py") --timeout $timeout -- @cmd 2>&1
    $code = $LASTEXITCODE
    $sw.Stop()
    $out | ForEach-Object { Write-Log "$prefix$_" }
    $note = $null
    if ($code -eq 124) { $note = "逾時 $timeout 秒，已強制結束" }
    if ($code -ne 0) {
        $extra = if ($note) { "（$note）" } else { "" }
        Write-Log "  !! $label 以 exit=$code 結束$extra"
    }
    return [pscustomobject]@{ script = $label; exit = $code; sec = [math]::Round($sw.Elapsed.TotalSeconds, 1); note = $note }
}

# 直接呼叫 git（commit、rebase --abort）時，stderr 每一行都會變成 ErrorRecord，
# 空行印出來是「System.Management.Automation.RemoteException」：轉成文字、略過空行
function Write-GitLog($lines) {
    foreach ($l in @($lines)) {
        $t = if ($l -is [System.Management.Automation.ErrorRecord]) { $l.Exception.Message } else { "$l" }
        if ($t -and $t.Trim() -and $t -ne "System.Management.Automation.RemoteException") { Write-Log "git: $t" }
    }
}

# --- git：不跳出任何互動視窗（S4U 沒有桌面，等登入視窗只會卡到排程上限）---
$env:GIT_TERMINAL_PROMPT = "0"
$env:GCM_INTERACTIVE = "never"
$gitNet = @("-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=60")   # 傳輸停住 60 秒就放棄

# 上一輪 git 被中途結束時會留下 index.lock，之後每一輪的 git add/commit 都會失敗
$gitDir = (git rev-parse --git-dir 2>$null)
if ($gitDir) {
    $lock = Join-Path $gitDir "index.lock"
    if ((Test-Path $lock) -and ((Get-Date) - (Get-Item $lock).LastWriteTime).TotalMinutes -gt 10) {
        Remove-Item $lock -Force -ErrorAction SilentlyContinue
        Write-Log "  已移除殘留的 .git/index.lock（上一輪的 git 被中途結束）"
    }
}

# --- 先同步遠端，避免 push 衝突 ---
# git 的結束碼也記進執行結果：pull/push 失敗原本完全靜默（日誌有 4 次 pull 失敗、1 次 401），
# 現在交給 run_summary.py 發 Telegram。
$r = Invoke-Step (@("git") + $gitNet + @("pull", "--rebase")) 120 "git pull" "git: "
$results += $r
if ($r.exit -ne 0 -and $gitDir -and
    ((Test-Path (Join-Path $gitDir "rebase-merge")) -or (Test-Path (Join-Path $gitDir "rebase-apply")))) {
    # rebase 停在一半會讓之後每一輪都失敗：先還原，下一輪再重新同步
    Write-GitLog (git rebase --abort 2>&1)
    Write-Log "  已執行 git rebase --abort，下一輪會重新同步"
}

# --- 依序執行所有更新腳本（單支失敗不中斷整體）---
# weekly_digest.py 自我守門：「本週最後一個交易日」（通常週五，週五休市則週四）發本週週報；
# 其他交易日若最近一週漏發（例如那天電腦沒開）則補發。手動補發：python weekly_digest.py --catchup
# 爬蟲清單來自 etfs.json（etf_registry.py 依序印出 check_and_update_{代號}.py）；新增 ETF 不用改這裡
$etfScripts = @(& python (Join-Path $root "etf_registry.py") | Where-Object { $_ -match '\.py$' })
$scraperSet = @{}
$etfScripts | ForEach-Object { $scraperSet[$_] = $true }
if ($etfScripts.Count -eq 0) {
    # 清單讀不到就一支爬蟲都不會跑；記成失敗讓 run_summary.py 發通知，而不是靜默結束
    Write-Log "  !! etf_registry.py 沒有輸出任何爬蟲（etfs.json 壞掉？）"
    $results += [pscustomobject]@{ script = "etfs.json"; exit = 1; sec = 0; note = "讀不到爬蟲清單" }
}
$scripts = $etfScripts + @(
    "send_queued_notifications.py", # 本輪暫存的單檔持股更新通知，依基金規模由大到小發送（排在日報之前）
    "check_and_update_index.py",
    "update_cost_basis.py",
    "record_common_actions.py",
    "export_history.py",
    "export_snapshots.py",
    "big_move_alert.py",   # 3 日內快速建倉／砍倉另發 🚨 警示（讀剛匯出的 snapshots，排在日報之前）
    "daily_digest.py",
    "weekly_digest.py",
    "sanitize_data.py"   # 最後一道防線：清除 data_*.json 內的 NaN/Inf（非法 JSON 會讓前端整頁掛掉）
)

# 逾時（秒）：歷史最長——爬蟲 97、送通知 146、週報 98。送通知被砍也安全（佇列每送一則就存檔）
$stepTimeout = @{ "send_queued_notifications.py" = 480; "daily_digest.py" = 300; "weekly_digest.py" = 300 }
$defaultTimeout = 240
# 爬蟲階段的時間預算：開跑 18 分鐘後剩下的爬蟲留給下一輪（30 分鐘後），
# 確保這一輪在下一次觸發前把已抓到的資料推上去並回報（後續步驟＋push 約 3–8 分鐘）。
$scraperBudgetMin = 18
Write-Log ("本輪共 {0} 個步驟（{1} 檔 ETF）；爬蟲階段預算 {2} 分鐘" -f
    $scripts.Count, $etfScripts.Count, $scraperBudgetMin)

# 單檔「📊 持股更新」通知改為先暫存、整輪跑完再依規模排序發送（見 notify.send_update_notification）
$env:ETF_QUEUE_NOTIFY = "1"
$skipped = @()
foreach ($s in $scripts) {
    if ($scraperSet.ContainsKey($s) -and ((Get-Date) - $runStart).TotalMinutes -gt $scraperBudgetMin) {
        $skipped += $s
        continue
    }
    Write-Log "--- 執行 $s ---"
    $t = if ($stepTimeout.ContainsKey($s)) { $stepTimeout[$s] } else { $defaultTimeout }
    $results += Invoke-Step @($s) $t $s
}
if ($skipped.Count -gt 0) {
    Write-Log "  !! 爬蟲階段超過 $scraperBudgetMin 分鐘，略過 $($skipped.Count) 支、留給下一輪：$($skipped -join ', ')"
    $results += [pscustomobject]@{ script = "時間不足"; exit = 1; sec = 0;
                                   note = "略過 $($skipped.Count) 支爬蟲，下一輪補跑" }
}

# --- 提交並推送（有變動才做）---
git add data_*.json holdings/ data_index.json 2>&1 | Out-Null
if (Test-Path "history.json")   { git add history.json   2>&1 | Out-Null }
if (Test-Path "snapshots")      { git add snapshots/     2>&1 | Out-Null }
if (Test-Path "last_digest.txt"){ git add last_digest.txt 2>&1 | Out-Null }
if (Test-Path "last_digest_overseas.txt"){ git add last_digest_overseas.txt 2>&1 | Out-Null }
if (Test-Path "digests.json")   { git add digests.json   2>&1 | Out-Null }
if (Test-Path "digests_overseas.json") { git add digests_overseas.json 2>&1 | Out-Null }
if (Test-Path "alert_state.json") { git add alert_state.json 2>&1 | Out-Null }   # 快速建倉／砍倉警示已發過哪些（防重發）
if (Test-Path "contrib_dates.json") { git add contrib_dates.json 2>&1 | Out-Null }   # 海外台股部位已計入哪天日報（防重複計算）
if (Test-Path "cost_basis.json") { git add cost_basis.json 2>&1 | Out-Null }
if (Test-Path "last_weekly.txt") { git add last_weekly.txt 2>&1 | Out-Null }
if (Test-Path "digests_weekly.json") { git add digests_weekly.json 2>&1 | Out-Null }
if (Test-Path "digests_weekly_overseas.json") { git add digests_weekly_overseas.json 2>&1 | Out-Null }

# 只看暫存區：原本連「未暫存的修改」也算，本機正在改程式時會去 commit 卻沒東西可交（commit 失敗）
git diff --staged --quiet; $staged = $LASTEXITCODE
$commitExit = 0
if ($staged -ne 0) {
    $msg = "Auto-update ETF holdings {0} (local)" -f (Get-Date -Format "yyyy-MM-dd")
    Write-GitLog (git commit -m $msg 2>&1)
    $commitExit = $LASTEXITCODE
    if ($commitExit -ne 0) {
        Write-Log "  !! git commit 以 exit=$commitExit 結束，本輪不 push"
        $results += [pscustomobject]@{ script = "git commit"; exit = $commitExit; sec = 0; note = $null }
    }
} else {
    Write-Log "無變動，略過提交"
}
# 有還沒推上去的 commit 就推：包含本輪的，以及之前 push 失敗留下的
# （原本只有「本輪有新變動」才 push，21:00 那輪 push 失敗就要等到下一個交易日）
$ahead = 0
try { $ahead = [int](git rev-list --count '@{u}..HEAD' 2>$null) } catch {}
if ($commitExit -eq 0 -and $ahead -gt 0) {
    $r = Invoke-Step (@("git") + $gitNet + @("push")) 180 "git push" "git: "
    $results += $r
    if ($r.exit -eq 0) {
        Write-Log "已推送 $ahead 個 commit"
    } else {
        Write-Log "  !! git push 失敗（網站不會更新，下一輪會再推）"
    }
}

# --- 回報：有腳本或 git 失敗就發 Telegram（同一天同一組失敗只發一次）---
# 結果檔由 PowerShell 以 UTF-8（含 BOM）寫出，run_summary.py 以 utf-8-sig 讀取
$results | ConvertTo-Json -Compress | Set-Content -Path (Join-Path $logDir "last_run_results.json") -Encoding UTF8
Write-Log "--- 執行 run_summary.py ---"
Invoke-Step @("run_summary.py") 120 "run_summary.py" | Out-Null

Remove-Item $marker -Force -ErrorAction SilentlyContinue
Write-Log ("===== run_update done ({0:N1} 分鐘) =====" -f ((Get-Date) - $runStart).TotalMinutes)
