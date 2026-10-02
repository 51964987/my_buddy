# kb/ 重置脚本（危险操作，方案文档 §4：kb/ 为唯一事实源，重置即清空全部沉淀）
#
# 用法（PowerShell）：
#   .\scripts\reset_kb.ps1                       # 交互确认后重置
#   .\scripts\reset_kb.ps1 -Yes                  # 跳过确认（自动化场景）
#   .\scripts\reset_kb.ps1 -BackupFirst          # 删除前自动备份到同级 kb-backup-<时间戳>
#   .\scripts\reset_kb.ps1 -BackupFirst -Yes     # 组合使用
#
# 说明：
#   - 重置 = 删除 kb/ 整目录并重建五区空目录（inbox/sources/collections/wiki；index.db 属 P4）
#   - 服务运行中可直接执行：所有写盘均为 mkdir+原子写，无需停服
#   - config（kbserver.config.json）不属于 kb/，不受影响
param(
    [string]$KbRoot = (Join-Path $PSScriptRoot "..\kb"),
    [switch]$Yes,
    [switch]$BackupFirst
)

$ErrorActionPreference = "Stop"
$kb = [System.IO.Path]::GetFullPath($KbRoot)

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
    Write-Warning "此操作将删除整个知识库目录：$kb"
    $answer = Read-Host "输入 YES 确认删除（其他任意输入取消）"
    if ($answer -cne "YES") {
        Write-Host "aborted"
        exit 1
    }
}

Remove-Item -Recurse -Force $kb
foreach ($region in @("inbox", "sources", "collections", "wiki")) {
    New-Item -ItemType Directory -Path (Join-Path $kb $region) -Force | Out-Null
}
Write-Host "kb reset done: $kb"
