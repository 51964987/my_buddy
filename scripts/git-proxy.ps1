# git-proxy.ps1 — 自动探测本机代理并执行 git push/fetch/pull
# 用法:
#   .\scripts\git-proxy.ps1 push
#   .\scripts\git-proxy.ps1 fetch
#   .\scripts\git-proxy.ps1 pull
#   .\scripts\git-proxy.ps1 push -Port 7890        # 手动指定代理端口
#   .\scripts\git-proxy.ps1 push -Direct           # 强制直连（不走代理）

param(
    [Parameter(Position = 0)]
    [ValidateSet('push', 'fetch', 'pull', 'status')]
    [string]$Action = 'push',

    [int]$Port = 0,          # 0 = 自动探测
    [switch]$Direct          # 跳过代理直连
)

$ErrorActionPreference = 'Stop'
$ProxyHost = '127.0.0.1'

# 常见代理软件默认端口，按命中概率排序
$CommonPorts = @(7897, 7890, 10809, 10808, 8888, 8118, 1080)

function Test-Port {
    param([string]$Target, [int]$TargetPort)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $task = $client.ConnectAsync($Target, $TargetPort)
        if (-not $task.Wait(800)) { return $false }
        return $client.Connected
    } catch { return $false }
    finally { $client.Dispose() }
}

function Test-GitHub {
    # 检测能否直连 github.com:443（超时 3 秒）
    return (Test-Port 'github.com' 443)
}

# ---- 主流程 ----
$gitArgs = @()
$useProxy = $false

if (-not $Direct -and -not (Test-GitHub)) {
    Write-Host '[i] 直连 github.com 失败，尝试探测本机代理...' -ForegroundColor Yellow

    if ($Port -gt 0) {
        if (Test-Port $ProxyHost $Port) {
            $useProxy = $true
        } else {
            Write-Error "指定端口 $ProxyHost`:$Port 未监听。请确认代理软件已启动。"
            exit 1
        }
    } else {
        foreach ($p in $CommonPorts) {
            if (Test-Port $ProxyHost $p) {
                $Port = $p
                $useProxy = $true
                break
            }
        }
    }

    if ($useProxy) {
        Write-Host "[+] 使用代理 $ProxyHost`:$Port" -ForegroundColor Green
        $gitArgs += @('-c', "http.proxy=http://$ProxyHost`:$Port",
                      '-c', "https.proxy=http://$ProxyHost`:$Port")
    } else {
        Write-Warning "未发现可用代理端口（已扫描: $($CommonPorts -join ', ')），将尝试直连。"
    }
}
elseif ($Direct) {
    Write-Host '[i] 已指定直连模式。'
}
else {
    Write-Host '[+] 直连 github.com 正常，无需代理。'
}

if ($Action -eq 'status') {
    git status --short --branch
    git log --oneline -3
    exit $LASTEXITCODE
}

# push 时若尚无上游，自动补 -u
if ($Action -eq 'push') {
    $branch = git rev-parse --abbrev-ref HEAD
    $upstream = git rev-parse --abbrev-ref --symbolic-full-name "@{u}" 2>$null
    if (-not $upstream) {
        $gitArgs += @('push', '--set-upstream', 'origin', $branch)
        Write-Host "[i] 分支 $branch 无上游，自动执行 push --set-upstream origin $branch" -ForegroundColor Cyan
    } else {
        $gitArgs += @('push')
    }
} else {
    $gitArgs += @($Action)
}

git @gitArgs
exit $LASTEXITCODE
