# VirtualBox (Windows) guest lifecycle for scripts/cchouse.py up.
# Cc-prefixed parameters stay out of a dot-sourced caller's scope.
# NAT only. No add-on package, no host-only adapter, no bridged NIC.
# Host public keys come from the guest serial log.
# Do not print credentials.
param(
  [ValidateSet("", "install", "import", "firstboot", "boot", "snapshot", "exists", "delete", "gui", "stop", "restore", "status", "sync-share")] [string]$CcOp = "",
  [string]$CcOva = "",
  [string]$CcVmName = "",
  [string]$CcVmDir = "",
  [ValidateSet("yes", "no")] [string]$CcReplaceExisting = "no",
  [string]$CcSeedIso = "",
  [string]$CcKey = "",
  [string]$CcKnownHosts = "",
  [string]$CcIpFile = "",
  [string]$CcSnapshot = "cchouse-clean",
  [int]$CcFirstbootSeconds = 180,
  [int]$CcSshPort = 0,
  [string]$CcInstaller = "",
  [string]$CcSha256 = "",
  [string]$CcHostKeyLog = "",
  [ValidateSet("yes", "no")] [string]$CcForce = "no",
  [int]$CcTimeout = 60,
  [ValidateSet("yes", "no", "keep")] [string]$CcShareEnabled = "keep",
  [string]$CcSharePath = "",
  [ValidateSet("yes", "no")] [string]$CcShareReadOnly = "no"
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
  if ([string]::IsNullOrWhiteSpace($Name)) { throw "CcVmName is required" }
  # VBoxManage startvm --type headless delegates VM creation to VBoxSVC and exits
  # once LaunchVMProcess finishes (~3-4s). Running it synchronously avoids racing
  # with the next showvminfo poll (VBOX_E_INVALID_OBJECT_STATE) and surfaces
  # launch errors immediately instead of waiting for the full firstboot timeout.
  $maxAttempts = 5
  for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
    $res = Invoke-CchouseVBoxOptional -CcArgumentList @("startvm", $Name, "--type", "headless")
    if ($res.ExitCode -eq 0) {
      return
    }
    $parts = @()
    if (-not [string]::IsNullOrWhiteSpace($res.Stderr)) { $parts += $res.Stderr.Trim() }
    $outText = ($res.Output -join "`n").Trim()
    if (-not [string]::IsNullOrWhiteSpace($outText)) { $parts += $outText }
    $detail = $parts -join "`n"
    $isTransientLock = ($detail -match 'VBOX_E_INVALID_OBJECT_STATE|already locked by a session')
    if ($isTransientLock -and $attempt -lt $maxAttempts) {
      Start-Sleep -Seconds 1
      continue
    }
    if ($detail) {
      throw "VBoxManage startvm --type headless failed for $Name (rc=$($res.ExitCode)): $detail"
    }
    throw "VBoxManage startvm --type headless failed for $Name (rc=$($res.ExitCode))"
  }
}

function Wait-CchousePowerOff {
  param([string]$Name, [int]$Seconds, [string]$FirstbootLog)
  $clock = [System.Diagnostics.Stopwatch]::StartNew()
  $started = $false
  while ($clock.Elapsed.TotalSeconds -lt $Seconds) {
    $info = Invoke-CchouseVBoxOptional -CcArgumentList @("showvminfo", $Name, "--machinereadable")
    if ($info.ExitCode -eq 0) {
      if ('VMState="aborted"' -in $info.Output) { return (-not $FirstbootLog) }
      if ('VMState="poweroff"' -in $info.Output) {
        if (-not $FirstbootLog) { return $true }
        $serialText = Read-CchouseSerialText -Path $FirstbootLog
        if ($serialText -cmatch '(?m)^CCHOUSE_FIRSTBOOT_DONE\r?$') { return $true }
        # The detached launcher may not have started the VM yet.
        if ($started) { throw "firstboot stopped without confirming offline provisioning" }
      } elseif ('VMState="running"' -in $info.Output) {
        $started = $true
      }
    }
    Start-Sleep -Seconds 1
  }
  return $false
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
  Set-CchouseVBoxCloseActions -Name $Name
  Write-Host "VBOX_IMPORTED=$Name"
}

