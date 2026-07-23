param([Parameter(Mandatory=$true)][string]$Backup,[string]$InstallDirectory="C:\Program Files\InstrumentHub",[string]$DataDirectory="C:\ProgramData\InstrumentHub")
$ErrorActionPreference="Stop"
. "$PSScriptRoot\common.ps1"
$env:INSTRUMENTHUB_INSTANCE=$DataDirectory
Invoke-Native "$InstallDirectory\InstrumentHub.exe" @("stop")
$failure=$null
try {
  Invoke-Native "$InstallDirectory\venv\Scripts\python.exe" @("-m","instrumenthub","verify-backup",$Backup)
  Invoke-Native "$InstallDirectory\venv\Scripts\python.exe" @("-m","instrumenthub","restore",$Backup,"--confirm-restore")
} catch { $failure=$_ } finally {
  # Always attempt the restart. A restart failure is itself a failure, but it must not
  # overwrite an earlier restore error.
  & "$InstallDirectory\InstrumentHub.exe" start
  if ($LASTEXITCODE -ne 0) {
    Write-Warning "Service restart reported exit code $LASTEXITCODE."
    if (-not $failure) { $failure="Service restart failed with exit code $LASTEXITCODE." }
  }
}
if ($failure) { Write-Error "Restore failed: $failure"; exit 1 }
