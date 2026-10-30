# 受限提权通道 · 执行端
#
# 设计要点：
#   1. 动作**硬编码白名单**（switch），不接受任意命令字符串
#   2. 读请求文件 → 执行 → 写结果文件，全程记日志
#   3. 每个动作都要「失败也不致命」（不删东西、不关机）
#
# 这个文件里每个 "xxx" { ... } 都是白名单里的一个动作。
# client.py list 会正则扫这个文件列出动作名，所以**格式要保持一致**。

$base = Split-Path -Parent $MyInvocation.MyCommand.Path
$req  = Join-Path $base "admin-req.json"
$res  = Join-Path $base "admin-res.json"
$log  = Join-Path $base "admin-op.log"

function W($m) {
    "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m |
        Add-Content -Path $log -Encoding utf8
}

# ── 顺手自愈：每次轮询都确认"我自己"的服务还活着 ──────────
#    这一段是刻意放在读请求**之前**的：即使没有新请求，
#    轮询本身也在做保活。这是"通道顺便当看门狗"的用法。
$svcPort = 8790
$alive = (netstat -ano | Select-String (":{0}\s" -f $svcPort) | Select-String "LISTENING")
if (-not $alive) {
    $starter = Join-Path $base "start-service.ps1"
    if (Test-Path $starter) {
        Start-Process -FilePath "powershell.exe" -ArgumentList "-NoProfile", "-File", $starter -WindowStyle Hidden
        W "服务不在，已拉起（端口 $svcPort）"
    }
}

if (-not (Test-Path $req)) { exit 0 }

try {
    $r = Get-Content $req -Raw -Encoding UTF8 | ConvertFrom-Json
} catch {
    W "坏请求，丢弃"
    Remove-Item $req -Force -ErrorAction SilentlyContinue
    exit 0
}

$id = $r.id
$action = $r.action
Remove-Item $req -Force -ErrorAction SilentlyContinue

W "REQ id=$id cmd=$action"

$ok = $true
$out = ""

try {
    switch ($action) {

        "service-status" {
            $p = (netstat -ano | Select-String (":{0}\s" -f $svcPort) | Select-String "LISTENING")
            if ($p) { $out = "端口 $svcPort = LISTEN" } else { $out = "端口 $svcPort = DOWN" }
        }

        "service-restart" {
            $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
                Where-Object { $_.CommandLine -like "*service.py*" }
            foreach ($p in $procs) { & cmd /c "taskkill /F /PID $($p.ProcessId)" 2>&1 | Out-Null }
            Start-Sleep -Seconds 3
            $starter = Join-Path $base "start-service.ps1"
            if (Test-Path $starter) { Start-Process -FilePath "powershell.exe" -ArgumentList "-NoProfile", "-File", $starter -WindowStyle Hidden }
            Start-Sleep -Seconds 5
            $p2 = (netstat -ano | Select-String (":{0}\s" -f $svcPort) | Select-String "LISTENING")
            $out = if ($p2) { "已重启，端口 LISTEN" } else { "重启了但端口还没起来" }
            if (-not $p2) { $ok = $false }
        }

        "service-matrix" {
            $ports = @(3080, 3000, 8790)
            $parts = @()
            foreach ($pt in $ports) {
                $up = (netstat -ano | Select-String (":{0}\s" -f $pt) | Select-String "LISTENING")
                $parts += ("{0}=" -f $pt) + $(if ($up) { "UP" } else { "DOWN" })
            }
            $out = ($parts -join "  ")
        }

        "service-ensure" {
            $p = (netstat -ano | Select-String (":{0}\s" -f $svcPort) | Select-String "LISTENING")
            if ($p) {
                $out = "已在跑（端口 $svcPort LISTEN）"
            } else {
                $starter = Join-Path $base "start-service.ps1"
                if (Test-Path $starter) {
                    Start-Process -FilePath "powershell.exe" -ArgumentList "-NoProfile", "-File", $starter -WindowStyle Hidden
                    Start-Sleep -Seconds 6
                }
                $p2 = (netstat -ano | Select-String (":{0}\s" -f $svcPort) | Select-String "LISTENING")
                $out = if ($p2) { "刚才不在，已拉起，现在 LISTEN" } else { "拉不起来" }
                if (-not $p2) { $ok = $false }
            }
        }

        "disk-usage" {
            $d = Get-PSDrive C -ErrorAction SilentlyContinue
            if ($d) { $out = "C 盘可用 {0:N1} GB / 共 {1:N1} GB" -f ($d.Free/1GB), (($d.Used+$d.Free)/1GB) }
            else { $out = "拿不到磁盘信息"; $ok = $false }
        }

        "uptime" {
            $os = Get-CimInstance Win32_OperatingSystem
            $up = (Get-Date) - $os.LastBootUpTime
            $out = "开机 {0:N1} 小时" -f $up.TotalHours
        }

        default {
            $ok = $false
            $out = "未知动作：$action（白名单里没有）"
            W "拒绝未知动作：$action"
        }
    }
} catch {
    $ok = $false
    $out = "执行出错：$($_.Exception.Message)"
}

$body = @{ id = $id; ok = $ok; out = $out; ts = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') } |
    ConvertTo-Json -Compress -Depth 3
[System.IO.File]::WriteAllText($res, $body, (New-Object System.Text.UTF8Encoding $false))

W "RES ok=$ok out=$($out -replace '\s+', ' ')"
exit 0
