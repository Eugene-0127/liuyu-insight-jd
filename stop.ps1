$ErrorActionPreference = 'Stop'
$serviceUrl = 'http://127.0.0.1:8080'
try {
    $state = Invoke-RestMethod "$serviceUrl/api/state" -TimeoutSec 3
    Invoke-RestMethod "$serviceUrl/api/shutdown" -Method Post -Headers @{'X-Local-Token'=$state.token} -ContentType 'application/json' -Body '{}' -TimeoutSec 5 | Out-Null
    Write-Output 'Shutdown requested. Received records are saved before the browser closes.'
} catch {
    Write-Output 'Service is already stopped, or not reachable. No process was forcibly terminated.'
}
