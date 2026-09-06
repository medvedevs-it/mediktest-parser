param(
    [Parameter(Mandatory = $true)]
    [string]$ExePath,
    [string]$DataDir = ""
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $ExePath -PathType Leaf)) {
    throw "EXE not found: $ExePath"
}

if ([string]::IsNullOrWhiteSpace($DataDir)) {
    $DataDir = Join-Path $env:TEMP ("MedikTest-Smoke-" + [guid]::NewGuid().ToString("N"))
}
New-Item -ItemType Directory -Path $DataDir -Force | Out-Null

$env:MEDIKTEST_DATA_DIR = $DataDir
$env:MEDIKTEST_NO_BROWSER = "1"
$baseUrl = "http://127.0.0.1:8765"
$process = Start-Process -FilePath $ExePath -PassThru

try {
    $healthy = $false
    for ($attempt = 1; $attempt -le 90; $attempt++) {
        if ($process.HasExited) {
            throw "Application exited before health check. Exit code: $($process.ExitCode)"
        }
        try {
            $health = Invoke-RestMethod -Uri "$baseUrl/api/health" -TimeoutSec 2
            if ($health.status -eq "ok") {
                $healthy = $true
                break
            }
        } catch {
            Start-Sleep -Seconds 1
        }
    }
    if (-not $healthy) {
        throw "Application did not become healthy in 90 seconds."
    }

    $payload = @{
        source_mode = "demo"
        material_type = "both"
        reference_tests = 10
        reference_cases = 3
        verification_percent = 15
        max_attempts = 5
        max_requests = 100
        max_duration_minutes = 10
        delay_seconds = 0
        allow_create_attempts = $false
        allow_answer_submission = $false
    } | ConvertTo-Json

    $created = Invoke-RestMethod `
        -Uri "$baseUrl/api/runs" `
        -Method Post `
        -ContentType "application/json" `
        -Body $payload

    $run = $null
    for ($poll = 1; $poll -le 60; $poll++) {
        $run = Invoke-RestMethod -Uri "$baseUrl/api/runs/$($created.id)" -TimeoutSec 5
        if ($run.status -in @("completed", "failed", "stopped")) {
            break
        }
        Start-Sleep -Milliseconds 500
    }

    if ($null -eq $run -or $run.status -ne "completed") {
        $status = if ($null -eq $run) { "unknown" } else { $run.status }
        throw "Demo run did not complete successfully. Status: $status"
    }

    $jsonPath = Join-Path $DataDir "smoke-export.json"
    $xlsxPath = Join-Path $DataDir "smoke-export.xlsx"
    $imagesPath = Join-Path $DataDir "smoke-images.zip"
    Invoke-WebRequest -Uri "$baseUrl/api/runs/$($created.id)/export.json" -OutFile $jsonPath
    Invoke-WebRequest -Uri "$baseUrl/api/runs/$($created.id)/export.xlsx" -OutFile $xlsxPath
    Invoke-WebRequest -Uri "$baseUrl/api/runs/$($created.id)/export.images.zip" -OutFile $imagesPath

    $document = Get-Content $jsonPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $testsCount = @($document.tests).Count
    $casesCount = @($document.cases).Count
    if ($testsCount -ne 10 -or $casesCount -ne 3) {
        throw "Unexpected export counts: tests=$testsCount cases=$casesCount"
    }
    if ((Get-Item $xlsxPath).Length -le 0) {
        throw "Unified Excel export is empty."
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $imagesArchive = [System.IO.Compression.ZipFile]::OpenRead($imagesPath)
    try {
        $manifestEntry = $imagesArchive.GetEntry("image_manifest.json")
        if ($null -eq $manifestEntry) {
            throw "Image ZIP does not contain image_manifest.json."
        }
    } finally {
        $imagesArchive.Dispose()
    }

    [pscustomobject]@{
        status = "passed"
        run_id = $created.id
        tests = $testsCount
        situational_tasks = $casesCount
        stop_reason = $run.stop_reason
        json_bytes = (Get-Item $jsonPath).Length
        xlsx_bytes = (Get-Item $xlsxPath).Length
        images_zip_bytes = (Get-Item $imagesPath).Length
        data_dir = $DataDir
    } | ConvertTo-Json -Compress
} finally {
    if ($null -ne $process -and -not $process.HasExited) {
        Stop-Process -Id $process.Id -Force
        $process.WaitForExit()
    }
}
