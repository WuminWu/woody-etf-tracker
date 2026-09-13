' run_watchdog.vbs
' 以「完全隱藏視窗」方式啟動 run_watchdog.ps1，避免排程觸發時閃黑色命令視窗。
' WScript.Shell.Run 第二參數 0 = 隱藏視窗；第三參數 False = 不等待。
' 由 register_task.ps1 註冊為排程動作：wscript.exe "…\run_watchdog.vbs"
Dim sh, scriptDir
Set sh = CreateObject("WScript.Shell")
scriptDir = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & scriptDir & "run_watchdog.ps1""", 0, False
