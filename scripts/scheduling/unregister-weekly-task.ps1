<#
.SYNOPSIS
Remove the weekly Ditat verification scheduled task.

.NOTES
Run as the same user who registered the task. No admin rights needed.
#>

$TaskName = "DitatVerify-Weekly"

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $existing) {
    Write-Host "No task named '$TaskName' is registered." -ForegroundColor Yellow
    exit 0
}

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Removed scheduled task '$TaskName'." -ForegroundColor Green
