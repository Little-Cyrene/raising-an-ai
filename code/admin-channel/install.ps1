# 受限提权通道 · 安装脚本（需要管理员权限）
#
# 装什么：一个每 2 分钟跑一次的计划任务，用当前用户 + 最高权限运行
#        （/RL HIGHEST + /IT：只在用户登录时跑，但用最高权限）
#
# 为什么是 2 分钟而不是 1 分钟：
#   1 分钟太频繁（每次都起一个 powershell，有开销）
#   2 分钟已经是"坏了也能很快发现"的量级

$ErrorActionPreference = "Stop"

# 1) 检查管理员
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "需要管理员权限。右键盘 Powershell，选『以管理员身份运行』。"
    exit 1
}

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$vbs = Join-Path $here "hidden.vbs"
$ps1 = Join-Path $here "executor.ps1"

if (-not (Test-Path $vbs)) { Write-Host "找不到 hidden.vbs"; exit 1 }
if (-not (Test-Path $ps1)) { Write-Host "找不到 executor.ps1"; exit 1 }

$taskName = "AgentAdminOp"

# 2) 删掉旧的（如果有）
schtasks /delete /tn $taskName /f 2>$null | Out-Null

# 3) 建任务：每 2 分钟，当前用户，最高权限，只在登录时跑
$tr = "wscript.exe //B //NoLogo ""$vbs"""
schtasks /create /tn $taskName /tr $tr /sc minute /mo 2 /ru $env:USERNAME /rl HIGHEST /it /f

if ($LASTEXITCODE -ne 0) {
    Write-Host "创建任务失败（exit $LASTEXITCODE）"
    exit 1
}

Write-Host ""
Write-Host "装好了。检查："
schtasks /query /tn $taskName /fo LIST /v |
    Select-String "Task Name|Task To Run|Run As User|Schedule Type|Repeat: Every|Status" |
    ForEach-Object { "  " + $_.Line.Trim() }

Write-Host ""
Write-Host "立刻跑一次试试："
schtasks /run /tn $taskName
Write-Host "  然后跑：python client.py status"