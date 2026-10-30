# admin-channel

> 给低权限 agent 一个**受限的**提权通道：白名单动作、轮询执行、全程留痕。
> 不是「让 agent 变成管理员」，是「给它一个只能用特定几个动作的按钮」。

## 为什么需要这个

在 Windows 上，一个跑在低完整性级别（Low IL）的进程会遇到这些：

```
PostMessage WM_CLOSE      → Access is denied   （想关窗口）
SetForegroundWindow       → Access is denied   （想切前台）
SendInput                 → Access is denied   （想模拟按键）
schtasks /query | /run    → Access is denied   （想查/触发计划任务）
```

**但读屏幕像素是能用的**（`ImageGrab`）。所以它的处境是：**看得见，动不了**。

它想做的事里，有些是合理的（重启一个自己管理的服务、确认某个进程还活着），
但都需要管理员权限。

**直接把 agent 提成管理员？** 那它一次判断失误就能做任何事。

**所以走第三条路**：给它一个**白名单动作**的通道。

## 架构

```
┌─────────────────────┐
│ agent（低权限）      │
│  写 req.json         │
│  {id, action}        │
└──────────┬──────────┘
           │ 文件当邮箱
           ▼
┌─────────────────────┐
│ 计划任务（高权限）    │
│  每 2 分钟轮询：      │
│   读 req.json        │
│   查动作在白名单吗    │
│   执行               │
│   写 res.json        │
│   记账               │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│ agent 读 res.json    │
│  按 id 匹配          │
└─────────────────────┘
```

**四个关键设计，每个都是被逼出来的**：

### ① 为什么要用文件当邮箱

因为 agent **没法触发计划任务**（`schtasks /run` 被 UAC 拦）。

所以只能**反向**：让高权限那边**定时来看**，而不是 agent 去敲它。

**代价**：最长要等一个轮询周期（我们设 2 分钟）。
**好处**：没有"即时提权"这个口子——一个被劫持的 agent 也只能排队等。

### ② 为什么动作必须白名单

```powershell
# 不好
param($cmd)
Invoke-Expression $cmd        # ← 这就是给管理员权限了

# 好
switch ($action) {
  "service-restart" { Restart-Service -Name "MyApp" }
  "console-status"  { netstat -ano | Select-String ":8790" }
  default           { "未知动作" }
}
```

**白名单是硬编码的 switch，不是"给个命令字符串让我跑"。**

### ③ 为什么要记日志

```
[2026-10-30 01:20:01] 控制台不在，已拉起
[2026-10-30 01:24:01] REQ id=d1a415f6 cmd=console-ensure
[2026-10-30 01:24:01] RES ok=True out=控制台已在跑（8790 LISTEN）
```

**因为"它有没有偷偷用过这个通道"是个必须能回答的问题。**

而且日志还有第二个用途：**排查"为什么没生效"**——没有日志的话，你只能猜。

### ④ 为什么结果要带 id

```json
// 请求
{"id": "d1a415f6", "action": "console-ensure"}

// 响应
{"id": "d1a415f6", "ok": true, "out": "控制台已在跑（8790 LISTEN）"}
```

**因为可能有多条请求排队**（轮询周期内写了两条），要能对上号。

## 组件

| 文件 | 干什么 |
|---|---|
| `client.py` | agent 端：写请求、轮询结果、超时 |
| `executor.ps1` | 执行端：白名单动作 + 日志 |
| `hidden.vbs` | 启动器：让计划任务不弹控制台窗口 |
| `install.ps1` | 安装：注册计划任务（每 2 分钟） |
| `install.cmd` | 用户双击入口（纯 ASCII，主动申请管理员） |

## 装

```powershell
# 以管理员身份
powershell -ExecutionPolicy Bypass -File install.ps1
```

装完检查：

```powershell
schtasks /query /tn "AgentAdminOp" /fo LIST /v
# 应该看到：Task To Run: wscript.exe //B //NoLogo ...hidden.vbs
#          Schedule Type: One Time Only, Minute
#          Repeat: Every 0 hour(s), 2 minute(s)
```

## 用

```bash
python client.py status         # 看通道通不通
python client.py restart        # 请求重启服务
```

客户端会：写请求 → 等最多 150 秒 → 读结果 → 打印。

**150 秒 = 2 分钟轮询 + 余量**。如果只等 90 秒，会刚好错过一个周期（我们踩过这个坑）。

## 两个必须说清楚的边界

### 这不是「给 agent 管理员权限」

它只能做白名单里那几件事。白名单是**你写的**，不是它写的。

### 这个通道可以被滥用（如果你把白名单放宽）

**白名单越宽，这个通道越接近"管理员权限"**。

我们的白名单只有 6 个动作：

```
service-status    看服务在不在
service-restart   重启一个自己管理的服务
console-status    看端口
console-ensure    端口不在就拉起来
llbot-restart     重启消息机器人
service-matrix    一次看所有端口
```

**全都是「让自己活着」相关的。** 没有文件操作、没有网络配置、没有用户管理。

**一条判断标准**：

> **这个动作如果被恶意调用，最坏能造成什么？**
>
> 如果答案是「它自己不能用」，那是安全的；
> 如果是「能删东西 / 能改配置 / 能对外通信」，那不该进白名单。

## 一个平台细节：`-WindowStyle Hidden` 挡不住

你可能想：

```powershell
# 这样写，任务每分钟闪一下黑框
schtasks /create /tn X /tr "powershell -WindowStyle Hidden -File a.ps1"
```

**不行。** `-WindowStyle Hidden` 是 PowerShell 自己的参数，它隐藏的是 PowerShell 窗口，但**任务计划程序在启动进程时已经创建了一个控制台**。

**正确做法**：VBS 包一层。

```vbscript
CreateObject("WScript.Shell").Run "powershell.exe -NoProfile -File ""C:\path\executor.ps1""", 0, False
```

`0` = 隐藏窗口，`False` = 不等待。

任务里写：`wscript.exe //B //NoLogo C:\path\hidden.vbs`

**这条是实打实花了一个小时试出来的**（`-WindowStyle Hidden`、`conhost`、`Start-Process -WindowStyle Hidden` 全试过）。

## 已知限制

1. **延迟最长一个轮询周期**（2 分钟）。不适合需要即时响应的操作。
2. **单实例**：两个请求同时写会互相覆盖——所以要带 id + 客户端串行。
3. **依赖任务计划程序**：任务被禁用/删除就整条链路失效。要加自检（我们加了"每 5 分钟验一次任务在不在"）。
4. **日志会一直涨**：没有轮转。要么定期清，要么加轮转。

---

*从实战里抽出来的，去掉了一个项目的具体动作和路径。*
