' Pet launcher target for 启动莫拉.lnk (portable shortcut).
' Runs launcher.py under pythonw (no console window).
' NOTE: keep this file pure ASCII - WSH parses it in the system codepage,
' UTF-8 Chinese bytes get mangled under GBK.
Option Explicit
Dim sh, fso, dir
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
On Error Resume Next
sh.Run "pythonw launcher.py", 0, False
If Err.Number <> 0 Then
    Err.Clear
    sh.Run "python launcher.py", 0, False
    If Err.Number <> 0 Then
        MsgBox "Python not found. Please install Python 3.10+ (check ""Add to PATH"").", 48, "MoraPet"
    End If
End If
On Error Goto 0
