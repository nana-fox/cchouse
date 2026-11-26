# VirtualBox (Windows) guest lifecycle for scripts/cchouse.py up.
# Cc-prefixed parameters stay out of a dot-sourced caller's scope.
# NAT only. No add-on package, no host-only adapter, no bridged NIC.
# Host public keys come from the guest serial log.
# Do not print credentials.
param(
  [ValidateSet("", "install", "import", "firstboot", "boot", "snapshot", "exists", "delete")] [string]$CcOp = "",
  [string]$CcOva = "",
  [string]$CcVmName = "",
  [string]$CcVmDir = "",
  [ValidateSet("yes", "no")] [string]$CcReplaceExisting = "no",
  [string]$CcSeedIso = "",
  [string]$CcKey = "",
  [string]$CcKnownHosts = "",
  [string]$CcIpFile = "",
  [string]$CcSnapshot = "cchouse-clean",
  [int]$CcFirstbootSeconds = 56,
  [int]$CcSshPort = 0,
  [string]$CcInstaller = "",
  [string]$CcSha256 = "",
  [string]$CcHostKeyLog = ""
)

$script:CchouseHostKeyHelper = Join-Path $PSScriptRoot "windows-hostkey.ps1"
if (-not (Test-Path -LiteralPath $script:CchouseHostKeyHelper)) { throw "windows-hostkey.ps1 not found" }
. $script:CchouseHostKeyHelper

function Get-CchouseVBoxManage {
  if ($env:CCHOUSE_VBOXMANAGE -and (Test-Path -LiteralPath $env:CCHOUSE_VBOXMANAGE)) {
    return $env:CCHOUSE_VBOXMANAGE
  }
  foreach ($base in @(${env:ProgramFiles}, ${env:ProgramFiles(x86)})) {
    if (-not $base) { continue }
    $candidate = Join-Path $base "Oracle\VirtualBox\VBoxManage.exe"
    if (Test-Path -LiteralPath $candidate) { return $candidate }
  }
  return $null
}

function Invoke-CchouseVBox {
  param([string[]]$CcArgumentList, [string]$FailureMessage = "VBoxManage failed")
  $vbm = Get-CchouseVBoxManage
  if (-not $vbm) { throw "VBoxManage.exe not found (install VirtualBox or set CCHOUSE_VBOXMANAGE)" }
  Invoke-CchouseNativeChecked -FilePath $vbm -ArgumentList $CcArgumentList -FailureMessage $FailureMessage
}

function Invoke-CchouseVBoxOptional {
  param([string[]]$CcArgumentList)
  $vbm = Get-CchouseVBoxManage
  if (-not $vbm) { throw "VBoxManage.exe not found" }
  Invoke-CchouseNative -FilePath $vbm -ArgumentList $CcArgumentList
}

function Get-CchouseVirtualBoxInstallArgs {
  # Oracle VirtualBox manual, "Installing on Windows hosts" (public properties):
  #   VirtualBox.exe -msiparams NAME=VALUE [...]
  # One --msiparams switch, then each MSI property as its own argument.
  # The manual spells the switch -msiparams; the same switch with two dashes is what the NAT-only install used.
  # Windows PowerShell 5.1 Start-Process -ArgumentList can take this array because no value contains a space.
  # ADDLOCAL stays VBoxApplication only. A silent install still shows the Windows UAC prompt unless already elevated.
  "--silent"
  "--ignore-reboot"
  "--msiparams"
  "ADDLOCAL=VBoxApplication"
  "VBOX_INSTALLDESKTOPSHORTCUT=0"
  "VBOX_INSTALLQUICKLAUNCHSHORTCUT=0"
  "VBOX_REGISTERFILEEXTENSIONS=0"
  "VBOX_START=0"
}

