$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$pythonCommand = if (Get-Command py -ErrorAction SilentlyContinue) { 'py' } elseif (Get-Command python -ErrorAction SilentlyContinue) { 'python' } else { throw 'Please install Python 3.10 or newer first.' }
$prefixArgs = if ($pythonCommand -eq 'py') { @('-3') } else { @() }
& $pythonCommand @prefixArgs -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"
if ($LASTEXITCODE -ne 0) { throw 'Python 3.10 or newer is required.' }
& $pythonCommand @prefixArgs -m venv .venv
if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python environment.' }
$runtime = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
& $runtime -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check your network and retry.' }
Write-Output 'Ready. Run the start CMD file to open the dashboard.'
