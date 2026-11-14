' run_institutional.vbs
' Hidden launcher for run_institutional.ps1 (no console window flash).
' Registered by register_task.ps1 as: wscript.exe "...\run_institutional.vbs"
Dim sh, scriptDir
Set sh = CreateObject("WScript.Shell")
scriptDir = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & scriptDir & "run_institutional.ps1""", 0, False
