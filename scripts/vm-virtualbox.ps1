# VirtualBox (Windows) guest lifecycle for scripts/cchouse.py up.
# Cc-prefixed parameters stay out of a dot-sourced caller's scope.
# NAT only. No add-on package, no host-only adapter, no bridged NIC.
# Do not print credentials.
param(
  [ValidateSet("", "install", "import", "firstboot", "boot", "snapshot", "exists")] [string]$CcOp = "",
  [string]$CcOva = "",
  [string]$CcVmName = "",
  [string]$CcVmDir = "",
  [string]$CcSeedIso = "",
  [string]$CcKey = "",
  [string]$CcKnownHosts = "",
  [string]$CcIpFile = "",
  [string]$CcSnapshot = "cchouse-clean",
  [int]$CcFirstbootSeconds = 56,
  [int]$CcSshPort = 2222,
  [string]$CcInstaller = "",
  [string]$CcSha256 = ""
)

$script:CchouseSystem32Ssh = "C:\Windows\System32\OpenSSH\ssh.exe"
$script:CchouseSystem32Keyscan = "C:\Windows\System32\OpenSSH\ssh-keyscan.exe"

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

function Install-CchouseVirtualBox {
  param([string]$Installer, [string]$ExpectedSha)
  $existing = Get-CchouseVBoxManage
  if ($existing) {
    Write-Host "VBOX_ALREADY_INSTALLED=$existing"
    return
  }
  if (-not $Installer -or -not (Test-Path -LiteralPath $Installer)) {
    throw "VirtualBox installer not found"
  }
  if ($ExpectedSha) {
    $hash = (Get-FileHash -LiteralPath $Installer -Algorithm SHA256).Hash.ToLower()
    if ($hash -ne $ExpectedSha.ToLower()) { throw "VirtualBox installer SHA-256 mismatch" }
  }
  # NAT-only feature set. A full install is not a fallback: it would add bridge filters.
  $argList = @(
    "--silent",
    "--ignore-reboot",
    "--msiparams",
    "ADDLOCAL=VBoxApplication",
    "VBOX_INSTALLDESKTOPSHORTCUT=0",
    "VBOX_INSTALLQUICKLAUNCHSHORTCUT=0",
    "VBOX_REGISTERFILEEXTENSIONS=0",
    "VBOX_START=0"
  )
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

function Get-CchouseIdeController {
  param([string]$Name)
  $vbm = Get-CchouseVBoxManage
  $info = @(& $vbm showvminfo $Name --machinereadable)
  foreach ($line in $info) {
    if ($line -match '^storagecontrollername\d+="([^"]+)"' -and $Matches[1] -match 'IDE') {
      return $Matches[1]
    }
  }
  throw "No IDE storage controller on $Name"
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

function Import-CchouseVBoxOva {
  param([string]$OvaPath, [string]$Name, [string]$DestDir, [int]$SshPort)
  if (-not (Test-Path -LiteralPath $OvaPath)) { throw "OVA not found" }
  if ($SshPort -lt 1 -or $SshPort -gt 65535) { throw "SSH port out of range" }
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
  Write-Host "VBOX_IMPORTED=$Name"
}

function Invoke-CchouseVBoxFirstboot {
  param([string]$Name, [string]$SeedIso, [int]$Seconds)
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
  Write-Host "Starting offline firstboot (headless, ${Seconds}s)..."
  Start-CchouseVmDetached $Name
  Start-Sleep -Seconds $Seconds
  & $vbm controlvm $Name acpipowerbutton 2>$null | Out-Null
  if (-not (Wait-CchousePowerOff $Name 40)) {
    & $vbm controlvm $Name poweroff 2>$null | Out-Null
    Wait-CchousePowerOff $Name 20 | Out-Null
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
    [int]$SshPort
  )
  if ($SshPort -lt 1 -or $SshPort -gt 65535) { throw "SSH port out of range" }
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
  Write-Host "Detached seed.iso; starting networked VM (detached)..."
  Start-CchouseVmDetached $Name

  if (-not (Test-Path -LiteralPath $script:CchouseSystem32Ssh)) { throw "System32 OpenSSH ssh.exe not found" }
  if (-not (Test-Path -LiteralPath $script:CchouseSystem32Keyscan)) { throw "System32 OpenSSH ssh-keyscan.exe not found" }
  if ($KeyPath -and (Test-Path -LiteralPath $KeyPath)) {
    & icacls.exe $KeyPath /inheritance:r /grant:r "${env:USERNAME}:(R,W)" | Out-Null
  }
  $sshOpts = @(
    "-F", "none", "-p", "$SshPort", "-i", $KeyPath,
    "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=NUL",
    "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes"
  )
  $ok = $false
  for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Seconds 3
    & $script:CchouseSystem32Ssh @sshOpts "claude@127.0.0.1" "true" 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { $ok = $true; break }
  }
  if (-not $ok) { throw "Timed out waiting for SSH on 127.0.0.1:$SshPort" }

  $scan = @(& $script:CchouseSystem32Keyscan -p $SshPort -t ed25519 127.0.0.1 2>$null)
  $scanText = (@($scan) -join "`n").Trim()
  if (-not $scanText) { throw "ssh-keyscan returned no host key" }
  [System.IO.File]::WriteAllText($KnownHostsPath, ($scanText + "`n"), [System.Text.UTF8Encoding]::new($false))
  [System.IO.File]::WriteAllText($IpFile, "127.0.0.1", [System.Text.UTF8Encoding]::new($false))
  Write-Host "GUEST_IP=127.0.0.1"
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
    "import" { Import-CchouseVBoxOva $CcOva $CcVmName $CcVmDir $CcSshPort }
    "firstboot" { Invoke-CchouseVBoxFirstboot $CcVmName $CcSeedIso $CcFirstbootSeconds }
    "boot" { Invoke-CchouseVBoxBoot $CcVmName $CcSeedIso $CcKey $CcKnownHosts $CcIpFile $CcSshPort }
    "snapshot" { New-CchouseVBoxSnapshot $CcVmName $CcSnapshot }
  }
  exit 0
}
