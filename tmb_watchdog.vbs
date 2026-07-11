' Silent wrapper for tmb_watchdog.ps1 - avoids a console flash every 5 minutes.
' (Task Scheduler runs wscript hidden; the bridge bat itself still opens visibly when restarted.)
Dim fso, here
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
CreateObject("Wscript.Shell").Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & here & "\tmb_watchdog.ps1""", 0, False
