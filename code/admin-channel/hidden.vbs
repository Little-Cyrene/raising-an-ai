' 受限提权通道 · 隐藏窗口启动器
'
' 为什么需要这个：
'   `powershell -WindowStyle Hidden` 挡不住任务计划程序创建的控制台窗口。
'   任务每分钟闪一下黑框，用户能看见 —— 这个 VBS 就是为了消掉它。
'
' 用法：wscript.exe //B //NoLogo hidden.vbs
'   Run(cmd, 0, False) 里的 0 = 隐藏窗口，False = 不等待

Dim shell, fso, here, target
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
target = here & "\executor.ps1"
shell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & target & """", 0, False