# kb/ 重置脚本（危险操作，方案文档 §4：kb/ 为唯一事实源，重置即清空沉淀）
#
# 用法（PowerShell）：
#   .\scripts\reset_kb.ps1                                  # 交互确认后重置全部四区
#   .\scripts\reset_kb.ps1 -Yes                             # 跳过确认（自动化场景）
#   .\scripts\reset_kb.ps1 -BackupFirst                     # 删除前自动备份到同级 kb-backup-<时间戳>
#   .\scripts\reset_kb.ps1 -BackupFirst -Yes                # 组合使用
#   .\scripts\reset_kb.ps1 -Regions wiki                    # 只重置 wiki 区
#   .\scripts\reset_kb.ps1 -Regions wiki,collections -Yes   # 只重置指定区（逗号分隔）
#
# 说明：
#   - 全量重置（默认）= 删除 kb/ 整目录并重建四区空目录（含 index.db 一并清空）
#   - 部分重置（-Regions 子集）= 只删指定区并重建空目录；index.db 不清，
#     其中可能残留已删条目的索引，属可重建缓存，如需干净可在设置页触发索引重建
#   - 服务运行中可直接执行：所有写盘均为 mkdir+原子写，无需停服
#   - config（kbserver.config.json）不属于 kb/，不受影响
param(
    [string]$KbRoot = (Join-Path $PSScriptRoot "..\kb"),
    [string[]]$Regions = @("inbox", "sources", "collections", "wiki"),
    [switch]$Yes,
    [switch]$BackupFirst
)

$ErrorActionPreference = "Stop"
$kb = [System.IO.Path]::GetFullPath($KbRoot)

$allRegions = @("inbox", "sources", "collections", "wiki")
$Regions = @($Regions | ForEach-Object { $_.Trim().ToLowerInvariant() } | Where-Object { $_ })
$invalid = $Regions | Where-Object { $_ -notin $allRegions }
if ($invalid) {
    Write-Error "unknown region(s): $($invalid -join ', '). valid: $($allRegions -join '/')"
    exit 1
}
if (-not $Regions) {
    Write-Error "-Regions is empty. valid: $($allRegions -join '/')"
    exit 1
}
$fullReset = ($allRegions | Where-Object { $_ -notin $Regions }).Count -eq 0

if (-not (Test-Path $kb)) {
    Write-Host "kb dir not found: $kb (nothing to reset)"
    exit 0
}

if ($BackupFirst) {
    $dest = "$kb-backup-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
    & (Join-Path $PSScriptRoot "backup_kb.ps1") -Source $kb -Dest $dest
    if ($LASTEXITCODE -ge 8) {
        Write-Error "backup failed (robocopy code $LASTEXITCODE), reset aborted"
        exit 1
    }
    Write-Host "backup saved to: $dest"
}

if (-not $Yes) {
    if ($fullReset) {
        Write-Warning "此操作将删除整个知识库目录：$kb"
    } else {
        Write-Warning "此操作将删除 kb/ 以下分区：$($Regions -join ', ')"
    }
    $answer = Read-Host "输入 YES 确认删除（其他任意输入取消）"
    if ($answer -cne "YES") {
        Write-Host "aborted"
        exit 1
    }
}

if ($fullReset) {
    Remove-Item -Recurse -Force $kb
} else {
    foreach ($region in $Regions) {
        $path = Join-Path $kb $region
        if (Test-Path $path) { Remove-Item -Recurse -Force $path }
    }
}
foreach ($region in $Regions) {
    New-Item -ItemType Directory -Path (Join-Path $kb $region) -Force | Out-Null
}
if ($fullReset) {
    Write-Host "kb reset done: $kb"
} else {
    Write-Host "kb partial reset done ($($Regions -join ', ')): $kb"
}
