param(
  [string]$InstallDirectory="C:\Program Files\InstrumentHub",
  [string]$DataDirectory="C:\ProgramData\InstrumentHub",
  [ValidateRange(1,3650)][int]$RetentionDays=30
)
$ErrorActionPreference="Stop"
. "$PSScriptRoot\common.ps1"
$env:INSTRUMENTHUB_INSTANCE=$DataDirectory
$env:INSTRUMENTHUB_BACKUP_RETENTION_DAYS=$RetentionDays.ToString()
Invoke-Native "$InstallDirectory\venv\Scripts\python.exe" @("-m","instrumenthub","backup")