function Invoke-CchouseVBoxFirstboot {
  param([string]$Name, [string]$SeedIso, [int]$Seconds, [string]$HostKeyLog)
  if (-not (Test-Path -LiteralPath $SeedIso)) { throw "seed.iso not found" }
  if (-not $HostKeyLog) { throw "CcHostKeyLog is required for firstboot" }
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
  [System.IO.File]::WriteAllText($HostKeyLog, "")
  Write-Host "Starting offline firstboot (headless, up to ${Seconds}s)..."
  Start-CchouseVmDetached $Name
  if (-not (Wait-CchousePowerOff -Name $Name -Seconds $Seconds -FirstbootLog $HostKeyLog)) {
    Invoke-CchouseVBoxOptional -CcArgumentList @("controlvm", $Name, "poweroff") | Out-Null
    if (-not (Wait-CchousePowerOff -Name $Name -Seconds 20)) {
      throw "firstboot shutdown timed out; could not stop the offline VM"
    }
    throw "firstboot did not shut down cleanly within ${Seconds}s; provisioning was not confirmed"
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
    [string]$HostKeyLog,
    [ValidateSet("yes", "no", "keep")] [string]$ShareEnabled = "keep",
    [string]$SharePath = "",
    [ValidateSet("yes", "no")] [string]$ShareReadOnly = "no"
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
  Sync-CchouseVBoxSharedFolder -Name $Name -Enabled $ShareEnabled -HostPath $SharePath -ReadOnly $ShareReadOnly
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
  $clock = [System.Diagnostics.Stopwatch]::StartNew()
  $budgetMs = $script:CchouseSshWaitSeconds * 1000
  while ($clock.ElapsedMilliseconds -lt $budgetMs) {
    $remainingMs = $budgetMs - $clock.ElapsedMilliseconds
    if ($remainingMs -le 0) { break }
    Start-Sleep -Milliseconds ([int][Math]::Min(3000, $remainingMs))
    if ($clock.ElapsedMilliseconds -ge $budgetMs) { break }
    $info = @(Get-CchouseShowvminfoLines -Name $Name)
    $decision = Resolve-CchouseProbeDecision -Name $Name -Lines $info -Port $SshPort -VBoxManage $vbm
    if ($decision -eq "reject") {
      throw "SSH forward 127.0.0.1:${SshPort} is not owned by $Name"
    }
    if ($decision -ne "probe") { continue }
    $pinned = [bool]$trust.Pinned
    $timeoutMs = [int][Math]::Min(10000, ($budgetMs - $clock.ElapsedMilliseconds))
    if ($timeoutMs -le 0) { break }
    $verify = @(Get-CchouseSshVerifyOptions -KnownHostsPath $KnownHostsPath -Pinned:$pinned)
    $sshOpts = @("-F", "none", "-p", "$SshPort", "-i", $KeyPath) + $verify
    if (Invoke-CchouseSshTrue -Target "claude@127.0.0.1" -SshOption $sshOpts -TimeoutMs $timeoutMs) {
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

function ConvertTo-CchouseVBoxFullPath {
  param([string]$Path)
  if ([System.IO.Path]::DirectorySeparatorChar -eq '\') {
    $Path = $Path.Replace('/', '\')
    if ($Path.StartsWith('\\?\UNC\', [System.StringComparison]::OrdinalIgnoreCase)) {
      $Path = '\\' + $Path.Substring(8)
    } elseif ($Path -match '^\\\\\?\\[a-z]:\\') {
      $Path = $Path.Substring(4)
    }
  }
  return [System.IO.Path]::GetFullPath($Path)
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
  $expected = [System.IO.Path]::Combine((ConvertTo-CchouseVBoxFullPath $VmDir), $Name, "$Name.vbox")
  if (-not $cfg -or (ConvertTo-CchouseVBoxFullPath $cfg) -ine $expected) {
    throw "registered VM config is outside the recorded instance: $Name"
  }

  $stopped = @('VMState="poweroff"', 'VMState="aborted"', 'VMState="saved"', 'VMState="aborted-saved"')
  if (-not ($info | Where-Object { $_ -in $stopped })) {
    Invoke-CchouseVBox -CcArgumentList @("controlvm", $vmUuid, "poweroff") -FailureMessage "poweroff failed for $Name"
    if (-not (Wait-CchousePowerOff $vmUuid 40)) { throw "poweroff timed out for $Name" }
  }
  # A GUI VirtualBoxVM process can keep the session lock briefly after VMState="poweroff".
  $lockClock = [System.Diagnostics.Stopwatch]::StartNew()
  while ($lockClock.Elapsed.TotalSeconds -lt 15) {
    $cur = Invoke-CchouseVBoxOptional -CcArgumentList @("showvminfo", $vmUuid, "--machinereadable")
    if ($cur.ExitCode -ne 0 -or ('SessionState="Locked"' -notin $cur.Output -and 'SessionState="Spawning"' -notin $cur.Output -and 'SessionState="Unlocking"' -notin $cur.Output)) {
      break
    }
    Start-Sleep -Milliseconds 500
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

function Set-CchouseVBoxCloseActions {
  param([string]$Name)
  if ([string]::IsNullOrWhiteSpace($Name)) { throw "CcVmName is required" }
  # Verified on VirtualBox 7.2.22 (UICommon.dll + GTR GUI test):
  # Clicking the window close button triggers ACPI Shutdown directly without the 4-way dialog.
  Invoke-CchouseVBox -CcArgumentList @("setextradata", $Name, "GUI/DefaultCloseAction", "Shutdown") `
    -FailureMessage "VBoxManage setextradata GUI/DefaultCloseAction failed" | Out-Null
  Invoke-CchouseVBox -CcArgumentList @(
    "setextradata", $Name, "GUI/RestrictedCloseActions", "SaveState,PowerOff,PowerOffRestoringSnapshot,Detach"
  ) -FailureMessage "VBoxManage setextradata GUI/RestrictedCloseActions failed" | Out-Null
}

function Get-CchouseVBoxSharedFolderNames {
  param([string[]]$Lines)
  $names = @()
  foreach ($line in @($Lines)) {
    if ($line -match '^SharedFolderNameMachineMapping\d+="([^"]*)"') {
      $names += $Matches[1]
    }
  }
  return $names
}

function Get-CchouseVBoxSharedFolderAddArgs {
  param(
    [string]$Name,
    [string]$HostPath,
    [ValidateSet("yes", "no")] [string]$ReadOnly = "no",
    [string]$ShareName = "cchouse-shared",
    [string]$AutoMountPoint = "/home/claude/shared"
  )
  if ([string]::IsNullOrWhiteSpace($Name)) { throw "CcVmName is required" }
  if ([string]::IsNullOrWhiteSpace($HostPath)) { throw "CcSharePath is required" }
  $argsList = @(
    "sharedfolder", "add", $Name,
    "--name", $ShareName,
    "--hostpath", $HostPath,
    "--automount",
    "--auto-mount-point", $AutoMountPoint
  )
  if ($ReadOnly -eq "yes") {
    $argsList += "--readonly"
  }
  return $argsList
}

function Sync-CchouseVBoxSharedFolder {
  param(
    [string]$Name,
    [ValidateSet("yes", "no", "keep")] [string]$Enabled = "keep",
    [string]$HostPath = "",
    [ValidateSet("yes", "no")] [string]$ReadOnly = "no"
  )
  if ($Enabled -eq "keep") { return }
  if ([string]::IsNullOrWhiteSpace($Name)) { throw "CcVmName is required" }
  $info = @(Get-CchouseShowvminfoLines -Name $Name)
  $state = Get-CchouseVBoxState -Lines $info
  if ($state -in @("running", "running-headless")) {
    throw "VirtualBox VM $Name is running; shared folder changes require poweroff"
  }
  foreach ($existingName in @(Get-CchouseVBoxSharedFolderNames -Lines $info)) {
    Invoke-CchouseVBox -CcArgumentList @("sharedfolder", "remove", $Name, "--name", $existingName) `
      -FailureMessage "VBoxManage sharedfolder remove failed" | Out-Null
  }
  if ($Enabled -eq "yes") {
    $addArgs = @(Get-CchouseVBoxSharedFolderAddArgs -Name $Name -HostPath $HostPath -ReadOnly $ReadOnly)
    Invoke-CchouseVBox -CcArgumentList $addArgs `
      -FailureMessage "VBoxManage sharedfolder add failed" | Out-Null
  }
}

function Get-CchouseVBoxState {
  param([string[]]$Lines)
  $vmState = $null
  $sessionName = $null
  foreach ($line in @($Lines)) {
    if ($line -match '^VMState="([^"]*)"') {
      $vmState = $Matches[1].ToLowerInvariant()
    } elseif ($line -match '^SessionName="([^"]*)"') {
      $sessionName = $Matches[1].ToLowerInvariant()
    }
  }
  if (-not $vmState) { return "missing" }
  if ($vmState -in @("running", "starting", "paused", "stopping")) {
    if ($sessionName -eq "headless") { return "running-headless" }
    return "running"
  }
  return "stopped"
}

function Test-CchouseVBoxSnapshotExists {
  param([string]$Name, [string]$SnapName)
  if ([string]::IsNullOrWhiteSpace($Name) -or [string]::IsNullOrWhiteSpace($SnapName)) { return $false }
  $res = Invoke-CchouseVBoxOptional -CcArgumentList @("snapshot", $Name, "list", "--machinereadable")
  if ($res.ExitCode -ne 0) { return $false }
  foreach ($line in @($res.Output)) {
    if ($line -cmatch '^SnapshotName(?:-\d+)*="(.+)"$' -and ($Matches[1] -ceq $SnapName)) {
      return $true
    }
  }
  return $false
}

function Get-CchouseVBoxStatus {
  param([string]$Name, [string]$SnapName = "cchouse-clean")
  if ([string]::IsNullOrWhiteSpace($Name)) { throw "CcVmName is required" }
  $info = Invoke-CchouseVBoxOptional -CcArgumentList @("showvminfo", $Name, "--machinereadable")
  if ($info.ExitCode -ne 0) {
    Write-Output "VM_STATE=missing"
    Write-Output "SNAPSHOT_STATE=missing"
    return
  }
  $state = Get-CchouseVBoxState -Lines $info.Output
  $snapState = "missing"
  if ($SnapName -and (Test-CchouseVBoxSnapshotExists -Name $Name -SnapName $SnapName)) {
    $snapState = "present"
  }
  Write-Output "VM_STATE=$state"
  Write-Output "SNAPSHOT_STATE=$snapState"
}

function Test-CchouseInteractiveSession {
  if ($env:SSH_CONNECTION -or $env:SSH_CLIENT -or $env:SSH_TTY) { return $false }
  if (-not [System.Environment]::UserInteractive) { return $false }
  try {
    if ([System.Diagnostics.Process]::GetCurrentProcess().SessionId -eq 0) { return $false }
  } catch {
    return $false
  }
  return $true
}

function Show-CchouseVBoxWindow {
  param([string]$Name)
  if ($env:OS -ne "Windows_NT" -or [string]::IsNullOrWhiteSpace($Name)) { return $false }
  try {
    $proc = Get-Process -Name "VirtualBoxVM" -ErrorAction SilentlyContinue |
      Where-Object { $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle -like "$Name*" } |
      Select-Object -First 1
    if (-not $proc) { return $false }
    if (-not ("CchouseWin32Window" -as [type])) {
      Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class CchouseWin32Window {
  [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
}
"@ -ErrorAction SilentlyContinue
    }
    [void][CchouseWin32Window]::ShowWindowAsync($proc.MainWindowHandle, 9)
    [void][CchouseWin32Window]::SetForegroundWindow($proc.MainWindowHandle)
    return $true
  } catch {
    return $false
  }
}

function Start-CchouseVmGui {
  param([string]$Name)
  Invoke-CchouseVBox -CcArgumentList @("startvm", $Name, "--type", "gui") -FailureMessage "VBoxManage startvm --type gui failed" | Out-Null
}

function Stop-CchouseVBoxVM {
  param(
    [string]$Name,
    [ValidateSet("yes", "no")] [string]$Force = "no",
    [int]$Timeout = 60
  )
  if ([string]::IsNullOrWhiteSpace($Name)) { throw "CcVmName is required" }
  if ($Timeout -lt 1) { $Timeout = 1 }
  $info = Invoke-CchouseVBoxOptional -CcArgumentList @("showvminfo", $Name, "--machinereadable")
  if ($info.ExitCode -ne 0) { throw "VirtualBox VM not found: $Name" }
  $state = Get-CchouseVBoxState -Lines $info.Output
  if ($state -eq "stopped") {
    Write-Host "VM_STOPPED=already-stopped"
    return
  }
  if ($Force -eq "yes") {
    Invoke-CchouseVBox -CcArgumentList @("controlvm", $Name, "poweroff") -FailureMessage "poweroff failed for $Name" | Out-Null
    $waitSec = [Math]::Max(20, $Timeout)
    if (-not (Wait-CchousePowerOff -Name $Name -Seconds $waitSec)) {
      throw "poweroff timed out for $Name"
    }
    Write-Host "VM_STOPPED=force"
    return
  }
  Invoke-CchouseVBox -CcArgumentList @("controlvm", $Name, "acpipowerbutton") -FailureMessage "acpipowerbutton failed for $Name" | Out-Null
  if (-not (Wait-CchousePowerOff -Name $Name -Seconds $Timeout)) {
    throw "ACPI shutdown timed out after ${Timeout}s for $Name (use --force to power off)"
  }
  Write-Host "VM_STOPPED=acpi"
}

function Invoke-CchouseVBoxGui {
  param(
    [string]$Name,
    [int]$Timeout = 60,
    [ValidateSet("yes", "no", "keep")] [string]$ShareEnabled = "keep",
    [string]$SharePath = "",
    [ValidateSet("yes", "no")] [string]$ShareReadOnly = "no"
  )
  if ([string]::IsNullOrWhiteSpace($Name)) { throw "CcVmName is required" }
  $info = Invoke-CchouseVBoxOptional -CcArgumentList @("showvminfo", $Name, "--machinereadable")
  if ($info.ExitCode -ne 0) { throw "VirtualBox VM not found: $Name" }
  Set-CchouseVBoxCloseActions -Name $Name
  $state = Get-CchouseVBoxState -Lines $info.Output
  if ($state -eq "stopped") {
    Sync-CchouseVBoxSharedFolder -Name $Name -Enabled $ShareEnabled -HostPath $SharePath -ReadOnly $ShareReadOnly
  }
  if (-not (Test-CchouseInteractiveSession)) {
    Write-Host "当前为 SSH/非交互会话，无法弹出虚拟机窗口。请在桌面双击「cchouse」图标打开。"
    Write-Host "VM_GUI=deferred-noninteractive"
    return
  }
  if ($state -eq "running") {
    [void](Show-CchouseVBoxWindow -Name $Name)
    Write-Host "VM_GUI=already-running"
    return
  }
  if ($state -eq "running-headless") {
    Write-Host "客机正在后台无窗口运行，正在安全关机后以桌面窗口重启..."
    Stop-CchouseVBoxVM -Name $Name -Force "no" -Timeout $Timeout
    Sync-CchouseVBoxSharedFolder -Name $Name -Enabled $ShareEnabled -HostPath $SharePath -ReadOnly $ShareReadOnly
  }
  Start-CchouseVmGui -Name $Name
  Write-Host "VM_GUI=started"
}

function Restore-CchouseVBoxSnapshot {
  param(
    [string]$Name,
    [string]$SnapName = "cchouse-clean",
    [int]$Timeout = 60,
    [ValidateSet("yes", "no", "keep")] [string]$ShareEnabled = "keep",
    [string]$SharePath = "",
    [ValidateSet("yes", "no")] [string]$ShareReadOnly = "no"
  )
  if ([string]::IsNullOrWhiteSpace($Name)) { throw "CcVmName is required" }
  if ([string]::IsNullOrWhiteSpace($SnapName)) { throw "CcSnapshot is required" }
  $info = Invoke-CchouseVBoxOptional -CcArgumentList @("showvminfo", $Name, "--machinereadable")
  if ($info.ExitCode -ne 0) { throw "VirtualBox VM not found: $Name" }
  $state = Get-CchouseVBoxState -Lines $info.Output
  if ($state -in @("running", "running-headless")) {
    Stop-CchouseVBoxVM -Name $Name -Force "no" -Timeout $Timeout
  }
  if (-not (Test-CchouseVBoxSnapshotExists -Name $Name -SnapName $SnapName)) {
    throw "Snapshot not found: $SnapName"
  }
  Invoke-CchouseVBox -CcArgumentList @("snapshot", $Name, "restore", $SnapName) `
    -FailureMessage "VBoxManage snapshot restore failed" | Out-Null
  Set-CchouseVBoxCloseActions -Name $Name
  Sync-CchouseVBoxSharedFolder -Name $Name -Enabled $ShareEnabled -HostPath $SharePath -ReadOnly $ShareReadOnly
  Write-Host "RESTORE_OK=$SnapName"
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
    "boot" { Invoke-CchouseVBoxBoot $CcVmName $CcSeedIso $CcKey $CcKnownHosts $CcIpFile $CcSshPort $CcHostKeyLog $CcShareEnabled $CcSharePath $CcShareReadOnly }
    "snapshot" { New-CchouseVBoxSnapshot $CcVmName $CcSnapshot }
    "delete" { Remove-CchouseVBoxVM -Name $CcVmName -VmDir $CcVmDir }
    "status" { Get-CchouseVBoxStatus -Name $CcVmName -SnapName $CcSnapshot }
    "gui" { Invoke-CchouseVBoxGui -Name $CcVmName -Timeout $CcTimeout -ShareEnabled $CcShareEnabled -SharePath $CcSharePath -ShareReadOnly $CcShareReadOnly }
    "stop" { Stop-CchouseVBoxVM -Name $CcVmName -Force $CcForce -Timeout $CcTimeout }
    "restore" { Restore-CchouseVBoxSnapshot -Name $CcVmName -SnapName $CcSnapshot -Timeout $CcTimeout -ShareEnabled $CcShareEnabled -SharePath $CcSharePath -ShareReadOnly $CcShareReadOnly }
    "sync-share" { Sync-CchouseVBoxSharedFolder -Name $CcVmName -Enabled $CcShareEnabled -HostPath $CcSharePath -ReadOnly $CcShareReadOnly }
  }
  exit 0
}
