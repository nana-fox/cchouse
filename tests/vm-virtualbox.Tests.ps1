# Function tests for scripts/vm-virtualbox.ps1.
# Dot-source only. Do not install VirtualBox or call VBoxManage, ssh, or the installer.
$ErrorActionPreference = "Stop"
$scriptPath = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\scripts\vm-virtualbox.ps1"))
. $scriptPath

$script:Failures = @()

function Add-Failure {
  param([string]$Message)
  $script:Failures += $Message
}

function Assert-Equal {
  param([string]$Name, $Got, $Expected)
  if ($Got -ne $Expected) {
    Add-Failure "$Name`: got [$Got] expected [$Expected]"
  }
}

function Assert-True {
  param([string]$Name, $Cond)
  if (-not $Cond) { Add-Failure "$Name`: expected true" }
}

function Assert-False {
  param([string]$Name, $Cond)
  if ($Cond) { Add-Failure "$Name`: expected false" }
}

function Assert-Throws {
  param([string]$Name, [scriptblock]$Block, [string]$Pattern)
  $threw = $false
  $msg = ""
  try {
    & $Block | Out-Null
  } catch {
    $threw = $true
    $msg = [string]$_.Exception.Message
  }
  if (-not $threw) {
    Add-Failure "$Name`: expected throw"
    return
  }
  if ($msg -notmatch $Pattern) {
    Add-Failure "$Name`: message [$msg] did not match $Pattern"
  }
}

$bare = Get-CchouseIdeControllerName -Lines @('storagecontrollername0="IDE"')
Assert-Equal "bare IDE" $bare "IDE"

$ideController = Get-CchouseIdeControllerName -Lines @(
  'storagecontrollername0="SATA"',
  'storagecontrollername1="IDE Controller"'
)
Assert-Equal "IDE Controller" $ideController "IDE Controller"
Assert-True "sata only is null" ($null -eq (Get-CchouseIdeControllerName -Lines @('storagecontrollername0="SATA"')))

$forward = @('Forwarding(0)="ssh,tcp,127.0.0.1,2301,,22"')
Assert-True "forward 2301" (Test-CchouseForwardRule -Lines $forward -Port 2301)
Assert-False "forward not 2222" (Test-CchouseForwardRule -Lines $forward -Port 2222)