function Install-CchouseVirtualBox {
  param([string]$Installer, [string]$ExpectedSha)
  if ($ExpectedSha -notmatch '^[0-9a-fA-F]{64}$') {
    throw "CcSha256 is required"
  }
  $existing = Get-CchouseVBoxManage
  if ($existing) {
    Write-Host "VBOX_ALREADY_INSTALLED=$existing"
    return
  }
  if (-not $Installer -or -not (Test-Path -LiteralPath $Installer -PathType Leaf)) {
    throw "VirtualBox installer not found"
  }
  $hash = (Get-FileHash -LiteralPath $Installer -Algorithm SHA256).Hash.ToLower()
  if ($hash -ne $ExpectedSha.ToLower()) { throw "VirtualBox installer SHA-256 mismatch" }
  # NAT-only feature set. A full install is not a fallback: it would add bridge filters.
  $argList = @(Get-CchouseVirtualBoxInstallArgs)
  $proc = Start-Process -FilePath $Installer -ArgumentList $argList -Verb RunAs -Wait -PassThru -ErrorAction Stop
  # 3010 is ERROR_SUCCESS_REBOOT_REQUIRED. --ignore-reboot still installed the NAT-only product.
  if ($proc.ExitCode -ne 0 -and $proc.ExitCode -ne 3010) {
    throw "VirtualBox NAT-only install failed (exit $($proc.ExitCode))"
  }
  if (-not (Get-CchouseVBoxManage)) { throw "VBoxManage.exe missing after install" }
  Write-Host "VBOX_INSTALLED=nat-only"
}

function Test-CchouseVmRegistered {
  param([string]$Name)
  $vbm = Get-CchouseVBoxManage
  if (-not $vbm -or -not $Name) { return $false }
  $result = Invoke-CchouseVBoxOptional -CcArgumentList @("showvminfo", $Name, "--machinereadable")
  return ($result.ExitCode -eq 0)
}

function Get-CchouseIdeControllerName {
  param([string[]]$Lines)
  foreach ($line in @($Lines)) {
    if ($line -match '^storagecontrollername\d+="([^"]*IDE[^"]*)"') {
      return $Matches[1]
    }
  }
  return $null
}

function Get-CchouseIdeController {
  param([string]$Name)
  $info = @(Get-CchouseShowvminfoLines -Name $Name)
  $controller = Get-CchouseIdeControllerName -Lines $info
  if ([string]::IsNullOrEmpty($controller)) {
    throw "No IDE storage controller on $Name"
  }
  return $controller
}

function Get-CchouseVmNameFromLines {
  param([string[]]$Lines)
  foreach ($line in @($Lines)) {
    if ($line -match '^name="([^"]*)"') { return $Matches[1] }
  }
  return $null
}

function Test-CchouseForwardRule {
  param([string[]]$Lines, [int]$Port)
  $want = "ssh,tcp,127.0.0.1,$Port,,22"
  foreach ($line in @($Lines)) {
    if ($line -match '^Forwarding\(\d+\)="([^"]*)"') {
      if ($Matches[1] -eq $want) { return $true }
    }
  }
  return $false
}

function Get-CchouseVBoxLogPid {
  param([string]$Name)
  $log = Invoke-CchouseVBoxOptional -CcArgumentList @("showvminfo", $Name, "--log=0")
  if ($log.ExitCode -ne 0) { return $null }
  foreach ($line in ($log.Output | Select-Object -First 32)) {
    if ($line -match '^(?:\d{2}:\d{2}:\d{2}\.\d+\s+)?Process ID:\s*([1-9][0-9]*)\s*$') {
      return [int]$Matches[1]
    }
  }
  return $null
}

