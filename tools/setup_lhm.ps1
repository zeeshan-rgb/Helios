# One-time setup for Helios's CPU-temperature + fan-RPM sensors (LibreHardwareMonitor).
#
# CPU core temps and fan tach require Ring0 (kernel) access, which needs admin. Helios itself runs
# un-elevated, so this registers the sensor helper (helios/lhm_sensors.py) as its OWN scheduled task
# at the HIGHEST run level (elevated, no UAC prompt at logon), mirroring the cua-driver daemon. The
# helper then writes data/lhm.json every few seconds; the system doctor reads it.
#
# Run this ONCE:  right-click > Run with PowerShell, or:  ! powershell -ExecutionPolicy Bypass -File tools\setup_lhm.ps1
# It self-elevates (one UAC prompt). The DLLs are already in data/lhm/ (downloaded from NuGet).

$ErrorActionPreference = "Stop"

# --- self-elevate ---
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
    [Security.Principal.WindowsBuiltinRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "Requesting administrator (one UAC prompt) to register the elevated sensor task..."
    Start-Process powershell.exe "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`"" -Verb RunAs
    exit
}

# --- paths (repo root = this script's parent dir) ---
$repo = Split-Path $PSScriptRoot -Parent
$py = Join-Path $repo ".venv\Scripts\pythonw.exe"
$script = Join-Path $repo "helios\lhm_sensors.py"
$task = "Helios-Sensors"

if (-not (Test-Path $py))     { throw "venv python not found at $py" }
if (-not (Test-Path $script)) { throw "helper not found at $script" }
if (-not (Test-Path (Join-Path $repo "data\lhm\LibreHardwareMonitorLib.dll"))) {
    throw "LibreHardwareMonitorLib.dll missing in data\lhm\ - re-run the Helios setup that downloads it."
}

# --- register the elevated, run-at-logon task ---
$action  = New-ScheduledTaskAction  -Execute $py -Argument "`"$script`"" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger  -AtLogOn -User $env:USERNAME
$princ   = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
$set     = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                                        -StartWhenAvailable -RestartCount 3 `
                                        -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $task -Action $action -Trigger $trigger -Principal $princ `
                       -Settings $set -Force | Out-Null
Start-ScheduledTask -TaskName $task

Write-Host ""
Write-Host "Done. '$task' is registered (elevated) and started." -ForegroundColor Green
Write-Host "CPU temperatures + fan RPM will now appear in Helios's system doctor within ~10s."
Write-Host "(It auto-starts at every logon. To remove: Unregister-ScheduledTask -TaskName $task)"
Start-Sleep -Seconds 4
$j = Join-Path $repo "data\lhm.json"
if (Test-Path $j) { Write-Host "`nCurrent sensor read:`n$(Get-Content $j -Raw)" }
