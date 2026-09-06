$ErrorActionPreference = "Stop"

$dataDir = "C:\MedikTestData"
$appDir = "C:\Users\Administrator\MedikTest-Collector-Full"
$exePath = Join-Path $appDir ".venv\Scripts\python.exe"

New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
if (-not (Test-Path $exePath -PathType Leaf)) {
    throw "MedikTest Collector Python runtime not found: $exePath"
}

$env:MEDIKTEST_DATA_DIR = $dataDir
$env:MEDIKTEST_NO_BROWSER = "1"
$env:MEDIKTEST_PUBLIC_BASE_URL = "https://testbd.easystation.ru"
$env:PLAYWRIGHT_BROWSERS_PATH = "0"
Set-Location $appDir

$process = Start-Process `
    -FilePath $exePath `
    -ArgumentList "-m medik_pilot" `
    -WorkingDirectory $appDir `
    -RedirectStandardOutput (Join-Path $dataDir "collector.stdout.log") `
    -RedirectStandardError (Join-Path $dataDir "collector.stderr.log") `
    -Wait `
    -PassThru

Add-Content `
    -Path (Join-Path $dataDir "collector.exit.log") `
    -Value ("{0:o} exit_code={1}" -f (Get-Date), $process.ExitCode)
exit $process.ExitCode
