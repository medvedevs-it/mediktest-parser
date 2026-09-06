param(
    [Parameter(Mandatory = $true)]
    [string]$RunId
)

$ErrorActionPreference = "Stop"
$baseUrl = "http://127.0.0.1:8765"

$health = Invoke-RestMethod -Uri "$baseUrl/api/health" -TimeoutSec 10
if ($health.status -ne "ok") {
    throw "Collector API is not healthy."
}

$username = Read-Host "MedikTest login"
$securePassword = Read-Host "MedikTest password" -AsSecureString
$passwordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)

try {
    $plainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($passwordPointer)
    $credentialsBody = @{
        username = $username
        password = $plainPassword
    } | ConvertTo-Json -Compress

    $configured = Invoke-RestMethod `
        -Uri "$baseUrl/api/config/credentials" `
        -Method Post `
        -ContentType "application/json" `
        -Body $credentialsBody
} finally {
    $plainPassword = $null
    $credentialsBody = $null
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($passwordPointer)
}

$resumed = Invoke-RestMethod `
    -Uri "$baseUrl/api/runs/$RunId/resume" `
    -Method Post `
    -ContentType "application/json"

Start-Sleep -Seconds 3
$run = Invoke-RestMethod -Uri "$baseUrl/api/runs/$RunId" -TimeoutSec 10

[pscustomobject]@{
    credentials = $configured.status
    resume_status = $resumed.status
    run_id = $run.id
    run_status = $run.status
    attempts_completed = $run.attempts_completed
    tests = $run.test_count
    cases = $run.case_count
} | ConvertTo-Json -Compress
