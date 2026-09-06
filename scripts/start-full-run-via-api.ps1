param(
    [Parameter(Mandatory = $true)]
    [string]$CredentialsBase64,
    [Parameter(Mandatory = $true)]
    [string]$RunBase64
)

$ErrorActionPreference = "Stop"
$baseUrl = "http://127.0.0.1:8765"

function Decode-Json([string]$encoded) {
    return [Text.Encoding]::UTF8.GetString(
        [Convert]::FromBase64String($encoded)
    )
}

$health = Invoke-RestMethod -Uri "$baseUrl/api/health" -TimeoutSec 5
if ($health.status -ne "ok") {
    throw "Collector API is not healthy."
}

$configured = Invoke-RestMethod `
    -Uri "$baseUrl/api/config/credentials" `
    -Method Post `
    -ContentType "application/json" `
    -Body (Decode-Json $CredentialsBase64)

$created = Invoke-RestMethod `
    -Uri "$baseUrl/api/runs" `
    -Method Post `
    -ContentType "application/json" `
    -Body (Decode-Json $RunBase64)

[pscustomobject]@{
    credentials = $configured.status
    run_id = $created.id
} | ConvertTo-Json -Compress
