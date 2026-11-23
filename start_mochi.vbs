Set fs = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
launcher = fs.BuildPath(fs.GetParentFolderName(WScript.ScriptFullName), "start_mochi.bat")
shell.Run Chr(34) & launcher & Chr(34), 7, False
