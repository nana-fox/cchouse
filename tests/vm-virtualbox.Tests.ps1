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
Assert-False "no ssh-keyscan" $src.Contains("ssh-keyscan")
Assert-False "no strict no" $src.Contains("StrictHostKeyChecking=no")
Assert-True "deadline symbol" $src.Contains("CchouseSshWaitSeconds")
Assert-True "loads hostkey helper" $src.Contains("windows-hostkey.ps1")
$bootVb = $src.Split("function Invoke-CchouseVBoxBoot")[1].Split("function New-CchouseVBoxSnapshot")[0]
$ownAt = $bootVb.LastIndexOf("Test-CchouseOwnForward")
$verifyAt = $bootVb.LastIndexOf("Get-CchouseSshVerifyOptions")
Assert-True "ownership before recording ssh" (($ownAt -ge 0) -and ($verifyAt -gt $ownAt))
Assert-True "serial trust before start" ($bootVb.IndexOf("Resolve-CchouseHostKeyTrust") -lt $bootVb.IndexOf("Start-CchouseVmDetached"))
$fbVb = $src.Split("function Invoke-CchouseVBoxFirstboot")[1].Split("function Invoke-CchouseVBoxBoot")[0]
Assert-True "uart before first start" ($fbVb.IndexOf("Enable-CchouseVBoxSerialLog") -lt $fbVb.IndexOf("Start-CchouseVmDetached"))
Assert-False "no hardcoded ssh port default" $src.Contains('$CcSshPort = 2222')

$demoKey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDemoTestKeyForUnitTestsOnly"
$otherKey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDemoTestKeyForUnitTestsTwo"
$noise = "kernel line`r`nCCHOUSE_HOSTKEY ssh-ed25519 AAAA comment`nCCHOUSE_HOSTKEY $demoKey`nCCHOUSE_HOSTKEY $demoKey`n"
Assert-True "no key in noise" ($null -eq (Get-CchouseHostKeyFromSerialText -Text "kernel only"))
Assert-Equal "duplicate key" (Get-CchouseHostKeyFromSerialText -Text $noise) $demoKey
Assert-Throws "conflicting keys" { Get-CchouseHostKeyFromSerialText -Text "CCHOUSE_HOSTKEY $demoKey`nCCHOUSE_HOSTKEY $otherKey`n" } "conflicting"
$bracket = Resolve-CchouseHostKeyTrust -SerialText "CCHOUSE_HOSTKEY $demoKey" -HostName "127.0.0.1" -Port 2301
Assert-True "pinned port" ([bool]$bracket.Pinned)
Assert-Equal "bracket line" $bracket.KnownHostsLine "[127.0.0.1]:2301 $demoKey"
$plain = Resolve-CchouseHostKeyTrust -SerialText "CCHOUSE_HOSTKEY $demoKey" -HostName "192.168.140.20" -Port 22
Assert-Equal "plain line" $plain.KnownHostsLine "192.168.140.20 $demoKey"
Assert-Equal "port zero" (Format-CchouseKnownHostsLine -HostName "192.168.140.20" -Port 0 -Key $demoKey) "192.168.140.20 $demoKey"
$missing = Resolve-CchouseHostKeyTrust -SerialText "" -HostName "127.0.0.1" -Port 2301
Assert-False "not pinned" ([bool]$missing.Pinned)
$opts = @(Get-CchouseSshVerifyOptions -KnownHostsPath "C:\Users\a b\known_hosts" -Pinned:$true -ConnectTimeout 5)
Assert-Equal "option count" ([string]$opts.Count) "12"
Assert-True "strict yes" ($opts -contains "StrictHostKeyChecking=yes")
Assert-True "forward slashes" ($opts -contains 'UserKnownHostsFile="C:/Users/a b/known_hosts"')
Assert-False "no backslash in options" (($opts -join " ") -match '\\')
Assert-True "connect timeout" ($opts -contains "ConnectTimeout=5")
Assert-True "one attempt" ($opts -contains "ConnectionAttempts=1")
$fallback = @(Get-CchouseSshVerifyOptions -KnownHostsPath "C:\kh" -Pinned:$false -ConnectTimeout 9)
Assert-True "accept-new" ($fallback -contains "StrictHostKeyChecking=accept-new")
Assert-True "timeout capped" ($fallback -contains "ConnectTimeout=5")
Assert-False "never no" ($fallback -contains "StrictHostKeyChecking=no")
$floor = @(Get-CchouseSshVerifyOptions -KnownHostsPath "C:\kh" -Pinned:$true -ConnectTimeout 0)
Assert-True "timeout floor" ($floor -contains "ConnectTimeout=1")
Assert-True "deadline past" (Test-CchouseSshDeadlineReached -StartedAt (Get-Date).AddSeconds(-181) -LimitSeconds 180)
Assert-False "deadline fresh" (Test-CchouseSshDeadlineReached -StartedAt (Get-Date) -LimitSeconds 180)
Assert-True "changed key" (Test-CchouseHostKeyMismatch -Stderr "WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!")
Assert-True "verify failed" (Test-CchouseHostKeyMismatch -Stderr "Host key verification failed.")
Assert-False "connection refused is not a mismatch" (Test-CchouseHostKeyMismatch -Stderr "Connection refused")

