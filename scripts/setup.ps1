# One-time setup: create a local venv and install runtime dependencies.
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt

Write-Host ""
Write-Host "Setup complete. venv at $root\.venv"
Write-Host "Next: copy .env.example to .env and fill in your keys, then run:"
Write-Host "  powershell -ExecutionPolicy Bypass -File scripts\run_bot.ps1"