function Resolve-CchouseProbeDecision {
  param(
    [string]$Name,
    [string[]]$Lines,
    [int]$Port,
    [string]$VBoxManage
  )
  if ((Get-CchouseVmNameFromLines -Lines $Lines) -cne $Name) { return "reject" }
  if (-not (Test-CchouseForwardRule -Lines $Lines -Port $Port)) { return "reject" }
  $listenerId = Get-CchouseLoopbackListenerId -Port $Port
  if ($null -eq $listenerId) { return "wait" }
  if ('VMState="running"' -notin $Lines) { return "wait" }

  $vmPid = Get-CchouseVBoxLogPid -Name $Name
  if ($null -ne $vmPid) {
    if ($listenerId -eq $vmPid) { return "probe" }
    return "reject"
  }

  # If the VM log cannot identify the PID, accept only a VM executable
  # from the same install directory as VBoxManage.
  $processPath = (Get-Process -Id $listenerId -ErrorAction SilentlyContinue).Path
  $installDir = Split-Path -Parent ([System.IO.Path]::GetFullPath($VBoxManage))
  if ($processPath -in @((Join-Path $installDir "VBoxHeadless.exe"), (Join-Path $installDir "VirtualBoxVM.exe"))) {
    return "probe"
  }
  return "reject"
}

function Assert-CchouseSshPort {
  param([int]$Port)
  if ($Port -lt 1 -or $Port -gt 65535) { throw "CcSshPort is required" }
}

function Get-CchouseShowvminfoLines {
  param([string]$Name)
  Invoke-CchouseVBox -CcArgumentList @("showvminfo", $Name, "--machinereadable")
}

function Get-CchouseLoopbackListenerId {
  param([int]$Port)
  try {
    $conns = @(Get-NetTCPConnection -LocalAddress "127.0.0.1" -LocalPort $Port -State Listen -ErrorAction Stop)
  } catch {
    if ($_.FullyQualifiedErrorId -like 'CmdletizationQuery_NotFound*') { return $null }
    throw
  }
  if ($conns.Count -eq 0) { return $null }
  if ($conns.Count -ne 1 -or [int]$conns[0].OwningProcess -le 0) {
    throw "SSH forward 127.0.0.1:${Port} has no unique listener PID"
  }
  return [int]$conns[0].OwningProcess
}

function Start-CchouseVmDetached {
  param([string]$Name)
  $vbm = Get-CchouseVBoxManage
  $cmd = "`"$vbm`" startvm `"$Name`" --type headless"
  $res = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd }
  if ($res.ReturnValue -ne 0) {
    throw "Win32_Process.Create failed for startvm (rc=$($res.ReturnValue))"
  }
}

function Wait-CchousePowerOff {
  param([string]$Name, [int]$Seconds)
  for ($i = 0; $i -lt $Seconds; $i++) {
    $info = Invoke-CchouseVBoxOptional -CcArgumentList @("showvminfo", $Name, "--machinereadable")
    if ($info.ExitCode -eq 0) {
      if ('VMState="poweroff"' -in $info.Output -or 'VMState="aborted"' -in $info.Output) { return $true }
    }
    Start-Sleep -Seconds 1
  }
  return $false
}

function Resolve-CchouseFirstbootStop {
  param([bool]$AfterAcpi, [bool]$AfterHard)
  if ($AfterAcpi) { return "acpi" }
  if ($AfterHard) { return "hard" }
  throw "firstboot shutdown timed out"
}

function Enable-CchouseVBoxSerialLog {
  param([string]$Name, [string]$HostKeyLog)
  if ([string]::IsNullOrWhiteSpace($HostKeyLog)) { return }
  Initialize-CchouseHostKeyLog -Path $HostKeyLog
  # COM1 (0x3F8 IRQ 4) is /dev/ttyS0 in the guest. File mode needs the VM powered off.
  Invoke-CchouseVBox -CcArgumentList @(
    "modifyvm", $Name,
    "--uart1", "0x3F8", "4",
    "--uartmode1", "file", $HostKeyLog
  )
}

