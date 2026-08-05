# One autonomous cycle. This is what Windows Task Scheduler invokes - no Claude needed.
# Reads .env, runs a single scan/review/execute pass, then exits.
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }   # fall back to system python

if (-not (Test-Path (Join-Path $root "data"))) { New-Item -ItemType Directory -Path (Join-Path $root "data") | Out-Null }
$schedLog = Join-Path $root "data\scheduler.log"

"$(Get-Date -Format o)  starting cycle" | Out-File -Append -Encoding utf8 $schedLog
& $py -m predictionedge.run *>> $schedLog
"$(Get-Date -Format o)  cycle exit code $LASTEXITCODE" | Out-File -Append -Encoding utf8 $schedLog
