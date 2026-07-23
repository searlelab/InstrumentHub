# Shared helpers for InstrumentHub operational scripts.
# PowerShell does not turn native executable exit codes into terminating errors, so
# $ErrorActionPreference="Stop" alone would let a failed backup or restore report success.
# Every native invocation must assert its own exit code.
function Invoke-Native {
  param([Parameter(Mandatory=$true)][string]$Executable,[string[]]$Arguments=@())
  & $Executable @Arguments
  if ($LASTEXITCODE -ne 0) { throw "Command failed with exit code ${LASTEXITCODE}: $Executable $($Arguments -join ' ')" }
}