function Import-CchouseVBoxOva {
  param([string]$OvaPath, [string]$Name, [string]$DestDir, [int]$SshPort, [string]$HostKeyLog, [string]$ReplaceExisting)
  if (-not (Test-Path -LiteralPath $OvaPath)) { throw "OVA not found" }
  Assert-CchouseSshPort -Port $SshPort
  New-Item -ItemType Directory -Force -Path $DestDir | Out-Null
  $vmUuid = Get-CchouseRegisteredVBoxUuid -Name $Name
  if ($vmUuid) {
    if ($ReplaceExisting -ne "yes") { throw "Existing VirtualBox VM is not owned by this install: $Name" }
    Remove-CchouseVBoxVM -Name $Name -VmDir $DestDir
  }
  Invoke-CchouseVBox -CcArgumentList @("import", $OvaPath, "--vsys", "0", "--vmname", $Name, "--basefolder", $DestDir)
  $rule = "ssh,tcp,127.0.0.1,$SshPort,,22"
  Invoke-CchouseVBox -CcArgumentList @(
    "modifyvm", $Name,
    "--firmware", "bios",
    "--cpus", "2",
    "--memory", "2048",
    "--graphicscontroller", "vmsvga",
    "--vram", "128",
    "--nic1", "nat",
    "--nictype1", "82540EM",
    "--cableconnected1", "off",
    "--natpf1", $rule,
    "--clipboard-mode", "bidirectional",
    "--drag-and-drop", "disabled",
    "--audio-enabled", "off",
    "--usb-ohci", "off"
  )
  Enable-CchouseVBoxSerialLog -Name $Name -HostKeyLog $HostKeyLog
  Write-Host "VBOX_IMPORTED=$Name"
}

function Invoke-CchouseVBoxFirstboot {
  param([string]$Name, [string]$SeedIso, [int]$Seconds, [string]$HostKeyLog)
  if (-not (Test-Path -LiteralPath $SeedIso)) { throw "seed.iso not found" }
  $ide = Get-CchouseIdeController $Name
  Invoke-CchouseVBox -CcArgumentList @(
    "storageattach", $Name,
    "--storagectl", $ide,
    "--port", "1",
    "--device", "0",
    "--type", "dvddrive",
    "--medium", $SeedIso
  )
  Enable-CchouseVBoxSerialLog -Name $Name -HostKeyLog $HostKeyLog
  Write-Host "Starting offline firstboot (headless, ${Seconds}s)..."
  Start-CchouseVmDetached $Name
  Start-Sleep -Seconds $Seconds
  Invoke-CchouseVBoxOptional -CcArgumentList @("controlvm", $Name, "acpipowerbutton") | Out-Null
  $afterAcpi = Wait-CchousePowerOff $Name 40
  $afterHard = $false
  if (-not $afterAcpi) {
    Invoke-CchouseVBoxOptional -CcArgumentList @("controlvm", $Name, "poweroff") | Out-Null
    $afterHard = Wait-CchousePowerOff $Name 20
  }
  $how = Resolve-CchouseFirstbootStop -AfterAcpi:$afterAcpi -AfterHard:$afterHard
  if ($how -eq "hard") {
    Write-Host "HARD_POWEROFF=1"
  }
  Write-Host "FIRSTBOOT_STOPPED=OK"
}