$uuid = "vbox-vm-cc01"
$vmLines = @(
  "UUID=`"$uuid`"",
  'name="claude-cc01"',
  'Forwarding(0)="ssh,tcp,127.0.0.1,2301,,22"'
)
$ownCmd = "C:\Program Files\Oracle\VirtualBox\VBoxHeadless.exe --comment claude-cc01 --startvm $uuid"
$otherCmd = "C:\Program Files\Oracle\VirtualBox\VBoxHeadless.exe --comment claude-cc02 --startvm vbox-vm-cc02"
$sshdCmd = "C:\Windows\System32\OpenSSH\sshd.exe -p 2301"
Assert-True "own listener" (Test-CchouseListenerCommand -CommandLine $ownCmd -VmUuid $uuid -VmName "claude-cc01")
Assert-False "other vm" (Test-CchouseListenerCommand -CommandLine $otherCmd -VmUuid $uuid -VmName "claude-cc01")
Assert-False "sshd" (Test-CchouseListenerCommand -CommandLine $sshdCmd -VmUuid $uuid -VmName "claude-cc01")
Assert-True "own forward" (Test-CchouseOwnForward -Lines $vmLines -Port 2301 -ListenerCommand $ownCmd)
Assert-False "wrong port is not this vm" (Test-CchouseOwnForward -Lines $vmLines -Port 2222 -ListenerCommand $ownCmd)
Assert-False "foreign listener" (Test-CchouseOwnForward -Lines $vmLines -Port 2301 -ListenerCommand $otherCmd)
Assert-Equal "probe own" (Resolve-CchouseProbeDecision -Lines $vmLines -Port 2301 -ListenerCommand $ownCmd -ListenerPresent:$true) "probe"
Assert-Equal "wait if silent" (Resolve-CchouseProbeDecision -Lines $vmLines -Port 2301 -ListenerPresent:$false) "wait"
Assert-Equal "reject foreign" (Resolve-CchouseProbeDecision -Lines $vmLines -Port 2301 -ListenerCommand $sshdCmd -ListenerPresent:$true) "reject"
Assert-Equal "reject unreadable" (Resolve-CchouseProbeDecision -Lines $vmLines -Port 2301 -ListenerCommand "" -ListenerPresent:$true) "reject"
Assert-Equal "reject wrong rule" (Resolve-CchouseProbeDecision -Lines $vmLines -Port 2222 -ListenerCommand $ownCmd -ListenerPresent:$true) "reject"

Assert-Throws "empty sha" { Install-CchouseVirtualBox -Installer "C:\no-such-vbox-installer.exe" -ExpectedSha "" } "CcSha256"
Assert-Throws "short sha" { Install-CchouseVirtualBox -Installer "C:\no-such-vbox-installer.exe" -ExpectedSha "abcd" } "CcSha256"
$validSha = "a" * 64
$rejectedValid = $false
try {
  Install-CchouseVirtualBox -Installer "C:\no-such-vbox-installer.exe" -ExpectedSha $validSha | Out-Null
} catch {
  if ([string]$_.Exception.Message -match "CcSha256") { $rejectedValid = $true }
}
Assert-False "64 hex is accepted as a hash" $rejectedValid

Assert-Throws "both stops failed" { Resolve-CchouseFirstbootStop -AfterAcpi:$false -AfterHard:$false } "timed out"
Assert-Equal "acpi stop" (Resolve-CchouseFirstbootStop -AfterAcpi:$true -AfterHard:$false) "acpi"
Assert-Equal "hard stop" (Resolve-CchouseFirstbootStop -AfterAcpi:$false -AfterHard:$true) "hard"

Assert-Throws "port 0" { Assert-CchouseSshPort -Port 0 } "CcSshPort is required"
Assert-Throws "port 65536" { Assert-CchouseSshPort -Port 65536 } "CcSshPort is required"
Assert-CchouseSshPort -Port 2301

$installArgs = @(Get-CchouseVirtualBoxInstallArgs)
if ($installArgs.Count -eq 1 -and $installArgs[0] -is [System.Array]) {
  $installArgs = @($installArgs[0])
}
$msiAt = [array]::IndexOf($installArgs, "--msiparams")
Assert-True "msiparams present" ($msiAt -ge 0)
Assert-True "msiparams not last" ($msiAt -lt ($installArgs.Count - 1))
Assert-Equal "ADDLOCAL only" $installArgs[$msiAt + 1] "ADDLOCAL=VBoxApplication"
for ($i = $msiAt + 1; $i -lt $installArgs.Count; $i++) {
  Assert-True "property $i" ($installArgs[$i] -match '^[A-Za-z0-9_]+=[^ ]+$')
}

$src = [System.IO.File]::ReadAllText($scriptPath)
$ownAt = $src.LastIndexOf("Test-CchouseOwnForward")
$keyAt = $src.LastIndexOf("CchouseSystem32Keyscan")
Assert-True "ownership check before keyscan" (($ownAt -ge 0) -and ($keyAt -gt $ownAt))
Assert-False "no hardcoded ssh port default" $src.Contains('$CcSshPort = 2222')
Assert-False "hard poweroff wait is not ignored" $src.Contains('Wait-CchousePowerOff $Name 20 | Out-Null')
Assert-True "single IDE regex" $src.Contains('^storagecontrollername\d+="([^"]*IDE[^"]*)"')
Assert-False "second match does not clobber the capture" $src.Contains('$Matches[1] -match')

if ($script:Failures.Count -gt 0) {
  foreach ($item in $script:Failures) { Write-Host "FAIL $item" }
  exit 1
}
Write-Host "OK vm-virtualbox function tests"
exit 0
