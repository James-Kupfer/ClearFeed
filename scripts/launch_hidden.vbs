' Launch run_ingestion.bat as a completely hidden background process.
' No console window appears. The process continues until killed via Task Manager.
'
' Usage:
'   Double-click launch_hidden.vbs, or:
'   wscript "C:\path\to\ClearFeed\scripts\launch_hidden.vbs"
'
' To stop: open Task Manager -> Details tab -> kill python.exe (the ingest process)
'   or kill wscript.exe (the launcher, which also stops the loop).

Dim oShell, scriptDir, batPath
Set oShell  = CreateObject("WScript.Shell")
Set oFSO    = CreateObject("Scripting.FileSystemObject")

scriptDir = oFSO.GetParentFolderName(WScript.ScriptFullName)
batPath   = scriptDir & "\run_ingestion_service.bat"

' Window style 0 = hidden, bWaitOnReturn = False (fire and forget)
oShell.Run "cmd /c """ & batPath & """", 0, False

Set oShell = Nothing
Set oFSO   = Nothing
