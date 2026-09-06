$ErrorActionPreference = "Stop"

$taskName = "MedikTest Full Collector"
Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
Get-Process "MedikTest-Collector" -ErrorAction SilentlyContinue |
    Stop-Process -Force
$listeners = Get-NetTCPConnection `
    -LocalPort 8765 `
    -State Listen `
    -ErrorAction SilentlyContinue
foreach ($listenerPid in @($listeners.OwningProcess | Select-Object -Unique)) {
    if (-not $listenerPid) {
        continue
    }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$listenerPid"
    if ($process.CommandLine -notlike "*medik_pilot*") {
        throw "Port 8765 belongs to an unrelated process: $listenerPid"
    }
    Stop-Process -Id $listenerPid -Force
}
Start-Sleep -Seconds 1
Start-ScheduledTask -TaskName $taskName
Write-Output "RESTARTED"
