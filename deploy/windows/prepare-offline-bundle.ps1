param(
  [Parameter(Mandatory=$true)][string]$SourceDirectory,
  [Parameter(Mandatory=$true)][string]$OutputDirectory,
  [Parameter(Mandatory=$true)][string]$CaddyExecutable,
  [Parameter(Mandatory=$true)][string]$WinSWExecutable
)
$ErrorActionPreference="Stop"
. "$PSScriptRoot\common.ps1"
foreach ($path in @($SourceDirectory,$CaddyExecutable,$WinSWExecutable)) { if (-not (Test-Path $path)) { throw "Required input is missing: $path" } }
New-Item -ItemType Directory -Force -Path $OutputDirectory, "$OutputDirectory\wheelhouse", "$OutputDirectory\bin" | Out-Null
Copy-Item "$SourceDirectory\instrumenthub" $OutputDirectory -Recurse -Force
Copy-Item "$SourceDirectory\deploy" $OutputDirectory -Recurse -Force
Copy-Item "$SourceDirectory\docs" $OutputDirectory -Recurse -Force
Copy-Item "$SourceDirectory\pyproject.toml", "$SourceDirectory\requirements.lock" $OutputDirectory -Force
Copy-Item $CaddyExecutable "$OutputDirectory\bin\caddy.exe" -Force
Copy-Item $WinSWExecutable "$OutputDirectory\bin\WinSW.exe" -Force
Invoke-Native "py" @("-3.12","-m","pip","download","--only-binary=:all:","--no-deps","--dest","$OutputDirectory\wheelhouse","-r","$SourceDirectory\requirements.lock")
Invoke-Native "py" @("-3.12","-m","pip","wheel","--no-deps","--wheel-dir","$OutputDirectory\wheelhouse",$SourceDirectory)
Write-Host "Offline bundle prepared at $OutputDirectory. Transfer it with the lab-issued certificate and key."
