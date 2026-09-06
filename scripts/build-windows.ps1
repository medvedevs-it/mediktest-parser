param(
    [switch]$PackageOnly
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not (Test-Path .\.venv\Scripts\python.exe)) {
    throw "Сначала запустите scripts\setup-windows.ps1"
}

if (-not $PackageOnly) {
    & .\.venv\Scripts\python.exe -m pip install pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller installation failed." }
    $env:PLAYWRIGHT_BROWSERS_PATH = "0"
    & .\.venv\Scripts\python.exe -m playwright install chromium
    if ($LASTEXITCODE -ne 0) { throw "Chromium installation failed." }

    if (Test-Path .\build) { Remove-Item .\build -Recurse -Force }
    if (Test-Path .\dist) { Remove-Item .\dist -Recurse -Force }
    if (Test-Path .\MedikTest-Collector.spec) { Remove-Item .\MedikTest-Collector.spec -Force }

    & .\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onedir `
      --name MedikTest-Collector `
      --add-data "medik_pilot\web;medik_pilot\web" `
      --collect-all playwright `
      medik_pilot\__main__.py
    if ($LASTEXITCODE -ne 0) { throw "EXE compilation failed." }
} elseif (-not (Test-Path .\dist\MedikTest-Collector\MedikTest-Collector.exe)) {
    throw "PackageOnly requires an existing compiled EXE in dist."
}

if (Test-Path .\release) { Remove-Item .\release -Recurse -Force }
if (Test-Path .\r) { Remove-Item .\r -Recurse -Force }

# A short staging path keeps bundled Chromium files below the legacy Windows
# MAX_PATH limit while preserving the friendly folder names inside the ZIP.
$stagingRoot = ".\r"
$releaseRoot = ".\release"
New-Item -ItemType Directory -Path $stagingRoot -Force | Out-Null
New-Item -ItemType Directory -Path $releaseRoot -Force | Out-Null
Copy-Item ".\dist\MedikTest-Collector" "$stagingRoot\MedikTest-Collector" -Recurse
Copy-Item ".\README_CLIENT.md" "$stagingRoot\CLIENT_INSTRUCTIONS.md"
Set-Content -Path "$stagingRoot\VERSION.txt" -Value "MedikTest Collector 1.4.1" -Encoding UTF8

$archive = "$releaseRoot\MedikTest-Collector-Windows.zip"
Compress-Archive -Path "$stagingRoot\*" -DestinationPath $archive -CompressionLevel Optimal
& .\.venv\Scripts\python.exe .\scripts\verify-client-package.py $archive
if ($LASTEXITCODE -ne 0) { throw "Client archive validation failed." }

$archiveHash = (Get-FileHash $archive -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -Path ".\release\MedikTest-Collector-Windows.zip.sha256" `
  -Value "$archiveHash  MedikTest-Collector-Windows.zip" -Encoding ASCII

Write-Host "Build complete: $stagingRoot\MedikTest-Collector\MedikTest-Collector.exe" -ForegroundColor Green
Write-Host "Client archive: $archive" -ForegroundColor Green
Write-Host "SHA-256: $archiveHash" -ForegroundColor Green
