# Register a Windows Scheduled Task that runs the bot every N minutes, unattended.
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 -IntervalMinutes 30
# Remove with:  Unregister-ScheduledTask -TaskName PredictionEdge -Confirm:$false
param(
    [int]$IntervalMinutes = 30,
    [string]$TaskName = "PredictionEdge"
)
$ErrorActionPreference = "Stop"
$runner = Join-Path $PSScriptRoot "run_bot.ps1"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$runner`""

# Fire shortly from now, then repeat indefinitely at the chosen interval.
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "PredictionEdge autonomous Kalshi bot" -Force

Write-Host "Registered '$TaskName' to run every $IntervalMinutes minute(s)."
Write-Host "It runs whether or not Claude is open. Pause anytime by creating the file:"
Write-Host "  data\STOP   (delete it to resume)"
