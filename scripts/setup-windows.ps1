$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw "Python Launcher не найден. Установите Python 3.12 с python.org."
}

py -3.12 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
& .\.venv\Scripts\python.exe -m playwright install chromium

if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
}

Write-Host "Setup complete. Fill in .env and run scripts\run-windows.ps1" -ForegroundColor Green
