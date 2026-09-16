@echo off
chcp 65001 >nul
title ETF Tracker - 註冊排程 (S4U)
echo ============================================
echo  ETF Tracker - register scheduled tasks
echo ============================================
echo.
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo [X] 權限不足 / Not elevated.
  echo     請關掉這個視窗，改成「對本檔按右鍵 -^> 以系統管理員身分執行」。
  echo.
  pause
  exit /b 1
)
echo [OK] 已取得系統管理員權限，開始註冊...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0register_task.ps1"
echo.
echo ============== 驗證結果 ==============
powershell -NoProfile -Command "Get-ScheduledTask ETF_Tracker_Daily_Update,ETF_Tracker_Freshness_Watchdog -ErrorAction SilentlyContinue | Select-Object TaskName,@{N=LogonType;E={$_.Principal.LogonType}},State | Format-Table -AutoSize"
echo  （兩個任務都要出現，且 LogonType 必須是 S4U）
echo.
pause
