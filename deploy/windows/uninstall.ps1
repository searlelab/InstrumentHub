param([string]$InstallDirectory="C:\Program Files\InstrumentHub")
$ErrorActionPreference="Stop"
Unregister-ScheduledTask -TaskName "InstrumentHub Daily Backup" -Confirm:$false -ErrorAction SilentlyContinue
& "$InstallDirectory\InstrumentHubCaddy.exe" stop
& "$InstallDirectory\InstrumentHub.exe" stop
& "$InstallDirectory\InstrumentHubCaddy.exe" uninstall
& "$InstallDirectory\InstrumentHub.exe" uninstall
Write-Host "Services removed. Application and data directories were retained for recovery."

