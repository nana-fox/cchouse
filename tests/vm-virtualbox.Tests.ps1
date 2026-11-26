# Function tests for scripts/vm-virtualbox.ps1.
# Dot-source only. The native stderr test uses a fake VBoxManage command.
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

$vmLines = @(
  'name="claude-cc01"',
  'VMState="running"',
  'Forwarding(0)="ssh,tcp,127.0.0.1,2301,,22"'
)
& {
  $installDir = Join-Path ([System.IO.Path]::GetTempPath()) "VirtualBox"
  $probeArgs = @{
    Name = "claude-cc01"
    Lines = $vmLines
    Port = 2301
    VBoxManage = Join-Path $installDir "VBoxManage.exe"
  }
  $connection = [pscustomobject]@{ OwningProcess = 1234 }
  $process = [pscustomobject]@{ CommandLine = $null; Path = Join-Path $installDir "VBoxHeadless.exe" }
  $log = [pscustomobject]@{ ExitCode = 0; Output = @("00:00:00.399544 Process ID: 1234") }
  function Get-NetTCPConnection { if ($null -ne $connection) { return $connection } }
  function Get-CimInstance { return $process }
  function Get-Process { return $process }
  function Invoke-CchouseVBoxOptional {
    param([string[]]$CcArgumentList)
    Assert-Equal "log for requested VM" ($CcArgumentList -join " ") "showvminfo claude-cc01 --log=0"
    return $log
  }

  Assert-Equal "probe when CommandLine is empty and log PID matches" (Resolve-CchouseProbeDecision @probeArgs) "probe"
  $connection.OwningProcess = 4321
  Assert-Equal "reject mismatched PID even with a VirtualBox executable" (Resolve-CchouseProbeDecision @probeArgs) "reject"
  $connection.OwningProcess = 1234
  $probeArgs.Port = 2222
  Assert-Equal "reject wrong forward rule despite matching PID" (Resolve-CchouseProbeDecision @probeArgs) "reject"
  $probeArgs.Port = 2301
  $probeArgs.Name = "claude-cc02"
  Assert-Equal "reject wrong VM name" (Resolve-CchouseProbeDecision @probeArgs) "reject"
  $probeArgs.Name = "claude-cc01"
  $log.ExitCode = 1
  Assert-Equal "probe installed VBoxHeadless when log is unavailable" (Resolve-CchouseProbeDecision @probeArgs) "probe"
  $process.Path = Join-Path $installDir "VirtualBoxVM.exe"
  Assert-Equal "probe installed VirtualBoxVM when log is unavailable" (Resolve-CchouseProbeDecision @probeArgs) "probe"
  $process.Path = Join-Path ([System.IO.Path]::GetTempPath()) "VBoxHeadless.exe"
  Assert-Equal "reject VBoxHeadless outside installation directory" (Resolve-CchouseProbeDecision @probeArgs) "reject"
  $process.Path = $null
  Assert-Equal "reject unreadable executable path" (Resolve-CchouseProbeDecision @probeArgs) "reject"
  $connection = $null
  Assert-Equal "wait for an unbound port" (Resolve-CchouseProbeDecision @probeArgs) "wait"
}

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
$bootVb = (($src -csplit "function Invoke-CchouseVBoxBoot", 2)[1] -csplit "function New-CchouseVBoxSnapshot", 2)[0]
$ownAt = $bootVb.IndexOf("Resolve-CchouseProbeDecision")
$verifyAt = $bootVb.LastIndexOf("Get-CchouseSshVerifyOptions")
Assert-True "ownership before recording ssh" (($ownAt -ge 0) -and ($verifyAt -gt $ownAt))
Assert-True "trust mark before start" ($bootVb.IndexOf("Write-CchouseHostKeyTrustMark") -lt $bootVb.IndexOf("Start-CchouseVmDetached"))
Assert-True "visible unverified warning" $bootVb.Contains("未经串口核对主机密钥")
$deleteVb = (($src -csplit "function Remove-CchouseVBoxVM", 2)[1] -csplit "function New-CchouseVBoxSnapshot", 2)[0]
Assert-True "power off before unregister" ($deleteVb.IndexOf("poweroff") -lt $deleteVb.IndexOf("unregistervm"))
Assert-True "unregister deletes disks" $deleteVb.Contains("--delete")
Assert-True "exact name" $deleteVb.Contains('-cne $Name')
Assert-True "serial trust before start" ($bootVb.IndexOf("Resolve-CchouseHostKeyTrust") -lt $bootVb.IndexOf("Start-CchouseVmDetached"))
$fbVb = (($src -csplit "function Invoke-CchouseVBoxFirstboot", 2)[1] -csplit "function Invoke-CchouseVBoxBoot", 2)[0]
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
  if ($env:OS -eq "Windows_NT") {
    $fakeVBox = Join-Path $tmp "VBoxManage.cmd"
    [System.IO.File]::WriteAllLines($fakeVBox, @(
      "@echo off",
      'if "%2"=="test-registered" exit /b 0',
      "echo VM not registered 1>&2",
      "exit /b 1"
    ), [System.Text.Encoding]::ASCII)
    $oldVBox = $env:CCHOUSE_VBOXMANAGE
    $env:CCHOUSE_VBOXMANAGE = $fakeVBox
    try {
      Assert-False "native stderr and exit 1 mean VM is unregistered" (Test-CchouseVmRegistered "test-unregistered")
      Assert-True "native exit 0 means VM is registered" (Test-CchouseVmRegistered "test-registered")
      Assert-Equal "optional VBoxManage calls preserve caller preference" $ErrorActionPreference "Stop"
    } finally {
      $env:CCHOUSE_VBOXMANAGE = $oldVBox
    }
  }

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
  Write-CchouseHostKeyTrustMark -SerialLogPath $serial -Pinned:$false
  $trustPath = Join-Path $tmp "logs\hostkey.trust"
  Assert-Equal "accept-new mark" ([System.IO.File]::ReadAllText($trustPath)) "accept-new`n"
  Write-CchouseHostKeyTrustMark -SerialLogPath $serial -Pinned:$true
  Assert-Equal "serial mark" ([System.IO.File]::ReadAllText($trustPath)) "serial`n"
} finally {
  if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Recurse -Force }
}

$vmwarePath = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\scripts\vm-vmware.ps1"))
$vmware = [System.IO.File]::ReadAllText($vmwarePath)
Assert-False "vmware has no keyscan" $vmware.Contains("ssh-keyscan")
Assert-False "vmware has no strict no" $vmware.Contains("StrictHostKeyChecking=no")
Assert-True "vmware uses helper" $vmware.Contains("windows-hostkey.ps1")
Assert-True "vmware uses deadline" $vmware.Contains("CchouseSshWaitSeconds")
$bootVm = (($vmware -csplit "function Invoke-CchouseBoot", 2)[1] -csplit "function New-CchouseSnapshot", 2)[0]
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
