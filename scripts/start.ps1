$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
New-Item -ItemType Directory -Force -Path (Join-Path $projectRoot 'data') | Out-Null
$backendExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $backendExe)) { throw 'Please run uv sync first.' }
$nodeExe = (Get-Command node.exe).Source
$started = @{}
$backendPort = Get-NetTCPConnection -State Listen -LocalPort 8000 -ErrorAction SilentlyContinue
if (-not $backendPort) {
    $p = Start-Process -FilePath $backendExe -ArgumentList @('-m','uvicorn','backend.main:app','--host','127.0.0.1','--port','8000') -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $projectRoot 'data\backend.log') -RedirectStandardError (Join-Path $projectRoot 'data\backend-error.log')
    $started.backend = $p.Id
}
$frontendPort = Get-NetTCPConnection -State Listen -LocalPort 5173 -ErrorAction SilentlyContinue
if (-not $frontendPort) {
    $p = Start-Process -FilePath $nodeExe -ArgumentList @('node_modules/vite/bin/vite.js','--host','127.0.0.1','--port','5173','--strictPort') -WorkingDirectory (Join-Path $projectRoot 'frontend') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $projectRoot 'data\frontend.log') -RedirectStandardError (Join-Path $projectRoot 'data\frontend-error.log')
    $started.frontend = $p.Id
}
if ($started.Count -gt 0) { $started | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $projectRoot 'data\service-pids.json') }
Write-Output 'Frontend: http://127.0.0.1:5173'
Write-Output 'Backend API: http://127.0.0.1:8000/docs'
