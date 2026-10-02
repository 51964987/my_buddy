# kb/ 文件级备份脚本（方案文档 §6：kb/ 为唯一事实源，纳入文件级备份；index.db 可重建不备份）
#
# 用法（PowerShell）：
#   .\scripts\backup_kb.ps1 -Dest D:\backups\kb              # 增量备份（不删除目标端多余文件）
#   .\scripts\backup_kb.ps1 -Dest D:\backups\kb -Mirror      # 镜像模式（目标端与源完全一致，慎用：源端删除会同步删除）
#
# 定时任务注册示例（每天 03:00，管理员 PowerShell）：
#   $action = New-ScheduledTaskAction -Execute "powershell.exe" `
#     -Argument "-NoProfile -ExecutionPolicy Bypass -File d:\biancheng\otherProject\my_buddy\scripts\backup_kb.ps1 -Dest D:\backups\kb"
#   $trigger = New-ScheduledTaskTrigger -Daily -At 03:00
#   Register-ScheduledTask -TaskName "kb-backup" -Action $action -Trigger $trigger
param(
    [string]$Source = (Join-Path $PSScriptRoot "..\kb"),
    [Parameter(Mandatory = $true)][string]$Dest,
    [switch]$Mirror
)

$ErrorActionPreference = "Stop"
$Source = (Resolve-Path $Source).Path
if (-not (Test-Path $Source)) {
    Write-Error "kb source not found: $Source"
    exit 1
}

$args = @($Source, $Dest, "/E", "/R:2", "/W:5", "/NFL", "/NDL", "/NP", "/XD", "index.db")
if ($Mirror) { $args += "/MIR" }

Write-Host "robocopy $($args -join ' ')"
& robocopy @args
$code = $LASTEXITCODE

# robocopy 退出码 0-7 均为成功（8+ 为失败）
if ($code -ge 8) {
    Write-Error "robocopy failed with exit code $code"
    exit $code
}
Write-Host "backup done: $Source -> $Dest (robocopy code $code)"
exit 0
