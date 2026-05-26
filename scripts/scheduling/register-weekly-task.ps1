<#
.SYNOPSIS
Register a weekly Windows Task Scheduler job that runs run-ditat-weekly.ps1.

.DESCRIPTION
Run this ONCE. After this completes, ditat verification will run automatically
every Monday at 9:00 AM local time (configurable below). The task only fires
while the user is logged on, so Claude Code can launch normally.

.NOTES
Run as the same user who normally uses Claude Code. No admin rights needed.
To change the schedule, edit $DayOfWeek / $StartTime below and re-run.
#>

# ------------------------------------------------------------------ User settings

$TaskName  = "DitatVerify-Weekly"
$DayOfWeek = "Monday"        # Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday
$StartTime = "09:00"         # 24-hour HH:mm, local time
$RunScript = "$PSScriptRoot\run-ditat-weekly.ps1"

# ------------------------------------------------------------------ Implementation

$ErrorActionPreference = "Stop"

if (-not (Test-Path $RunScript)) {
    Write-Error "Cannot find runner: $RunScript"
    exit 1
}

# Build the action: powershell.exe -NoProfile -ExecutionPolicy Bypass -File run-ditat-weekly.ps1
$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$RunScript`"" `
    -WorkingDirectory $PSScriptRoot

$trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek $DayOfWeek `
    -At $StartTime

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -MultipleInstances IgnoreNew

# Run only when this user is logged on (interactive). No password storage.
$principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited

# Unregister an existing instance with the same name (idempotent).
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed existing task '$TaskName' so we can re-create it."
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "Weekly Ditat shipment verification — runs run-ditat-weekly.ps1" `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal | Out-Null

Write-Host ""
Write-Host "Task registered: $TaskName" -ForegroundColor Green
Write-Host "  Schedule : Every $DayOfWeek at $StartTime"
Write-Host "  Runs     : $RunScript"
Write-Host "  Log      : (set inside run-ditat-weekly.ps1 — default `$HOME\ditat-verify\.scheduled-runs.log)"
Write-Host ""
Write-Host "Verify in: Task Scheduler  →  Task Scheduler Library  →  '$TaskName'"
Write-Host "Trigger a test run now:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Remove:                  scripts\scheduling\unregister-weekly-task.ps1"
