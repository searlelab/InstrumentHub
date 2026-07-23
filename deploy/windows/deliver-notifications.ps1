param(
  [string]$InstallDirectory="C:\Program Files\InstrumentHub",
  [string]$DataDirectory="C:\ProgramData\InstrumentHub"
)
$ErrorActionPreference="Stop"
. "$PSScriptRoot\common.ps1"
$env:INSTRUMENTHUB_INSTANCE=$DataDirectory
Invoke-Native "$InstallDirectory\venv\Scripts\python.exe" @("-m","instrumenthub","deliver-notifications","--limit","100")
