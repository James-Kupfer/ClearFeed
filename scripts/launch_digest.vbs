' Launch a ClearFeed digest dispatch as a hidden, one-shot process.
' Runs to completion then exits — designed for Windows Scheduled Tasks.
'
' Usage (manual):
'   wscript "C:\...\scripts\launch_digest.vbs" action_digest
'
' Usage (Scheduled Task):
'   Program/script : wscript.exe
'   Arguments      : "C:\Users\James Kupfer\Claude\ClearFeed\scripts\launch_digest.vbs" action_digest
'
' The profile JSON must exist at:
'   <ClearFeed root>\profiles\<profile_name>.yaml

If WScript.Arguments.Count = 0 Then
    WScript.Echo "Usage: launch_digest.vbs <profile_name>"
    WScript.Echo "Example: launch_digest.vbs action_digest"
    WScript.Quit 1
End If

Dim oShell, oFSO, scriptDir, batPath, profileName, cmd
Set oShell = CreateObject("WScript.Shell")
Set oFSO   = CreateObject("Scripting.FileSystemObject")

scriptDir   = oFSO.GetParentFolderName(WScript.ScriptFullName)
batPath     = scriptDir & "\..\clearfeed.bat"
profileName = WScript.Arguments(0)

cmd = "cmd /c """ & batPath & """ dispatch profiles\" & profileName & ".yaml"

' Window style 0 = hidden. bWaitOnReturn = True so the Scheduled Task
' tracks actual completion and reports success/failure correctly.
oShell.Run cmd, 0, True

Set oShell = Nothing
Set oFSO   = Nothing
