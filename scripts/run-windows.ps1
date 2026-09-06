$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not (Test-Path .\.venv\Scripts\python.exe)) {
    throw "Окружение не найдено. Сначала запустите scripts\setup-windows.ps1"
}

& .\.venv\Scripts\python.exe -m medik_pilot