function Invoke-CchouseVBoxBoot {
  param(
    [string]$Name,
    [string]$SeedIso,
    [string]$KeyPath,
    [string]$KnownHostsPath,
    [string]$IpFile,
    [int]$SshPort,
    [string]$HostKeyLog
  )
  Assert-CchouseSshPort -Port $SshPort
  $vbm = Get-CchouseVBoxManage
  if (-not $vbm) { throw "VBoxManage.exe not found" }
  $ide = Get-CchouseIdeController $Name
  Invoke-CchouseVBox -CcArgumentList @(
    "storageattach", $Name, "--storagectl", $ide, "--port", "1", "--device", "0", "--type", "dvddrive", "--medium", "emptydrive"
  ) -FailureMessage "detach seed ISO failed"
  if ($SeedIso) {
    Invoke-CchouseVBoxOptional -CcArgumentList @("closemedium", "dvd", $SeedIso) | Out-Null
    if (Test-Path -LiteralPath $SeedIso) { Remove-Item -LiteralPath $SeedIso -Force }
    if (Test-Path -LiteralPath $SeedIso) { throw "seed.iso still present" }
  }
  Invoke-CchouseVBox -CcArgumentList @("modifyvm", $Name, "--cableconnected1", "on") -FailureMessage "enable NIC failed"
  # Read the serial log while the VM is off. A running VM may lock the file.
  Enable-CchouseVBoxSerialLog -Name $Name -HostKeyLog $HostKeyLog
  $serialText = Read-CchouseSerialText -Path $HostKeyLog
  $trust = Resolve-CchouseHostKeyTrust -SerialText $serialText -HostName "127.0.0.1" -Port $SshPort
  Write-CchouseHostKeyTrustMark -SerialLogPath $HostKeyLog -Pinned:([bool]$trust.Pinned)
  if ($trust.Pinned) {
    Write-CchouseKnownHostsFile -Path $KnownHostsPath -Line $trust.KnownHostsLine
  } else {
    Write-Host "WARN  未经串口核对主机密钥"
  }
  Write-Host "Detached seed.iso; starting networked VM (detached)..."
  Start-CchouseVmDetached $Name

  if (-not (Test-Path -LiteralPath $script:CchouseSystem32Ssh)) { throw "System32 OpenSSH ssh.exe not found" }
  Fix-KeyAcl $KeyPath
  $ok = $false
  $startedAt = Get-Date
  while (-not (Test-CchouseSshDeadlineReached -StartedAt $startedAt -LimitSeconds $script:CchouseSshWaitSeconds)) {
    $remaining = [int][Math]::Floor($script:CchouseSshWaitSeconds - ((Get-Date) - $startedAt).TotalSeconds)
    if ($remaining -lt 1) { break }
    $sleepFor = [int][Math]::Min(3, $remaining)
    if ($sleepFor -lt 1) { break }
    Start-Sleep -Seconds $sleepFor
    $info = @(Get-CchouseShowvminfoLines -Name $Name)
    $decision = Resolve-CchouseProbeDecision -Name $Name -Lines $info -Port $SshPort -VBoxManage $vbm
    if ($decision -eq "reject") {
      throw "SSH forward 127.0.0.1:${SshPort} is not owned by $Name"
    }
    if ($decision -ne "probe") { continue }
    $pinned = [bool]$trust.Pinned
    $timeout = $remaining
    if ($timeout -gt 5) { $timeout = 5 }
    $verify = @(Get-CchouseSshVerifyOptions -KnownHostsPath $KnownHostsPath -Pinned:$pinned -ConnectTimeout $timeout)
    $sshOpts = @("-F", "none", "-p", "$SshPort", "-i", $KeyPath) + $verify
    if (Invoke-CchouseSshTrue -Target "claude@127.0.0.1" -SshOption $sshOpts) {
      $ok = $true
      break
    }
  }
  if (-not $ok) { throw "Timed out waiting for SSH on 127.0.0.1:$SshPort" }
  if (-not $trust.Pinned) {
    $recorded = ""
    if (Test-Path -LiteralPath $KnownHostsPath) {
      $recorded = [System.IO.File]::ReadAllText($KnownHostsPath).Trim()
    }
    if (-not $recorded) { throw "ssh did not record a host key" }
  }
  [System.IO.File]::WriteAllText($IpFile, "127.0.0.1", [System.Text.UTF8Encoding]::new($false))
  Write-Host "GUEST_IP=127.0.0.1"
}

function Get-CchouseRegisteredVBoxUuid {
  param([string]$Name)
  $pattern = '^"{0}"\s+\{{([0-9a-f]{{8}}(?:-[0-9a-f]{{4}}){{3}}-[0-9a-f]{{12}})\}}$' -f [regex]::Escape($Name)
  $vmUuid = $null
  foreach ($line in @(Invoke-CchouseVBox -CcArgumentList @("list", "vms") -FailureMessage "VirtualBox inventory failed")) {
    if ($line -match $pattern) {
      if ($vmUuid) { throw "Multiple registered VirtualBox VMs match $Name" }
      $vmUuid = $Matches[1]
    }
  }
  return $vmUuid
}

