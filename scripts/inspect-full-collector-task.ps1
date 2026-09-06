$ErrorActionPreference = "Continue"

$taskName = "MedikTest Full Collector"
$task = Get-ScheduledTask -TaskName $taskName
$info = Get-ScheduledTaskInfo -TaskName $taskName

[pscustomobject]@{
    TaskName = $task.TaskName
    State = $task.State
    Execute = $task.Actions.Execute
    Arguments = $task.Actions.Arguments
    LastRunTime = $info.LastRunTime
    LastTaskResult = $info.LastTaskResult
} | Format-List

Get-Process "MedikTest-Collector" -ErrorAction SilentlyContinue |
    Select-Object Id, StartTime, Path
Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue |
    Select-Object LocalAddress, LocalPort, OwningProcess

if (Test-Path "C:\MedikTestData\collector.log") {
    Get-Content "C:\MedikTestData\collector.log" -Tail 100
}
