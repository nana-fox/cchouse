# Function tests for scripts/vm-virtualbox.ps1.
# Dot-source only. Native-command tests use fake VBoxManage and SSH commands.
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
      'if "%2"=="test-registered" (',
      '  echo showvminfo warning 1>&2',
      '  echo healthy stdout',
      '  exit /b 0',
      ')',
      "echo VM not registered 1>&2",
      "exit /b 1"
    ), [System.Text.Encoding]::ASCII)
    $oldVBox = $env:CCHOUSE_VBOXMANAGE
    $env:CCHOUSE_VBOXMANAGE = $fakeVBox
    try {
      Assert-False "native stderr and exit 1 mean VM is unregistered" (Test-CchouseVmRegistered "test-unregistered")
      Assert-True "native exit 0 means VM is registered" (Test-CchouseVmRegistered "test-registered")
      $native = Invoke-CchouseNative -FilePath $fakeVBox -ArgumentList @("showvminfo", "test-registered")
      Assert-Equal "successful native command with stderr" $native.ExitCode 0
      Assert-Equal "stdout is separate from stderr" ($native.Output -join "|") "healthy stdout"
      Assert-True "native stderr was captured" ($native.Stderr -match "showvminfo warning")
      Assert-Equal "native calls preserve caller preference" $ErrorActionPreference "Stop"
    } finally {
      $env:CCHOUSE_VBOXMANAGE = $oldVBox
    }
  }

  & {
    $installer = Join-Path $tmp "VirtualBox test installer.exe"
    [System.IO.File]::WriteAllText($installer, "synthetic bytes")
    $pinnedSha = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash
    $launches = [System.Collections.Generic.List[object]]::new()
    $installerExit = 0
    function Start-Process {
      param($FilePath, $ArgumentList, $Verb, [switch]$Wait, [switch]$PassThru, $ErrorAction)
      $launches.Add($PSBoundParameters)
      return [pscustomobject]@{ ExitCode = $installerExit }
    }
    function Get-CchouseVBoxManage {
      if ($launches.Count -gt 0 -and $installerExit -eq 0) { return "installed-VBoxManage.exe" }
    }

    Assert-Throws "installer SHA-256 mismatch blocks elevation" {
      Install-CchouseVirtualBox -Installer $installer -ExpectedSha ("b" * 64)
    } "SHA-256 mismatch"
    Assert-Equal "bad installer was not launched" $launches.Count 0
    Install-CchouseVirtualBox -Installer $installer -ExpectedSha $pinnedSha
    Install-CchouseVirtualBox -Installer $installer -ExpectedSha $pinnedSha
    Assert-Equal "verified installer elevated exactly once" $launches.Count 1
    Assert-Equal "elevated the verified executable" $launches[0].FilePath $installer
    Assert-Equal "elevated via RunAs" $launches[0].Verb "RunAs"
    Assert-True "installer is awaited with process exit status" ($launches[0].Wait -and $launches[0].PassThru)
    Assert-True "elevated installer is silent NAT-only" (
      $launches[0].ArgumentList -contains "--silent" -and $launches[0].ArgumentList -contains "ADDLOCAL=VBoxApplication"
    )
    $launches.Clear()
    $installerExit = 1603
    Assert-Throws "installation failure propagates" {
      Install-CchouseVirtualBox -Installer $installer -ExpectedSha $pinnedSha
    } "1603"
    Assert-Equal "installer failure is not retried" $launches.Count 1
  }

  & {
    $vmDir = Join-Path $tmp "vm 测试"
    $ova = Join-Path $tmp "image.ova"
    [System.IO.File]::WriteAllText($ova, "synthetic OVA")
    $fixtureName = "claude-cc01"
    $fixtureUuid = [guid]::NewGuid().ToString()
    $otherUuid = [guid]::NewGuid().ToString()
    $config = Join-Path (Join-Path $vmDir $fixtureName) "$fixtureName.vbox"
    $vm = @{ Registered = $true; PowerOff = $true; Config = $config; ReportedName = $fixtureName; State = "running" }
    $commands = [System.Collections.Generic.List[string]]::new()
    function Invoke-CchouseVBox {
      param($CcArgumentList, $FailureMessage)
      switch ($CcArgumentList[0]) {
        "list" {
          Assert-Equal "only list registered VMs" ($CcArgumentList -join " ") "list vms"
          '"claude-cc01-extra" {{{0}}}' -f $otherUuid
          if ($vm.Registered) { '"{0}" {{{1}}}' -f $fixtureName, $fixtureUuid }
        }
        "controlvm" { $commands.Add($CcArgumentList -join " ") }
        "unregistervm" {
          $commands.Add($CcArgumentList -join " ")
          $vm.Registered = $false
        }
        default { throw "unexpected mocked VBoxManage operation" }
      }
    }
    function Get-CchouseShowvminfoLines {
      param($Name)
      Assert-Equal "lookup uses the registry UUID" $Name $fixtureUuid
      'name="{0}"' -f $vm.ReportedName
      'CfgFile="{0}"' -f $vm.Config.Replace('\', '\\')
      'VMState="{0}"' -f $vm.State
    }
    function Wait-CchousePowerOff {
      param($Name, $Seconds)
      $commands.Add("wait $Name")
      return $vm.PowerOff
    }
    function Invoke-CchouseVBoxOptional {
      param($CcArgumentList)
      return [pscustomobject]@{ ExitCode = 0; Output = @('VMState="poweroff"', 'SessionState="Unlocked"') }
    }

    Assert-Throws "fresh import cannot overwrite a registered VM" {
      Import-CchouseVBoxOva -OvaPath $ova -Name $fixtureName -DestDir $vmDir -SshPort 2301 -ReplaceExisting "no"
    } "not owned"
    $vm.ReportedName = "Claude-cc01"
    Assert-Throws "remove rejects wrong exact name" { Remove-CchouseVBoxVM -Name $fixtureName -VmDir $vmDir } "name does not match"
    $vm.ReportedName = $fixtureName
    $vm.Config = Join-Path $tmp "foreign.vbox"
    Assert-Throws "remove rejects same name outside instance path" { Remove-CchouseVBoxVM -Name $fixtureName -VmDir $vmDir } "outside"
    Assert-Equal "ownership failures do not touch the VM" $commands.Count 0
    $vm.Config = $config
    $vm.PowerOff = $false
    Assert-Throws "remove stops when VM does not power off" { Remove-CchouseVBoxVM -Name $fixtureName -VmDir $vmDir } "timed out"
    Assert-Equal "failed poweroff never unregisters" ($commands -join "|") "controlvm $fixtureUuid poweroff|wait $fixtureUuid"
    $commands.Clear()
    $vm.PowerOff = $true
    Remove-CchouseVBoxVM -Name $fixtureName -VmDir $vmDir
    Remove-CchouseVBoxVM -Name $fixtureName -VmDir $vmDir
    Assert-Equal "remove powers off then unregisters only the verified UUID" (
      $commands -join "|"
    ) "controlvm $fixtureUuid poweroff|wait $fixtureUuid|unregistervm $fixtureUuid --delete"

    if ($env:OS -eq "Windows_NT") {
      $prefix = '\\?\'
      foreach ($prefixedProviderPath in @($false, $true)) {
        $commands.Clear()
        $vm.Registered = $true
        $vm.Config = $config.Replace('\', '/').ToUpperInvariant()
        $removeDir = $vmDir
        if ($prefixedProviderPath) {
          $removeDir = $prefix + $removeDir
        } else {
          $vm.Config = $prefix + $vm.Config
        }
        Remove-CchouseVBoxVM -Name $fixtureName -VmDir $removeDir
        Assert-Equal "Windows remove normalizes extended paths ($prefixedProviderPath)" (
          $commands -join "|"
        ) "controlvm $fixtureUuid poweroff|wait $fixtureUuid|unregistervm $fixtureUuid --delete"
      }
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

  & {
    $startCalls = [System.Collections.Generic.List[string]]::new()
    $responses = [System.Collections.Generic.Queue[object]]::new()
    function Invoke-CchouseVBoxOptional {
      param([string[]]$CcArgumentList)
      $startCalls.Add($CcArgumentList -join " ")
      return $responses.Dequeue()
    }
    function Start-Sleep {}

    # 1. Synchronous startvm succeeds cleanly on first try
    $responses.Enqueue([pscustomobject]@{ ExitCode = 0; Output = @('VM "claude-cc01" has been successfully started.'); Stderr = "" })
    Start-CchouseVmDetached -Name "claude-cc01"
    Assert-Equal "startvm headless called synchronously" ($startCalls -join "|") "startvm claude-cc01 --type headless"

    # 2. Transient VBOX_E_INVALID_OBJECT_STATE session lock is retried and succeeds
    $startCalls.Clear()
    $responses.Enqueue([pscustomobject]@{
      ExitCode = 1
      Output = @()
      Stderr = "VBoxManage.exe: error: The machine 'claude-cc01' is already locked by a session (or being locked or unlocked)`nDetails: code VBOX_E_INVALID_OBJECT_STATE (0x80bb0007)"
    })
    $responses.Enqueue([pscustomobject]@{ ExitCode = 0; Output = @('VM "claude-cc01" has been successfully started.'); Stderr = "" })
    Start-CchouseVmDetached -Name "claude-cc01"
    Assert-Equal "transient session lock retried once" $startCalls.Count 2

    # 3. Non-lock launch failure fails fast without retrying and includes stderr
    $startCalls.Clear()
    $responses.Enqueue([pscustomobject]@{
      ExitCode = 1
      Output = @()
      Stderr = "VBoxManage.exe: error: VT-x is not available (VERR_VMX_NO_VMX)"
    })
    Assert-Throws "non-lock startvm error fails fast with stderr" {
      Start-CchouseVmDetached -Name "claude-cc01"
    } "VERR_VMX_NO_VMX"
    Assert-Equal "non-lock error is not retried" $startCalls.Count 1

    # 4. Persistent session lock throws after exhausting retries
    $startCalls.Clear()
    for ($i = 0; $i -lt 5; $i++) {
      $responses.Enqueue([pscustomobject]@{
        ExitCode = 1
        Output = @()
        Stderr = "VBoxManage.exe: error: The machine 'claude-cc01' is already locked by a session"
      })
    }
    Assert-Throws "persistent session lock throws after 5 attempts" {
      Start-CchouseVmDetached -Name "claude-cc01"
    } "already locked by a session"
    Assert-Equal "persistent session lock retried 5 times" $startCalls.Count 5
  }
  $startFn = (($src -csplit "function Start-CchouseVmDetached", 2)[1] -csplit "function Wait-CchousePowerOff", 2)[0]
  Assert-False "startvm does not use fire-and-forget Win32_Process" $startFn.Contains("Win32_Process")

  & {
    $states = [System.Collections.Generic.Queue[string]]::new()
    function Invoke-CchouseVBoxOptional { return @{ ExitCode = 0; Output = @($states.Dequeue()) } }
    function Start-Sleep {}
    foreach ($withMarker in @($true, $false)) {
      $states.Enqueue('VMState="poweroff"')
      $states.Enqueue('VMState="running"')
      $states.Enqueue('VMState="poweroff"')
      $completionLog = Join-Path $tmp "firstboot.serial"
      [System.IO.File]::WriteAllText($completionLog, "")
      if ($withMarker) {
        # VBoxManage starts asynchronously. Publish the token only after the VM runs.
        function Start-Sleep {
          if ($states.Count -eq 1) {
            [System.IO.File]::WriteAllText($completionLog, "noise`r`nCCHOUSE_FIRSTBOOT_DONE`r`n")
          }
        }
        Assert-True "firstboot waits for provisioning and clean poweroff" (
          Wait-CchousePowerOff -Name "claude-cc01" -Seconds 3 -FirstbootLog $completionLog
        )
        function Start-Sleep {}
      } else {
        Assert-Throws "early poweroff does not confirm firstboot" {
          Wait-CchousePowerOff -Name "claude-cc01" -Seconds 3 -FirstbootLog $completionLog
        } "without confirming"
      }
    }
    $states.Enqueue('VMState="aborted"')
    Assert-False "aborted VM does not confirm firstboot" (
      Wait-CchousePowerOff -Name "claude-cc01" -Seconds 3 -FirstbootLog $completionLog
    )
  }

  if ($env:OS -eq "Windows_NT") {
    $ipFile = Join-Path $tmp "guest-ip"
    $oldSsh = $script:CchouseSystem32Ssh
    $script:CchouseSystem32Ssh = (Get-Process -Id $PID).Path
    $probe = (Get-Command Invoke-CchouseSshTrue).ScriptBlock
    try {
      $clock = [System.Diagnostics.Stopwatch]::StartNew()
      & {
        $attempts = [System.Collections.Generic.List[string]]::new()
        function Get-CchouseVBoxManage { return $fakeVBox }
        function Get-CchouseIdeController { return "IDE" }
        function Invoke-CchouseVBox {}
        function Start-CchouseVmDetached {}
        function Start-Sleep {}
        function Get-CchouseShowvminfoLines { return $vmLines }
        function Resolve-CchouseProbeDecision { return "probe" }
        function Invoke-CchouseSshTrue {
          param($Target, $SshOption, $TimeoutMs)
          Assert-True "boot passes a hard timeout" ($TimeoutMs -gt 0 -and $TimeoutMs -le 10000)
          if ($attempts.Count -eq 0) {
            $attempts.Add("timeout")
            return (& $probe -Target $Target -TimeoutMs 1500 -SshOption @(
              "-NoProfile", "-NonInteractive", "-Command", 'Start-Sleep -Seconds 30 #'
            ))
          }
          $attempts.Add("success")
          return (& $probe -Target $Target -SshOption @("-NoProfile", "-NonInteractive", "-Command", 'exit 0 #'))
        }
        Invoke-CchouseVBoxBoot -Name "claude-cc01" -KnownHostsPath $kh -IpFile $ipFile -SshPort 2301 -HostKeyLog $serial
        Assert-Equal "SSH retries a hung subprocess" ($attempts -join ",") "timeout,success"
      }
      Assert-True "hung probe was terminated" ($clock.Elapsed.TotalSeconds -lt 15)
      Assert-Equal "boot records IP after successful retry" ([System.IO.File]::ReadAllText($ipFile)) "127.0.0.1"
      Assert-Throws "native SSH host key mismatch stops probing" {
        Invoke-CchouseSshTrue -Target "claude@127.0.0.1" -SshOption @(
          "-NoProfile", "-NonInteractive", "-Command", '[Console]::Error.WriteLine("REMOTE HOST IDENTIFICATION HAS CHANGED"); exit 255 #'
        )
      } "host key verification failed"
      Assert-Equal "SSH keeps caller preference" $ErrorActionPreference "Stop"
    } finally {
      $script:CchouseSystem32Ssh = $oldSsh
    }
  }
} finally {
  if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Recurse -Force }
}

Assert-True "single IDE regex" $src.Contains('^storagecontrollername\d+="([^"]*IDE[^"]*)"')
Assert-False "second match does not clobber the capture" $src.Contains('$Matches[1] -match')

# --- preview.5: status / gui / stop / restore / close-action extradata ---
Assert-Equal "state running GUI" (Get-CchouseVBoxState -Lines @('VMState="running"', 'SessionName="GUI/Qt"')) "running"
Assert-Equal "state running headless" (Get-CchouseVBoxState -Lines @('VMState="running"', 'SessionName="headless"')) "running-headless"
Assert-Equal "state poweroff" (Get-CchouseVBoxState -Lines @('VMState="poweroff"')) "stopped"
Assert-Equal "state inaccessible" (Get-CchouseVBoxState -Lines @('VMState="inaccessible"')) "inaccessible"
Assert-Equal "state missing" (Get-CchouseVBoxState -Lines @()) "missing"

& {
  $calls = [System.Collections.Generic.List[string]]::new()
  $vmState = "stopped"
  $vmSession = ""
  $vmRegistered = $true
  $snapPresent = $true
  $interactive = $true

  function Invoke-CchouseVBox {
    param([string[]]$CcArgumentList, [string]$FailureMessage)
    $calls.Add($CcArgumentList -join " ")
    if ($CcArgumentList[0] -eq "controlvm" -and $CcArgumentList[2] -in @("acpipowerbutton", "poweroff")) {
      $script:testVmStateRef.Value = "poweroff"
    }
  }
  $script:testVmStateRef = [ref]$vmState
  function Invoke-CchouseVBoxOptional {
    param([string[]]$CcArgumentList)
    if ($CcArgumentList[0] -eq "showvminfo") {
      if (-not $vmRegistered) { return [pscustomobject]@{ ExitCode = 1; Output = @() } }
      $lines = @('VMState="{0}"' -f $script:testVmStateRef.Value)
      if ($vmSession) { $lines += ('SessionName="{0}"' -f $vmSession) }
      return [pscustomobject]@{ ExitCode = 0; Output = $lines }
    }
    if ($CcArgumentList[0] -eq "snapshot" -and $CcArgumentList[2] -eq "list") {
      if ($snapPresent) { return [pscustomobject]@{ ExitCode = 0; Output = @('SnapshotName="cchouse-clean"') } }
      return [pscustomobject]@{ ExitCode = 0; Output = @('This machine does not have any snapshots') }
    }
    return [pscustomobject]@{ ExitCode = 0; Output = @() }
  }
  function Wait-CchousePowerOff { param($Name, $Seconds) return $true }
  function Test-CchouseInteractiveSession { return $interactive }
  function Show-CchouseVBoxWindow { param($Name) return $true }

  # status
  $script:testVmStateRef.Value = "running"
  $vmSession = "headless"
  $stOut = @(Get-CchouseVBoxStatus -Name "claude-cc01" -SnapName "cchouse-clean")
  Assert-Equal "status headless VM_STATE" $stOut[0] "VM_STATE=running-headless"
  Assert-Equal "status snapshot present" $stOut[1] "SNAPSHOT_STATE=present"
  $snapPresent = $false
  $stOut2 = @(Get-CchouseVBoxStatus -Name "claude-cc01" -SnapName "cchouse-clean")
  Assert-Equal "status snapshot missing" $stOut2[1] "SNAPSHOT_STATE=missing"
  $vmRegistered = $false
  $stOut3 = @(Get-CchouseVBoxStatus -Name "claude-cc01" -SnapName "cchouse-clean")
  Assert-Equal "status missing VM" $stOut3[0] "VM_STATE=missing"
  $vmRegistered = $true
  $snapPresent = $true

  # close actions extradata
  $calls.Clear()
  Set-CchouseVBoxCloseActions -Name "claude-cc01"
  Assert-True "sets GUI/DefaultCloseAction=Shutdown" ($calls -contains "setextradata claude-cc01 GUI/DefaultCloseAction Shutdown")
  Assert-True "sets GUI/RestrictedCloseActions" ($calls -contains "setextradata claude-cc01 GUI/RestrictedCloseActions SaveState,PowerOff,PowerOffRestoringSnapshot,Detach")

  # stop: acpipowerbutton vs --force poweroff
  $calls.Clear()
  $script:testVmStateRef.Value = "running"
  $vmSession = "GUI/Qt"
  Stop-CchouseVBoxVM -Name "claude-cc01" -Force "no" -Timeout 30
  Assert-Equal "graceful stop uses acpipowerbutton" ($calls -join "|") "controlvm claude-cc01 acpipowerbutton"
  $calls.Clear()
  $script:testVmStateRef.Value = "running"
  Stop-CchouseVBoxVM -Name "claude-cc01" -Force "yes" -Timeout 30
  Assert-Equal "force stop uses poweroff" ($calls -join "|") "controlvm claude-cc01 poweroff"

  # gui: stopped -> startvm --type gui
  $calls.Clear()
  $script:testVmStateRef.Value = "poweroff"
  $vmSession = ""
  Invoke-CchouseVBoxGui -Name "claude-cc01"
  Assert-True "gui starts stopped VM with --type gui" ($calls -contains "startvm claude-cc01 --type gui")

  # gui: already GUI running -> no startvm
  $calls.Clear()
  $script:testVmStateRef.Value = "running"
  $vmSession = "GUI/Qt"
  Invoke-CchouseVBoxGui -Name "claude-cc01"
  Assert-False "gui does not duplicate already running GUI VM" ($calls -contains "startvm claude-cc01 --type gui")

  # gui: headless -> safe stop then startvm --type gui
  $calls.Clear()
  $script:testVmStateRef.Value = "running"
  $vmSession = "headless"
  Invoke-CchouseVBoxGui -Name "claude-cc01"
  $acpiIdx = $calls.IndexOf("controlvm claude-cc01 acpipowerbutton")
  $guiIdx = $calls.IndexOf("startvm claude-cc01 --type gui")
  Assert-True "gui stops headless before starting GUI" (($acpiIdx -ge 0) -and ($guiIdx -gt $acpiIdx))

  # gui: non-interactive SSH session -> sets extradata but does not launch GUI
  $calls.Clear()
  $interactive = $false
  $script:testVmStateRef.Value = "poweroff"
  Invoke-CchouseVBoxGui -Name "claude-cc01"
  Assert-False "noninteractive session skips startvm --type gui" ($calls -contains "startvm claude-cc01 --type gui")
  $interactive = $true

  # restore: stops if running, restores snapshot, reapplies close actions
  $calls.Clear()
  $script:testVmStateRef.Value = "running"
  $vmSession = "GUI/Qt"
  Restore-CchouseVBoxSnapshot -Name "claude-cc01" -SnapName "cchouse-clean" -Timeout 30
  $stopIdx = $calls.IndexOf("controlvm claude-cc01 acpipowerbutton")
  $resIdx = $calls.IndexOf("snapshot claude-cc01 restore cchouse-clean")
  Assert-True "restore stops VM then restores snapshot" (($stopIdx -ge 0) -and ($resIdx -gt $stopIdx))
  $snapPresent = $false
  Assert-Throws "restore fails if snapshot missing" {
    Restore-CchouseVBoxSnapshot -Name "claude-cc01" -SnapName "cchouse-clean"
  } "Snapshot not found"
}

# --- preview.5: bootstrap-windows.ps1 args, curl --http1.1 fallback, IWR fallback, cchouse.cmd ---
$bootstrapPath = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\scripts\bootstrap-windows.ps1"))
$bsTmp = Join-Path ([System.IO.Path]::GetTempPath()) ("cchouse-bs-test-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $bsTmp | Out-Null
try {
  # Release assets are emitted with UTF-8 BOM by tools/build-bootstrap-release.py; mirror that for PS 5.1 dot-sourcing.
  $bootstrapBom = Join-Path $bsTmp "bootstrap-windows-bom.ps1"
  [System.IO.File]::WriteAllText(
    $bootstrapBom,
    [System.IO.File]::ReadAllText($bootstrapPath, [System.Text.UTF8Encoding]::new($false)),
    [System.Text.UTF8Encoding]::new($true)
  )
  . $bootstrapBom -CchouseLoadFunctionsOnly

  $defaultUp = @(Get-CchouseBootstrapUpArgs)
  Assert-Equal "bootstrap default up args" ($defaultUp -join " ") "up --tag cc01 --vm-backend virtualbox"
  $customUp = @(Get-CchouseBootstrapUpArgs -Tag "cc02" -ImageMirror "https://mirror.example/img" -NpmMirror "https://mirror.example/npm")
  Assert-Equal "bootstrap custom up args" ($customUp -join " ") "up --tag cc02 --vm-backend virtualbox --image-mirror https://mirror.example/img --npm-mirror https://mirror.example/npm"
  $fakePy = Join-Path $bsTmp "python.exe"
  $fakeCode = Join-Path $bsTmp "code"
  $cmdPath = Write-CchouseCmdEntry -LocalAppData $bsTmp -PythonExe $fakePy -CodeDir $fakeCode
  Assert-True "cchouse.cmd created" (Test-Path -LiteralPath $cmdPath -PathType Leaf)
  $cmdText = [System.IO.File]::ReadAllText($cmdPath)
  $expectedCchousePy = Join-Path $fakeCode (Join-Path "scripts" "cchouse.py")
  Assert-True "cchouse.cmd invokes python -I scripts/cchouse.py" ($cmdText.Contains("`"$fakePy`" -I `"$expectedCchousePy`" %*"))

  # Test curl.exe retry + 92 -> --http1.1 + persistent .part
  & {
    $payload = "verified archive bytes"
    $tmpFile = Join-Path $bsTmp "ref.zip"
    [System.IO.File]::WriteAllText($tmpFile, $payload)
    $expectedHash = (Get-FileHash -LiteralPath $tmpFile -Algorithm SHA256).Hash
    $dest = Join-Path $bsTmp "downloads\pkg.zip"
    $curlCalls = [System.Collections.Generic.List[string]]::new()
    function Get-CchouseCurlPath { return "C:\Windows\System32\curl.exe" }
    function Invoke-CchouseRetrySleep { param([int]$Seconds) }
    function Invoke-CchouseCurlDownload {
      param([string]$CurlPath, [string[]]$ArgumentList)
      $curlCalls.Add($ArgumentList -join " ")
      if ($curlCalls.Count -eq 1) {
        [System.IO.File]::WriteAllText("$dest.part", "partial")
        return 92
      }
      [System.IO.File]::WriteAllText("$dest.part", $payload)
      return 0
    }
    Get-VerifiedArchive "https://example.invalid/pkg.zip" $expectedHash $dest
    Assert-Equal "curl retried once after exit 92" $curlCalls.Count 2
    Assert-True "first curl uses resume and speed-limit" ($curlCalls[0].Contains("-C -") -and $curlCalls[0].Contains("--speed-limit"))
    Assert-False "first curl does not force http1.1" ($curlCalls[0].Contains("--http1.1"))
    Assert-True "second curl switches to --http1.1" ($curlCalls[1].Contains("--http1.1"))
    Assert-True "dest atomically moved into place" (Test-Path -LiteralPath $dest -PathType Leaf)
  }

  # Test Invoke-WebRequest fallback with retry when curl.exe absent
  & {
    $payload = "iwr fallback bytes"
    $tmpFile = Join-Path $bsTmp "ref2.zip"
    [System.IO.File]::WriteAllText($tmpFile, $payload)
    $expectedHash = (Get-FileHash -LiteralPath $tmpFile -Algorithm SHA256).Hash
    $dest = Join-Path $bsTmp "downloads\pkg2.zip"
    $iwrCount = 0
    function Get-CchouseCurlPath { return $null }
    function Invoke-CchouseRetrySleep { param([int]$Seconds) }
    function Invoke-WebRequest {
      param([switch]$UseBasicParsing, [string]$Uri, [string]$OutFile)
      $script:iwrCountRef.Value++
      if ($script:iwrCountRef.Value -eq 1) { throw "transient error" }
      [System.IO.File]::WriteAllText($OutFile, $payload)
    }
    $script:iwrCountRef = [ref]$iwrCount
    Get-VerifiedArchive "https://example.invalid/pkg2.zip" $expectedHash $dest
    Assert-Equal "IWR fallback retried on transient error" $script:iwrCountRef.Value 2
    Assert-True "IWR fallback wrote verified dest" (Test-Path -LiteralPath $dest -PathType Leaf)
  }

  # Test shared folder helpers in vm-virtualbox.ps1 & bootstrap-windows.ps1
  $sfNames = @(Get-CchouseVBoxSharedFolderNames -Lines @(
    'SharedFolderNameMachineMapping1="cchouse-shared"',
    'SharedFolderPathMachineMapping1="C:\Users\Alice\cchouse-share\cc01"',
    'SharedFolderNameMachineMapping2="extra"'
  ))
  Assert-Equal "shared folder name count" $sfNames.Count 2
  Assert-Equal "first shared folder name" $sfNames[0] "cchouse-shared"
  Assert-Equal "second shared folder name" $sfNames[1] "extra"

  $rwArgs = @(Get-CchouseVBoxSharedFolderAddArgs -Name "claude-cc01" -HostPath "C:\Users\Alice\cchouse-share\cc01" -ReadOnly "no")
  Assert-True "rw has --automount" ($rwArgs -contains "--automount")
  Assert-True "rw has --auto-mount-point" ($rwArgs -contains "--auto-mount-point")
  Assert-True "rw mounts at /home/claude/shared" ($rwArgs -contains "/home/claude/shared")
  Assert-False "rw omits --readonly" ($rwArgs -contains "--readonly")

  $roArgs = @(Get-CchouseVBoxSharedFolderAddArgs -Name "claude-cc01" -HostPath "C:\Users\Alice\cchouse-share\cc01" -ReadOnly "yes")
  Assert-True "ro includes --readonly" ($roArgs -contains "--readonly")

  $bsShareArgs = @(Get-CchouseBootstrapUpArgs -Tag "cc01" -Share "C:\Users\Alice\my-share" -ShareReadOnly)
  Assert-True "bootstrap passes --share" ($bsShareArgs -contains "--share")
  Assert-True "bootstrap passes share path" ($bsShareArgs -contains "C:\Users\Alice\my-share")
  Assert-True "bootstrap passes --read-only" ($bsShareArgs -contains "--read-only")

  # Test Windows .lnk creation & removal via WScript.Shell on Windows_NT
  if ($env:OS -eq "Windows_NT") {
    $lnkPath = Join-Path $bsTmp "cchouse (cc99).lnk"
    $ws = New-Object -ComObject WScript.Shell
    $sc = $ws.CreateShortcut($lnkPath)
    $sc.TargetPath = "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe"
    $sc.Arguments = '-NoProfile -WindowStyle Hidden -Command "& ''C:\bin\cchouse.cmd'' open --tag ''cc99''"'
    $sc.WindowStyle = 7
    $sc.Save()
    Assert-True ".lnk created on Windows" (Test-Path -LiteralPath $lnkPath -PathType Leaf)
    $readSc = $ws.CreateShortcut($lnkPath)
    Assert-True ".lnk uses hidden powershell open --tag" ($readSc.Arguments.Contains("-WindowStyle Hidden") -and $readSc.Arguments.Contains("open --tag"))
    Remove-Item -LiteralPath $lnkPath -Force
    Assert-False ".lnk deleted cleanly" (Test-Path -LiteralPath $lnkPath)
  }
} finally {
  if (Test-Path -LiteralPath $bsTmp) { Remove-Item -LiteralPath $bsTmp -Recurse -Force }
}

if ($script:Failures.Count -gt 0) {
  foreach ($item in $script:Failures) { Write-Host "FAIL $item" }
  exit 1
}
Write-Host "OK vm-virtualbox function tests"
exit 0
