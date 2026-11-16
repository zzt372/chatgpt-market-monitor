' Starts the market monitor tray icon (scripts\pc\tray.ps1) without a console window.
Option Explicit
Dim fso, shell, root
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
root = fso.GetParentFolderName(WScript.ScriptFullName)
shell.Run "powershell.exe -NoLogo -NoProfile -NonInteractive -STA -ExecutionPolicy Bypass -File """ & root & "\scripts\pc\tray.ps1""", 0, False
