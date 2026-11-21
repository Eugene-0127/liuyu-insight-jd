$ErrorActionPreference = 'Stop'
$appRoot = $PSScriptRoot
$serviceUrl = 'http://127.0.0.1:8080'
$alreadyRunning = $false
try {
    $health = Invoke-RestMethod "$serviceUrl/api/health" -TimeoutSec 2
    if ($health.app -eq 'jd-durian-local') { $alreadyRunning = $true }
    else { throw 'Port 8080 is used by another application.' }
} catch {
    if ($_.Exception.Message -like '*another application*') { throw }
}
if (-not $alreadyRunning) {
    $runtime = Join-Path $appRoot '.venv\Scripts\pythonw.exe'
    if (-not (Test-Path -LiteralPath $runtime)) { throw 'Missing .venv runtime.' }
    $serverFile = Join-Path $appRoot 'server.py'
    $dataFolder = Join-Path $appRoot 'data'
    New-Item -ItemType Directory -Path $dataFolder -Force | Out-Null
    $service = Start-Process -FilePath $runtime -ArgumentList @(('"' + $serverFile + '"')) -WorkingDirectory $appRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $dataFolder 'stdout.log') -RedirectStandardError (Join-Path $dataFolder 'stderr.log')
    Set-Content -LiteralPath (Join-Path $dataFolder 'service.pid') -Value $service.Id -Encoding ascii
    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Milliseconds 500
        try {
            $health = Invoke-RestMethod "$serviceUrl/api/health" -TimeoutSec 1
            if ($health.app -eq 'jd-durian-local') { $ready = $true; break }
        } catch {}
        if ($service.HasExited) { throw 'Service failed. Read data\stderr.log.' }
    }
    if (-not $ready) { throw 'Service startup timed out. Read data\stderr.log.' }
}
$edgeExecutable = Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe'
if (-not (Test-Path -LiteralPath $edgeExecutable)) {
    $edgeExecutable = Join-Path $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe'
}
if (-not (Test-Path -LiteralPath $edgeExecutable)) { throw 'Microsoft Edge was not found.' }
Start-Process -FilePath $edgeExecutable -ArgumentList @('--new-window', $serviceUrl)