$tmp = Join-Path ([System.IO.Path]::GetTempPath()) "cchouse-hostkey-fn"
if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Recurse -Force }
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
  $serial = Join-Path $tmp "logs\hostkey.serial"
  Initialize-CchouseHostKeyLog -Path $serial
  Assert-Equal "created empty" ([System.IO.File]::ReadAllText($serial)) ""
  $payload = "boot noise`nCCHOUSE_HOSTKEY $demoKey`n"
  [System.IO.File]::WriteAllText($serial, $payload, [System.Text.UTF8Encoding]::new($false))
  Initialize-CchouseHostKeyLog -Path $serial
  $readBack = Read-CchouseSerialText -Path $serial
  Assert-True "initialize keeps the key" $readBack.Contains($demoKey)
  $round = Resolve-CchouseHostKeyTrust -SerialText $readBack -HostName "127.0.0.1" -Port 2301
  $kh = Join-Path $tmp "known_hosts"
  Write-CchouseKnownHostsFile -Path $kh -Line $round.KnownHostsLine
  Assert-Equal "known_hosts byte" ([System.IO.File]::ReadAllText($kh)) ($round.KnownHostsLine + "`n")
} finally {
  if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Recurse -Force }
}

$vmwarePath = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\scripts\vm-vmware.ps1"))
$vmware = [System.IO.File]::ReadAllText($vmwarePath)
Assert-False "vmware has no keyscan" $vmware.Contains("ssh-keyscan")
Assert-False "vmware has no strict no" $vmware.Contains("StrictHostKeyChecking=no")
Assert-True "vmware uses helper" $vmware.Contains("windows-hostkey.ps1")
Assert-True "vmware uses deadline" $vmware.Contains("CchouseSshWaitSeconds")
$bootVm = $vmware.Split("function Invoke-CchouseBoot")[1].Split("function New-CchouseSnapshot")[0]
Assert-True "vmware ip before verify" (($bootVm.IndexOf("getGuestIPAddress") -ge 0) -and ($bootVm.IndexOf("getGuestIPAddress") -lt $bootVm.IndexOf("Get-CchouseSshVerifyOptions")))
Assert-True "vmware parse before start" ($bootVm.IndexOf("Get-CchouseHostKeyFromSerialText") -lt $bootVm.IndexOf("Start-VmDetached"))
Assert-False "hard poweroff wait is not ignored" $src.Contains('Wait-CchousePowerOff $Name 20 | Out-Null')
Assert-True "single IDE regex" $src.Contains('^storagecontrollername\d+="([^"]*IDE[^"]*)"')
Assert-False "second match does not clobber the capture" $src.Contains('$Matches[1] -match')

if ($script:Failures.Count -gt 0) {
  foreach ($item in $script:Failures) { Write-Host "FAIL $item" }
  exit 1
}
Write-Host "OK vm-virtualbox function tests"
exit 0
