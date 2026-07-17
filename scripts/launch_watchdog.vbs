' Runs watchdog_ingestion.ps1 hidden, WAITING for it to finish (unlike
' launch_hidden.vbs, which is fire-and-forget). The watchdog is a short, bounded
' check -- waiting lets Task Scheduler's MultipleInstancesPolicy=IgnoreNew
' correctly block overlapping watchdog runs if one is ever slow to finish.
'
' Usage: wscript "C:\path\to\ClearFeed\scripts\launch_watchdog.vbs"

Dim oShell, scriptDir, psPath, cmd
Set oShell = CreateObject("WScript.Shell")
Set oFSO   = CreateObject("Scripting.FileSystemObject")

scriptDir = oFSO.GetParentFolderName(WScript.ScriptFullName)
psPath    = scriptDir & "\watchdog_ingestion.ps1"

cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & psPath & """"

' Window style 0 = hidden, bWaitOnReturn = True (wait for the check to finish)
oShell.Run cmd, 0, True

Set oShell = Nothing
Set oFSO   = Nothing
