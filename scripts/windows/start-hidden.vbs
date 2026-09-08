' Starts OpenTelegramStorage without a console window (used by the autostart shortcut).
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
Set sh = CreateObject("WScript.Shell")
sh.Run """" & here & "\OpenTelegramStorage.cmd""", 0, False
