# Start the converter on http://127.0.0.1:8000
# Usage:  .\run.ps1  [-Port 8000]
param([int]$Port = 8000)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "Creating virtual environment..."
    py -m venv .venv
    & $python -m pip install --quiet --upgrade pip
    & $python -m pip install --quiet -r requirements.txt
}

Write-Host "Converter starting on http://127.0.0.1:$Port"
& $python -m uvicorn main:app --host 127.0.0.1 --port $Port