function Remove-CchouseVBoxVM {
  param([string]$Name, [string]$VmDir)
  if ($Name -cnotmatch '^claude-[a-z][a-z0-9_-]{1,15}$') { throw "VM name is not an exact instance name" }
  if (-not $VmDir) { throw "VM directory is required" }
  $vmUuid = Get-CchouseRegisteredVBoxUuid -Name $Name
  if (-not $vmUuid) { return }

  $encoding = [Console]::OutputEncoding
  try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $info = @(Get-CchouseShowvminfoLines -Name $vmUuid)
  } finally {
    [Console]::OutputEncoding = $encoding
  }
  $reported = Get-CchouseVmNameFromLines -Lines $info
  if ([string]::IsNullOrEmpty($reported) -or ($reported -cne $Name)) {
    throw "registered VM name does not match $Name"
  }
  $cfg = $null
  foreach ($line in $info) {
    if ($line -cmatch '^CfgFile="([^"]+)"$') {
      $cfg = $Matches[1].Replace('\\', '\')
      break
    }
  }
  $expected = Join-Path (Join-Path $VmDir $Name) "$Name.vbox"
  if (-not $cfg -or [System.IO.Path]::GetFullPath($cfg) -ine [System.IO.Path]::GetFullPath($expected)) {
    throw "registered VM config is outside the recorded instance: $Name"
  }

  $stopped = @('VMState="poweroff"', 'VMState="aborted"', 'VMState="saved"', 'VMState="aborted-saved"')
  if (-not ($info | Where-Object { $_ -in $stopped })) {
    Invoke-CchouseVBox -CcArgumentList @("controlvm", $vmUuid, "poweroff") -FailureMessage "poweroff failed for $Name"
    if (-not (Wait-CchousePowerOff $vmUuid 40)) { throw "poweroff timed out for $Name" }
  }
  Invoke-CchouseVBox -CcArgumentList @("unregistervm", $vmUuid, "--delete") -FailureMessage "unregistervm failed for $Name"
  if (Get-CchouseRegisteredVBoxUuid -Name $Name) { throw "VM $Name is still registered" }
}

function New-CchouseVBoxSnapshot {
  param([string]$Name, [string]$SnapName)
  Invoke-CchouseVBoxOptional -CcArgumentList @("snapshot", $Name, "delete", $SnapName) | Out-Null
  Invoke-CchouseVBox -CcArgumentList @("snapshot", $Name, "take", $SnapName, "--description", "cchouse clean") `
    -FailureMessage "VBoxManage snapshot take failed"
  Write-Host "SNAPSHOT_OK=$SnapName"
}

if ($CcOp) {
  $ErrorActionPreference = "Stop"
  switch ($CcOp) {
    "exists" {
      if (Test-CchouseVmRegistered $CcVmName) { exit 0 }
      exit 1
    }
    "install" { Install-CchouseVirtualBox $CcInstaller $CcSha256 }
    "import" { Import-CchouseVBoxOva $CcOva $CcVmName $CcVmDir $CcSshPort $CcHostKeyLog $CcReplaceExisting }
    "firstboot" { Invoke-CchouseVBoxFirstboot $CcVmName $CcSeedIso $CcFirstbootSeconds $CcHostKeyLog }
    "boot" { Invoke-CchouseVBoxBoot $CcVmName $CcSeedIso $CcKey $CcKnownHosts $CcIpFile $CcSshPort $CcHostKeyLog }
    "snapshot" { New-CchouseVBoxSnapshot $CcVmName $CcSnapshot }
    "delete" { Remove-CchouseVBoxVM -Name $CcVmName -VmDir $CcVmDir }
  }
  exit 0
}
