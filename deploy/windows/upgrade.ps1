param([Parameter(Mandatory=$true)][string]$ReleaseDirectory,[string]$InstallDirectory="C:\Program Files\InstrumentHub",[string]$DataDirectory="C:\ProgramData\InstrumentHub")
$ErrorActionPreference="Stop"
. "$PSScriptRoot\common.ps1"
$env:INSTRUMENTHUB_INSTANCE=$DataDirectory
$applicationWheels = @(Get-ChildItem "$ReleaseDirectory\wheelhouse\instrumenthub-*.whl")
if ($applicationWheels.Count -ne 1) { throw "Release wheelhouse must contain exactly one InstrumentHub wheel." }
$failure=$null
$hubStopped=$false
$caddyStopped=$false
try {
  Invoke-Native "$InstallDirectory\InstrumentHub.exe" @("stop")
  $hubStopped=$true
  Invoke-Native "$InstallDirectory\InstrumentHubCaddy.exe" @("stop")
  $caddyStopped=$true
  Invoke-Native "$InstallDirectory\venv\Scripts\python.exe" @("-m","instrumenthub","backup")
  Copy-Item "$ReleaseDirectory\*" $InstallDirectory -Recurse -Force
  Copy-Item "$InstallDirectory\bin\caddy.exe" "$InstallDirectory\caddy.exe" -Force
  Invoke-Native "$InstallDirectory\venv\Scripts\python.exe" @("-m","pip","install","--no-index","--no-deps","--find-links","$InstallDirectory\wheelhouse","-r","$InstallDirectory\requirements.lock")
  Invoke-Native "$InstallDirectory\venv\Scripts\python.exe" @("-m","pip","install","--no-index","--force-reinstall","--no-deps",$applicationWheels[0].FullName)
} catch { $failure=$_ } finally {
  # Always attempt the restart. A restart failure is itself a failure, but it must not
  # overwrite an earlier upgrade error.
  if ($hubStopped) {
    & "$InstallDirectory\InstrumentHub.exe" start
    if ($LASTEXITCODE -ne 0) {
      Write-Warning "Service restart reported exit code $LASTEXITCODE."
      if (-not $failure) { $failure="Service restart failed with exit code $LASTEXITCODE." }
    }
  }
  if ($caddyStopped) {
    & "$InstallDirectory\InstrumentHubCaddy.exe" start
    if ($LASTEXITCODE -ne 0) {
      Write-Warning "Caddy service restart reported exit code $LASTEXITCODE."
      if (-not $failure) { $failure="Caddy service restart failed with exit code $LASTEXITCODE." }
    }
  }
}
if ($failure) { Write-Error "Upgrade failed: $failure"; exit 1 }
