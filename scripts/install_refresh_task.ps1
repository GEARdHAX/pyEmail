<#
.SYNOPSIS
  Installs (or removes) a Windows scheduled task that runs "python run.py refresh" every 15 minutes.

.DESCRIPTION
  The task ingests the latest Gmail replies and bounces into the local tracking database and syncs
  the resulting status changes (BOUNCED, RESPONDED, ...) to the Merge status column of the Sheet.
  It NEVER sends email: "run.py refresh" has no sending code path and ignores the DRY_RUN /
  ENABLE_EMAIL_SENDING switches entirely.

  Overlap protection, two layers:
    1. Task Scheduler:  MultipleInstances = IgnoreNew  (a new run is dropped if the last is still going)
    2. pyEmail itself:   an OS file lock (data\pyemail.lock) shared with "run.py send"

  Nothing is registered unless you pass -Install. Run with no switches to see the plan only.

.PARAMETER Install
  Create or replace the scheduled task for the current user (runs only while you are logged on).
.PARAMETER Uninstall
  Remove the scheduled task.
.PARAMETER Status
  Show the task and its last/next run times.
.PARAMETER IntervalMinutes
  Repeat interval, 5 to 1440 minutes (default 15).
.PARAMETER TaskName
  Name of the scheduled task (default pyEmail-Refresh).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install_refresh_task.ps1            # show the plan
  powershell -ExecutionPolicy Bypass -File scripts\install_refresh_task.ps1 -Install
  powershell -ExecutionPolicy Bypass -File scripts\install_refresh_task.ps1 -Status
  powershell -ExecutionPolicy Bypass -File scripts\install_refresh_task.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [switch]$Install,
    [switch]$Uninstall,
    [switch]$Status,
    [ValidateRange(5, 1440)][int]$IntervalMinutes = 15,
    [string]$TaskName = 'pyEmail-Refresh'
)

$ErrorActionPreference = 'Stop'
$ProjectDir = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $ProjectDir 'run.py'))) { throw "run.py not found in $ProjectDir" }

function Resolve-Python {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd) { throw 'python was not found on PATH' }
    $dir = Split-Path -Parent $cmd.Source
    $windowless = Join-Path $dir 'pythonw.exe'
    # pythonw has no console window, so nothing flashes on screen every 15 minutes; output goes to logs\refresh.log
    if (Test-Path $windowless) { return $windowless }
    return $cmd.Source
}

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'."
    } else {
        Write-Host "No scheduled task named '$TaskName'."
    }
    return
}

if ($Status) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) { Write-Host "No scheduled task named '$TaskName'."; return }
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host "Task      : $TaskName ($($task.State))"
    Write-Host "Last run  : $($info.LastRunTime)   result code: $($info.LastTaskResult)"
    Write-Host "Next run  : $($info.NextRunTime)"
    Write-Host "Log file  : $(Join-Path $ProjectDir 'logs\refresh.log')"
    return
}

$python    = Resolve-Python
$arguments = 'run.py refresh'
$action    = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $ProjectDir
$trigger   = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(2)) `
                 -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
                 -RepetitionDuration (New-TimeSpan -Days 3650)
$settings  = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
                 -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
                 -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Write-Host "Plan"
Write-Host "  Task name        : $TaskName"
Write-Host "  Command          : $python $arguments"
Write-Host "  Working folder   : $ProjectDir"
Write-Host "  Repeats every    : $IntervalMinutes minutes (first run in about 2 minutes)"
Write-Host "  Overlap          : MultipleInstances=IgnoreNew + file lock data\pyemail.lock"
Write-Host "  Time limit       : 10 minutes per run"
Write-Host "  Runs as          : $env:USERDOMAIN\$env:USERNAME, only while logged on, no stored password"
Write-Host "  Log file         : $(Join-Path $ProjectDir 'logs\refresh.log')"
Write-Host "  Sends email?     : never (refresh has no sending code path)"

if (-not $Install) {
    Write-Host ""
    Write-Host "Nothing was registered. Add -Install to create the task."
    return
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description 'pyEmail: ingest Gmail replies/bounces and sync Merge status. Never sends email.' `
    -Force | Out-Null
Write-Host ""
Write-Host "Installed scheduled task '$TaskName'. Check it with:  scripts\install_refresh_task.ps1 -Status"
