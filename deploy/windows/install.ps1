param(
  [Parameter(Mandatory=$true)][string]$ReleaseDirectory,
  [string]$InstallDirectory = "C:\Program Files\InstrumentHub",
  [string]$DataDirectory = "C:\ProgramData\InstrumentHub",
  [string]$ServiceAccount = "NT AUTHORITY\LocalService",
  [ValidateRange(1,3650)][int]$BackupRetentionDays = 30
)
$ErrorActionPreference = "Stop"
. "$PSScriptRoot\common.ps1"
if (-not (Test-Path "$ReleaseDirectory\wheelhouse")) { throw "Offline wheelhouse is required." }
if (-not (Test-Path "$ReleaseDirectory\bin\WinSW.exe")) { throw "WinSW.exe is required in bin." }
if (-not (Test-Path "$ReleaseDirectory\bin\caddy.exe")) { throw "caddy.exe is required in bin." }
if (-not (Test-Path "$DataDirectory\certs\scheduler.crt") -or -not (Test-Path "$DataDirectory\certs\scheduler.key")) { throw "Lab-issued TLS certificate and key are required." }
$applicationWheels = @(Get-ChildItem "$ReleaseDirectory\wheelhouse\instrumenthub-*.whl")
if ($applicationWheels.Count -ne 1) { throw "Release wheelhouse must contain exactly one InstrumentHub wheel." }
New-Item -ItemType Directory -Force -Path $InstallDirectory, $DataDirectory, "$DataDirectory\logs", "$DataDirectory\backups" | Out-Null
Copy-Item "$ReleaseDirectory\*" $InstallDirectory -Recurse -Force
Invoke-Native "py" @("-3.12","-m","venv","$InstallDirectory\venv")
Invoke-Native "$InstallDirectory\venv\Scripts\python.exe" @("-m","pip","install","--no-index","--no-deps","--find-links","$InstallDirectory\wheelhouse","-r","$InstallDirectory\requirements.lock")
Invoke-Native "$InstallDirectory\venv\Scripts\python.exe" @("-m","pip","install","--no-index","--force-reinstall","--no-deps",$applicationWheels[0].FullName)
Copy-Item "$InstallDirectory\deploy\windows\instrumenthub-service.xml" "$InstallDirectory\InstrumentHub.xml" -Force
Copy-Item "$InstallDirectory\deploy\windows\caddy-service.xml" "$InstallDirectory\InstrumentHubCaddy.xml" -Force
$builtInAccounts = @{ "localsystem" = "LocalSystem"; "nt authority\localservice" = "NT AUTHORITY\LocalService"; "nt authority\networkservice" = "NT AUTHORITY\NetworkService" }
$accountKey = $ServiceAccount.Trim().ToLowerInvariant()
if ($builtInAccounts.ContainsKey($accountKey)) { $ServiceAccount = $builtInAccounts[$accountKey] }
elseif ($ServiceAccount -notmatch '^[A-Za-z0-9.-]+\\[A-Za-z0-9._-]+\$$') { throw 'ServiceAccount must be LocalSystem, NT AUTHORITY\LocalService, NT AUTHORITY\NetworkService, or a DOMAIN\gMSA$ account.' }
$AclServiceAccount = if ($ServiceAccount -eq "LocalSystem") { "NT AUTHORITY\SYSTEM" } else { $ServiceAccount }
$escapedServiceAccount = [System.Security.SecurityElement]::Escape($ServiceAccount)
$escapedDataDirectory = [System.Security.SecurityElement]::Escape($DataDirectory)
foreach ($serviceXml in @("$InstallDirectory\InstrumentHub.xml", "$InstallDirectory\InstrumentHubCaddy.xml")) {
  $content = Get-Content $serviceXml -Raw
  $content = $content.Replace("__SERVICE_ACCOUNT__", $escapedServiceAccount)
  $content = $content.Replace("__DATA_DIRECTORY__", $escapedDataDirectory)
  Set-Content -Path $serviceXml -Value $content -NoNewline
}
Copy-Item "$InstallDirectory\bin\WinSW.exe" "$InstallDirectory\InstrumentHub.exe" -Force
Copy-Item "$InstallDirectory\bin\WinSW.exe" "$InstallDirectory\InstrumentHubCaddy.exe" -Force
Copy-Item "$InstallDirectory\bin\caddy.exe" "$InstallDirectory\caddy.exe" -Force
if (-not (Test-Path "$DataDirectory\Caddyfile")) {
  $caddy = (Get-Content "$InstallDirectory\deploy\windows\Caddyfile.example" -Raw).Replace("__DATA_DIRECTORY__", $DataDirectory.Replace("\", "/"))
  Set-Content -Path "$DataDirectory\Caddyfile" -Value $caddy -NoNewline
}
function Set-RestrictedAcl([string]$Path, [hashtable[]]$Rules) {
  $acl = Get-Acl -LiteralPath $Path
  $acl.SetAccessRuleProtection($true, $false)
  foreach ($rule in @($acl.Access)) { [void]$acl.RemoveAccessRuleSpecific($rule) }
  foreach ($rule in $Rules) {
    $accessRule = New-Object System.Security.AccessControl.FileSystemAccessRule($rule.Identity, $rule.Rights, "ContainerInherit, ObjectInherit", "None", "Allow")
    [void]$acl.AddAccessRule($accessRule)
  }
  Set-Acl -LiteralPath $Path -AclObject $acl
}
$dataRules = @(@{ Identity="NT AUTHORITY\SYSTEM"; Rights="FullControl" }, @{ Identity="BUILTIN\Administrators"; Rights="Modify" })
$certificateRules = @(@{ Identity="NT AUTHORITY\SYSTEM"; Rights="FullControl" }, @{ Identity="BUILTIN\Administrators"; Rights="Modify" })
$backupRules = @(@{ Identity="NT AUTHORITY\SYSTEM"; Rights="FullControl" }, @{ Identity="BUILTIN\Administrators"; Rights="Modify" })
if ($ServiceAccount -ne "LocalSystem") {
  $dataRules += @{ Identity=$AclServiceAccount; Rights="Modify" }
  $certificateRules += @{ Identity=$AclServiceAccount; Rights="ReadAndExecute" }
  $backupRules += @{ Identity=$AclServiceAccount; Rights="ReadAndExecute" }
}
Set-RestrictedAcl $DataDirectory $dataRules
Set-RestrictedAcl "$DataDirectory\certs" $certificateRules
Set-RestrictedAcl "$DataDirectory\backups" $backupRules
Invoke-Native "$InstallDirectory\InstrumentHub.exe" @("install")
Invoke-Native "$InstallDirectory\InstrumentHubCaddy.exe" @("install")
Invoke-Native "$InstallDirectory\InstrumentHub.exe" @("start")
Invoke-Native "$InstallDirectory\InstrumentHubCaddy.exe" @("start")
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$InstallDirectory\deploy\windows\backup.ps1`" -InstallDirectory `"$InstallDirectory`" -DataDirectory `"$DataDirectory`" -RetentionDays $BackupRetentionDays"
$trigger = New-ScheduledTaskTrigger -Daily -At 2am
Register-ScheduledTask -TaskName "InstrumentHub Daily Backup" -Action $action -Trigger $trigger -User "SYSTEM" -RunLevel Highest -Force | Out-Null
$notificationAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$InstallDirectory\deploy\windows\deliver-notifications.ps1`" -InstallDirectory `"$InstallDirectory`" -DataDirectory `"$DataDirectory`""
$notificationTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddMinutes(5) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 1)
Register-ScheduledTask -TaskName "InstrumentHub Notification Delivery" -Action $notificationAction -Trigger $notificationTrigger -User "SYSTEM" -RunLevel Highest -Force | Out-Null
Write-Host "Installed. Run the bootstrap-admin command interactively, then complete setup over HTTPS."
