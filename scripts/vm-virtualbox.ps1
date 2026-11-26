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
  param([string[]]$CcArgumentList)
  $vbm = Get-CchouseVBoxManage
  if (-not $vbm) { throw "VBoxManage.exe not found (install VirtualBox or set CCHOUSE_VBOXMANAGE)" }
  & $vbm @CcArgumentList
  if ($LASTEXITCODE -ne 0) { throw "VBoxManage failed (rc=$LASTEXITCODE)" }
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
  if (-not $Installer -or -not (Test-Path -LiteralPath $Installer)) {
    throw "VirtualBox installer not found"
  }
  $hash = (Get-FileHash -LiteralPath $Installer -Algorithm SHA256).Hash.ToLower()
  if ($hash -ne $ExpectedSha.ToLower()) { throw "VirtualBox installer SHA-256 mismatch" }
  # NAT-only feature set. A full install is not a fallback: it would add bridge filters.
  $argList = @(Get-CchouseVirtualBoxInstallArgs)
  $proc = Start-Process -FilePath $Installer -ArgumentList $argList -Wait -PassThru
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
  & $vbm showvminfo $Name --machinereadable 2>$null | Out-Null
  return ($LASTEXITCODE -eq 0)
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
  $vbm = Get-CchouseVBoxManage
  if (-not $vbm) { throw "VBoxManage.exe not found" }
  $info = @(& $vbm showvminfo $Name --machinereadable)
  $controller = Get-CchouseIdeControllerName -Lines $info
  if ([string]::IsNullOrEmpty($controller)) {
    throw "No IDE storage controller on $Name"
  }
  return $controller
}

