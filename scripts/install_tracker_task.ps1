<#
.SYNOPSIS
  Installs (or removes) a Windows scheduled task that starts the pyEmail tracking server when you log on.

.DESCRIPTION
  The tracking server ("python run.py track serve") answers the open pixel and click links that tracked
  emails contain. It listens only on 127.0.0.1:8787; Tailscale Funnel is what publishes it on the internet
  (https://<this-pc>.<tailnet>.ts.net). It holds no Google credentials, never touches the Sheet and
  never sends email.

  Restart protection: the task restarts the server (up to 5 times, 1 minute apart) if it crashes.
  Only one copy can run: MultipleInstances = IgnoreNew, and a second copy cannot bind the same port.

  Nothing is registered unless you pass -Install. Run with no switches to see the plan only.

.PARAMETER Install
  Create or replace the scheduled task for the current user (starts at logon).
.PARAMETER Uninstall
  Remove the scheduled task.
.PARAMETER Status
  Show the task state and whether the local server answers.
.PARAMETER TaskName
  Name of the scheduled task (default pyEmail-Tracker).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install_tracker_task.ps1            # show the plan
  powershell -ExecutionPolicy Bypass -File scripts\install_tracker_task.ps1 -Install
  powershell -ExecutionPolicy Bypass -File scripts\install_tracker_task.ps1 -Status
  powershell -ExecutionPolicy Bypass -File scripts\install_tracker_task.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [switch]$Install,
    [switch]$Uninstall,
    [switch]$Status,
    [string]$TaskName = 'pyEmail-Tracker'
)

$ErrorActionPreference = 'Stop'
$ProjectDir = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $ProjectDir 'run.py'))) { throw "run.py not found in $ProjectDir" }

function Resolve-Python {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd) { throw 'python was not found on PATH' }
    $dir = Split-Path -Parent $cmd.Source
    $windowless = Join-Path $dir 'pythonw.exe'
    if (Test-Path $windowless) { return $windowless }   # no console window
    return $cmd.Source
}

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'. (A running server keeps running until you log off or stop it.)"
    } else {
        Write-Host "No scheduled task named '$TaskName'."
    }
    return
}

if ($Status) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) { Write-Host "Task      : $TaskName ($($task.State))" } else { Write-Host "No scheduled task named '$TaskName'." }
    try {
        $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 http://127.0.0.1:8787/healthz
        Write-Host "Local server : answering ($($r.StatusCode))"
    } catch { Write-Host "Local server : NOT answering on 127.0.0.1:8787" }
    return
}

$python    = Resolve-Python
$arguments = 'run.py track serve'
$action    = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $ProjectDir
$trigger   = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings  = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
                 -ExecutionTimeLimit ([TimeSpan]::Zero) `
                 -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) `
                 -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Write-Host "Plan"
Write-Host "  Task name        : $TaskName"
Write-Host "  Command          : $python $arguments"
Write-Host "  Working folder   : $ProjectDir"
Write-Host "  Starts           : at logon of $env:USERDOMAIN\$env:USERNAME, restarts on crash (5 x, 1 minute apart)"
Write-Host "  Listens on       : 127.0.0.1:8787 only (published separately by Tailscale Funnel)"
Write-Host "  Google access    : none (no credentials, no Sheet access)"
Write-Host "  Sends email?     : never"

if (-not $Install) {
    Write-Host ""
    Write-Host "Nothing was registered. Add -Install to create the task."
    return
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description 'pyEmail: tracking endpoint for the open pixel (and optional click links). Never sends email.' `
    -Force | Out-Null
Write-Host ""
Write-Host "Installed scheduled task '$TaskName'. Check it with:  scripts\install_tracker_task.ps1 -Status"
