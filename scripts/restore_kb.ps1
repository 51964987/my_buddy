# kb/ 回切脚本（从 kb-backup-<时间戳> 备份还原，与 reset_kb_full.ps1 配套）
#
# 用法（PowerShell）：
#   powershell -ExecutionPolicy Bypass -File scripts\restore_kb.ps1                # 还原最新备份（交互确认）
#   powershell -ExecutionPolicy Bypass -File scripts\restore_kb.ps1 -Yes           # 跳过确认
#   powershell -ExecutionPolicy Bypass -File scripts\restore_kb.ps1 -List          # 只列出可用备份
#   powershell -ExecutionPolicy Bypass -File scripts\restore_kb.ps1 -BackupDir kb-backup-20261005-010259 -Yes
#   powershell -ExecutionPolicy Bypass -File scripts\restore_kb.ps1 -NoRestart     # 还原后不重启服务
#
# 流程：读端口 → 选备份（默认最新）→ 校验备份结构 → 停服 → 快照式还原（清现有四区再拷回，
#       index.db 不在备份内，残留属可重建缓存）→ 重启服务 + 冒烟
# 注意：还原是快照覆盖，备份之后新写入的数据会丢失；备份由 reset_kb_full.ps1 / backup_kb.ps1 产生。
param(
    [string]$BackupDir = "",
    [switch]$Yes,
    [switch]$NoRestart,
    [switch]$List,
    [int]$Port = 0
)

$ErrorActionPreference = "Stop"
$repo = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$kb = Join-Path $repo "kb"
$allRegions = @("inbox", "sources", "collections", "wiki")

# ---------- 1. 端口：默认读配置 ----------
if ($Port -le 0) {
    $Port = 8765
    $cfg = Join-Path $repo "kbserver.config.json"
    if (Test-Path $cfg) {
        try {
            $p = (Get-Content $cfg -Raw -Encoding UTF8 | ConvertFrom-Json).port
            if ($p -and [int]$p -gt 0) { $Port = [int]$p }
        } catch { Write-Host "读取 $cfg 失败，使用默认端口 $Port" }
    }
}

# ---------- 2. 选备份（-List 只列出） ----------
$backups = Get-ChildItem $repo -Directory -Filter "kb-backup-*" -ErrorAction SilentlyContinue | Sort-Object Name -Descending
if ($List) {
    if ($backups) {
        Write-Host "可用备份（新 → 旧）："
        $backups | ForEach-Object {
            $n = (Get-ChildItem $_.FullName -Recurse -File | Measure-Object).Count
            Write-Host ("  {0}  ({1} 个文件)" -f $_.Name, $n)
        }
    } else { Write-Host "库同级尚无 kb-backup-* 备份目录" }
    exit 0
}
if ($BackupDir) {
    $src = Join-Path $repo $BackupDir
    if (-not (Test-Path $src)) { Write-Error "指定备份不存在: $src"; exit 1 }
    $src = (Get-Item $src).FullName
} else {
    if (-not $backups) { Write-Error "未找到任何 kb-backup-* 备份目录，无从还原"; exit 1 }
    $src = $backups[0].FullName
    Write-Host "未指定 -BackupDir，自动选最新备份: $($backups[0].Name)"
}

# ---------- 3. 校验备份结构（防选错目录整库清空） ----------
$foundRegions = @($allRegions | Where-Object { Test-Path (Join-Path $src $_) })
if (-not $foundRegions) {
    Write-Error "备份结构异常：$src 下找不到任何分区目录（$($allRegions -join '/')），拒绝还原"
    exit 1
}
$fileCount = (Get-ChildItem $src -Recurse -File | Measure-Object).Count
Write-Host ("备份校验通过: {0}（分区: {1}，{2} 个文件）" -f (Split-Path $src -Leaf), ($foundRegions -join ','), $fileCount)

# ---------- 4. 停服：记录解释器路径供重启使用 ----------
$pyExe = $null
$conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($conn) {
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$($conn.OwningProcess)"
    $pyExe = $proc.ExecutablePath
    Write-Host "停止服务: PID=$($proc.ProcessId) ($pyExe)"
    taskkill /PID $proc.ProcessId /F | Out-Null
    Start-Sleep -Seconds 1
} else {
    Write-Host "端口 $Port 无运行中的服务（跳过停服）"
}

# ---------- 5. 快照式还原：清现有四区 → 拷回备份 ----------
if (-not $Yes) {
    Write-Warning "此操作将用备份 $(Split-Path $src -Leaf) 覆盖 kb/ 分区：$($foundRegions -join ', ')，备份之后新写入的数据会丢失！"
    $answer = Read-Host "输入 YES 确认还原（其他任意输入取消）"
    if ($answer -cne "YES") { Write-Host "aborted"; exit 1 }
}
foreach ($region in $allRegions) {
    $p = Join-Path $kb $region
    if (Test-Path $p) { Remove-Item -Recurse -Force $p }
    $b = Join-Path $src $region
    if (Test-Path $b) { Copy-Item $b $p -Recurse -Force }
}
Write-Host "还原完成: $kb ← $(Split-Path $src -Leaf)"
# ---------- 6. 重启服务（-NoRestart 跳过） ----------
if ($NoRestart) {
    Write-Host '按要求不重启服务。手工启动：start_kb_service.bat'
    exit 0
}
if (-not $pyExe) {
    $pyExe = 'python'
    Write-Warning '服务此前未在运行，重启使用 PATH 上的 python（请确认其已装依赖）'
}
Start-Process -FilePath $pyExe `
    -ArgumentList '-X', 'utf8', '-m', 'kbserver' `
    -WorkingDirectory $repo `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $repo 'kb_service.log') `
    -RedirectStandardError (Join-Path $repo 'kb_service.err.log')
Write-Host '服务已拉起，等待就绪...'

# ---------- 7. 冒烟：轮询 /api/status ----------
$st = $null
for ($i = 0; $i -lt 15; $i++) {
    Start-Sleep -Seconds 1
    try {
        $st = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/status" -TimeoutSec 3
        break
    } catch { }
}
if ($st) {
    Write-Host ("服务已就绪: http://127.0.0.1:{0}/app/" -f $Port)
    Write-Host ("库计数: inbox={0} sources={1} pending={2} enriched={3} collections={4} index={5}" -f `
        $st.inbox.inbox, $st.sources_entries, $st.enrich.pending, $st.enrich.enriched, $st.collections.Count, $st.index.docs)
} else {
    Write-Warning '服务 15 秒内未就绪，请查看 kb_service.err.log'
    exit 1
}