function Get-CchouseVmUuidFromLines {
  param([string[]]$Lines)
  foreach ($line in @($Lines)) {
    if ($line -match '^UUID="([^"]+)"') { return $Matches[1] }
  }
  return $null
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

function Test-CchouseListenerCommand {
  param([string]$CommandLine, [string]$VmUuid, [string]$VmName)
  if ([string]::IsNullOrWhiteSpace($CommandLine)) { return $false }
  if ($CommandLine -notmatch '(?i)(^|[\\/"\s])(VBoxHeadless|VBoxNetNAT)\.exe(\s|"|$)') {
    return $false
  }
  $uuid = ""
  if ($VmUuid) { $uuid = ($VmUuid -replace '[{}]', '').ToLower() }
  if ($uuid -and $CommandLine.ToLower().Contains($uuid)) { return $true }
  if ($VmName) {
    $escaped = [regex]::Escape($VmName)
    if ($CommandLine -match "(?i)(--comment|--startvm)\s+`"$escaped`"") { return $true }
    if ($CommandLine -match "(?i)(--comment|--startvm)\s+$escaped(\s|$)") { return $true }
  }
  return $false
}

function Test-CchouseOwnForward {
  param([string[]]$Lines, [int]$Port, [string]$ListenerCommand)
  if (-not (Test-CchouseForwardRule -Lines $Lines -Port $Port)) { return $false }
  $uuid = Get-CchouseVmUuidFromLines -Lines $Lines
  $name = Get-CchouseVmNameFromLines -Lines $Lines
  return [bool](Test-CchouseListenerCommand -CommandLine $ListenerCommand -VmUuid $uuid -VmName $name)
}

function Resolve-CchouseProbeDecision {
  param(
    [string[]]$Lines,
    [int]$Port,
    [string]$ListenerCommand,
    [bool]$ListenerPresent
  )
  # [string] turns $null into "". ListenerPresent is the "nothing is bound yet" signal.
  if (-not (Test-CchouseForwardRule -Lines $Lines -Port $Port)) { return "reject" }
  if (-not $ListenerPresent) { return "wait" }
  if (Test-CchouseOwnForward -Lines $Lines -Port $Port -ListenerCommand $ListenerCommand) { return "probe" }
  return "reject"
}

function Assert-CchouseSshPort {
  param([int]$Port)
  if ($Port -lt 1 -or $Port -gt 65535) { throw "CcSshPort is required" }
}

function Get-CchouseShowvminfoLines {
  param([string]$Name)
  $vbm = Get-CchouseVBoxManage
  if (-not $vbm) { throw "VBoxManage.exe not found" }
  return @(& $vbm showvminfo $Name --machinereadable)
}

function Get-CchouseLoopbackListenerCommand {
  param([int]$Port)
  $conns = @()
  try {
    $conns = @(Get-NetTCPConnection -LocalAddress "127.0.0.1" -LocalPort $Port -State Listen -ErrorAction Stop |
      Where-Object { $null -ne $_ })
  } catch {
    $msg = [string]$_.Exception.Message
    if ($msg -match 'No matching') { return $null }
    throw
  }
  if ($conns.Count -eq 0) { return $null }
  foreach ($conn in $conns) {
    $procId = [int]$conn.OwningProcess
    if ($procId -le 0) { continue }
    $proc = Get-CimInstance -ClassName Win32_Process -Filter ("ProcessId=" + $procId)
    if ($null -eq $proc) { continue }
    if ($null -eq $proc.CommandLine) { return "" }
    return [string]$proc.CommandLine
  }
  return ""
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
  $vbm = Get-CchouseVBoxManage
  for ($i = 0; $i -lt $Seconds; $i++) {
    $info = @(& $vbm showvminfo $Name --machinereadable)
    foreach ($line in $info) {
      if ($line -eq 'VMState="poweroff"' -or $line -eq 'VMState="aborted"') { return $true }
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
  param([string]$OvaPath, [string]$Name, [string]$DestDir, [int]$SshPort, [string]$HostKeyLog)
  if (-not (Test-Path -LiteralPath $OvaPath)) { throw "OVA not found" }
  Assert-CchouseSshPort -Port $SshPort
  $vbm = Get-CchouseVBoxManage
  if (-not $vbm) { throw "VBoxManage.exe not found (install VirtualBox or set CCHOUSE_VBOXMANAGE)" }
  New-Item -ItemType Directory -Force -Path $DestDir | Out-Null
  if (Test-CchouseVmRegistered $Name) {
    & $vbm controlvm $Name poweroff 2>$null | Out-Null
    & $vbm unregistervm $Name --delete
    if ($LASTEXITCODE -ne 0) { throw "unregistervm failed" }
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
  $vbm = Get-CchouseVBoxManage
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
  & $vbm controlvm $Name acpipowerbutton 2>$null | Out-Null
  $afterAcpi = Wait-CchousePowerOff $Name 40
  $afterHard = $false
  if (-not $afterAcpi) {
    & $vbm controlvm $Name poweroff 2>$null | Out-Null
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
  & $vbm storageattach $Name --storagectl $ide --port 1 --device 0 --type dvddrive --medium emptydrive
  if ($LASTEXITCODE -ne 0) { throw "detach seed ISO failed" }
  if ($SeedIso) {
    & $vbm closemedium dvd $SeedIso 2>$null | Out-Null
    if (Test-Path -LiteralPath $SeedIso) { Remove-Item -LiteralPath $SeedIso -Force }
    if (Test-Path -LiteralPath $SeedIso) { throw "seed.iso still present" }
  }
  & $vbm modifyvm $Name --cableconnected1 on
  if ($LASTEXITCODE -ne 0) { throw "enable NIC failed" }
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
  if ($KeyPath -and (Test-Path -LiteralPath $KeyPath)) {
    & icacls.exe $KeyPath /inheritance:r /grant:r "${env:USERNAME}:(R,W)" | Out-Null
  }
  $ok = $false
  $startedAt = Get-Date
  while (-not (Test-CchouseSshDeadlineReached -StartedAt $startedAt -LimitSeconds $script:CchouseSshWaitSeconds)) {
    $remaining = [int][Math]::Floor($script:CchouseSshWaitSeconds - ((Get-Date) - $startedAt).TotalSeconds)
    if ($remaining -lt 1) { break }
    $sleepFor = [int][Math]::Min(3, $remaining)
    if ($sleepFor -lt 1) { break }
    Start-Sleep -Seconds $sleepFor
    $info = @(Get-CchouseShowvminfoLines -Name $Name)
    $listener = Get-CchouseLoopbackListenerCommand -Port $SshPort
    $present = $null -ne $listener
    $decision = Resolve-CchouseProbeDecision -Lines $info -Port $SshPort -ListenerCommand ([string]$listener) -ListenerPresent:$present
    if ($decision -eq "reject") {
      throw "SSH forward 127.0.0.1:${SshPort} is not owned by $Name"
    }
    if ($decision -ne "probe") { continue }
    $pinned = [bool]$trust.Pinned
    if (-not $pinned) {
      if (-not (Test-CchouseOwnForward -Lines $info -Port $SshPort -ListenerCommand ([string]$listener))) {
        throw "SSH forward 127.0.0.1:${SshPort} is not owned by $Name; refusing to record a host key"
      }
    }
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

function Remove-CchouseVBoxVM {
  param([string]$Name)
  if ($Name -notmatch '^claude-[a-z][a-z0-9_-]{1,15}$') { throw "VM name is not an exact instance name" }
  $vbm = Get-CchouseVBoxManage
  if (-not $vbm) { throw "VBoxManage.exe not found" }
  if (-not (Test-CchouseVmRegistered $Name)) { return }
  $info = @(Get-CchouseShowvminfoLines -Name $Name)
  $reported = Get-CchouseVmNameFromLines -Lines $info
  if ([string]::IsNullOrEmpty($reported) -or ($reported -cne $Name)) {
    throw "registered VM name does not match $Name"
  }
  $vmState = ""
  foreach ($line in $info) {
    if ($line -match '^VMState="([^"]*)"') {
      $vmState = $Matches[1]
      break
    }
  }
  if ($vmState -ne "poweroff" -and $vmState -ne "aborted") {
    & $vbm controlvm $Name poweroff
    if ($LASTEXITCODE -ne 0) { throw "poweroff failed for $Name" }
    if (-not (Wait-CchousePowerOff $Name 40)) { throw "poweroff timed out for $Name" }
  }
  & $vbm unregistervm $Name --delete
  if ($LASTEXITCODE -ne 0) { throw "unregistervm failed for $Name" }
  if (Test-CchouseVmRegistered $Name) { throw "VM $Name is still registered" }
}

function New-CchouseVBoxSnapshot {
  param([string]$Name, [string]$SnapName)
  $vbm = Get-CchouseVBoxManage
  & $vbm snapshot $Name delete $SnapName 2>$null | Out-Null
  & $vbm snapshot $Name take $SnapName --description "cchouse clean"
  if ($LASTEXITCODE -ne 0) { throw "VBoxManage snapshot take failed" }
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
    "import" { Import-CchouseVBoxOva $CcOva $CcVmName $CcVmDir $CcSshPort $CcHostKeyLog }
    "firstboot" { Invoke-CchouseVBoxFirstboot $CcVmName $CcSeedIso $CcFirstbootSeconds $CcHostKeyLog }
    "boot" { Invoke-CchouseVBoxBoot $CcVmName $CcSeedIso $CcKey $CcKnownHosts $CcIpFile $CcSshPort $CcHostKeyLog }
    "snapshot" { New-CchouseVBoxSnapshot $CcVmName $CcSnapshot }
    "delete" { Remove-CchouseVBoxVM $CcVmName }
  }
  exit 0
}
