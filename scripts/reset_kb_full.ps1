# kb/ 全量重置一键脚本（备份 + 停服 + 重置 + 重启 + 冒烟）
#
# 用法（PowerShell）：
#   powershell -ExecutionPolicy Bypass -File scripts\reset_kb_full.ps1                # 交互确认
#   powershell -ExecutionPolicy Bypass -File scripts\reset_kb_full.ps1 -Yes           # 跳过确认
#   powershell -ExecutionPolicy Bypass -File scripts\reset_kb_full.ps1 -Yes -SkipBackup  # 不备份（慎用）
#   powershell -ExecutionPolicy Bypass -File scripts\reset_kb_full.ps1 -NoRestart     # 只重置不重启服务
#   powershell -ExecutionPolicy Bypass -File scripts\reset_kb_full.ps1 -Port 8765     # 手动指定端口（默认读配置）
#
# 流程：
#   1. 读取 kbserver.config.json 的端口（默认 8765）
#   2. 检测运行中的服务并停止（记录其解释器路径，重启时用同一个，避免 PATH python 依赖不一致）
#   3. 备份 kb/ 到同级 kb-backup-<时间戳>（-SkipBackup 跳过；备份失败则中止重置）
#   4. 复用 reset_kb.ps1 全量重置（四区 + index.db 一并清空，重建空目录）
#   5. 重启服务（隐藏窗口，日志落 kb_service.log / kb_service.err.log）
#   6. 冒烟：轮询 /api/status 最多 15 秒，输出库计数
#
# 注意：
#   - 必须停服的原因：全量重置要删 index.db，服务进程持有其 SQLite 连接，文件删不掉
#   - config（kbserver.config.json）不在 kb/ 内，不受影响
#   - 服务本来没在跑时，重启会用 PATH 上的 python（请自行确认该解释器已装依赖）
param(
    [switch]$Yes,
    [switch]$SkipBackup,
    [switch]$NoRestart,
    [int]$Port = 0
)

$ErrorActionPreference = "Stop"
$repo = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))

# ---------- 1. 端口：默认读配置 ----------
if ($Port -le 0) {
    $Port = 8765
    $cfg = Join-Path $repo "kbserver.config.json"
    if (Test-Path $cfg) {
        try {
            $p = (Get-Content $cfg -Raw -Encoding UTF8 | ConvertFrom-Json).port
            if ($p -and [int]$p -gt 0) { $Port = [int]$p }
        } catch {
            Write-Host "读取 $cfg 失败，使用默认端口 $Port"
        }
    }
}
Write-Host "服务端口: $Port"

# ---------- 2. 停服：记录解释器路径供重启使用 ----------
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

# ---------- 3+4. 备份 + 重置（复用 reset_kb.ps1；显式分支调用，数组展开会被误绑到 -Regions） ----------
if ($SkipBackup) {
    & (Join-Path $PSScriptRoot "reset_kb.ps1") -Yes
} else {
    & (Join-Path $PSScriptRoot "reset_kb.ps1") -Yes -BackupFirst
}
if ($LASTEXITCODE -ne 0) {
    Write-Error "reset_kb.ps1 失败 (exit $LASTEXITCODE)，已中止"
    exit 1
}

# ---------- 5. 重启服务（-NoRestart 跳过） ----------
if ($NoRestart) {
    Write-Host "按要求不重启服务。手工启动：start_kb_service.bat"
    exit 0
}
if (-not $pyExe) {
    $pyExe = "python"
    Write-Warning "服务此前未在运行，重启使用 PATH 上的 python（请确认其已装依赖）"
}
Start-Process -FilePath $pyExe `
    -ArgumentList "-X", "utf8", "-m", "kbserver" `
    -WorkingDirectory $repo `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $repo "kb_service.log") `
    -RedirectStandardError (Join-Path $repo "kb_service.err.log")
Write-Host "服务已拉起（$pyExe），等待就绪..."

# ---------- 6. 冒烟：轮询 /api/status ----------
$st = $null
for ($i = 0; $i -lt 15; $i++) {
    Start-Sleep -Seconds 1
    try {
        $st = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/status" -TimeoutSec 3
        break
    } catch {
        # 服务尚未就绪，继续等
    }
}
if ($st) {
    Write-Host ("服务已就绪: http://127.0.0.1:{0}/app/" -f $Port)
    Write-Host ("库计数: inbox={0} sources={1} pending={2} enriched={3} collections={4} index={5}" -f `
        $st.inbox.inbox, $st.sources_entries, $st.enrich.pending, $st.enrich.enriched, $st.collections.Count, $st.index.docs)
} else {
    Write-Warning "服务 15 秒内未就绪，请查看 kb_service.err.log"
    exit 1
}
