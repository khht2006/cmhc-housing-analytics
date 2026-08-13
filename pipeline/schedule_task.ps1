<#
.SYNOPSIS
    Register the monthly refresh with Windows Task Scheduler.

.DESCRIPTION
    Schedules pipeline/refresh.py to run monthly.

    Timing: CMHC publishes housing starts around the 8th-10th business day of
    the month, and the CBA arrears PDF lands later still. Running on the 15th
    gives both a comfortable margin; the extractor walks backwards to the newest
    published arrears PDF anyway, so an early run degrades to "no new data"
    rather than failing.

    Exit codes from refresh.py, which Task Scheduler surfaces as "Last Run Result":
        0  success
        1  unhandled error
        2  reconciliation breach - export deliberately skipped

.EXAMPLE
    .\schedule_task.ps1
    .\schedule_task.ps1 -DayOfMonth 20 -TimeOfDay "06:00"
    .\schedule_task.ps1 -Unregister
#>

[CmdletBinding()]
param(
    [int]    $DayOfMonth = 15,
    [string] $TimeOfDay  = "07:00",
    [string] $TaskName   = "CMHC Housing Analytics - Monthly Refresh",
    [switch] $Unregister
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PythonExe   = (Get-Command python).Source

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Unregistered '$TaskName'."
    return
}

if (-not (Test-Path (Join-Path $ProjectRoot "pipeline\refresh.py"))) {
    throw "refresh.py not found under $ProjectRoot - run this from the repo."
}

# -WorkingDirectory matters: refresh.py resolves config and data paths relative
# to the project root, and Task Scheduler otherwise starts in system32.
$Action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "-m pipeline.refresh --triggered-by scheduler" `
    -WorkingDirectory $ProjectRoot

$Trigger = New-ScheduledTaskTrigger -Monthly -DaysOfMonth $DayOfMonth -At $TimeOfDay

# StartWhenAvailable covers the laptop-was-asleep case: a missed monthly run
# fires as soon as the machine is back, instead of silently skipping a month.
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 30)

Register-ScheduledTask `
    -TaskName    $TaskName `
    -Action      $Action `
    -Trigger     $Trigger `
    -Settings    $Settings `
    -Description "Extracts CMHC/StatCan/CBA sources, rebuilds the star schema, reconciles against published control totals, and exports Parquet for Power BI. Export is skipped on a reconciliation breach." `
    -Force | Out-Null

Write-Host "Registered '$TaskName'"
Write-Host "  runs      : day $DayOfMonth of each month at $TimeOfDay"
Write-Host "  command   : $PythonExe -m pipeline.refresh"
Write-Host "  workdir   : $ProjectRoot"
Write-Host ""
Write-Host "Run it now with:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Check result with: (Get-ScheduledTaskInfo -TaskName '$TaskName').LastTaskResult"
