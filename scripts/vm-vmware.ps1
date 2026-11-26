# VMware Workstation (Windows) 客机生命周期公共函数：
#   - tools/e2e/gtr-remote.ps1 以点源（. vm-vmware.ps1）方式复用函数；
#   - scripts/cchouse.py up 以 -CcOp <op> 子命令方式调用。
# 参数统一加 Cc 前缀：点源时 param 变量会落在调用方作用域，避免覆盖调用方同名变量。
# 主机公钥来自客机串口日志。不打印任何凭据。
param(
  [ValidateSet("", "import", "firstboot", "boot", "snapshot", "exists", "delete")] [string]$CcOp = "",
  [string]$CcVmx = "",
  [string]$CcOva = "",
  [string]$CcVmName = "",
  [string]$CcVmDir = "",
  [ValidateSet("yes", "no")] [string]$CcReplaceExisting = "no",
  [string]$CcSeedIso = "",
  [string]$CcKey = "",
  [string]$CcKnownHosts = "",
  [string]$CcIpFile = "",
  [string]$CcSnapshot = "cchouse-clean",
  [int]$CcFirstbootSeconds = 55,
  [string]$CcHostKeyLog = ""
)

$script:CchouseHostKeyHelper = Join-Path $PSScriptRoot "windows-hostkey.ps1"
if (-not (Test-Path -LiteralPath $script:CchouseHostKeyHelper)) { throw "windows-hostkey.ps1 not found" }
. $script:CchouseHostKeyHelper

function Get-CchouseVmrun {
  if ($env:CCHOUSE_VMRUN -and (Test-Path $env:CCHOUSE_VMRUN)) { return $env:CCHOUSE_VMRUN }
  foreach ($p in @("C:\Program Files\VMware\VMware Workstation\vmrun.exe", "C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe")) {
    if (Test-Path $p) { return $p }
  }
  $cmd = Get-Command vmrun.exe -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  throw "vmrun.exe not found (install VMware Workstation or set CCHOUSE_VMRUN)"
}

function Get-CchouseOvftool {
  if ($env:CCHOUSE_OVFTOOL -and (Test-Path $env:CCHOUSE_OVFTOOL)) { return $env:CCHOUSE_OVFTOOL }
  foreach ($p in @("C:\Program Files\VMware\VMware Workstation\OVFTool\ovftool.exe", "C:\Program Files (x86)\VMware\VMware Workstation\OVFTool\ovftool.exe")) {
    if (Test-Path $p) { return $p }
  }
  $cmd = Get-Command ovftool.exe -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  throw "ovftool.exe not found (bundled with VMware Workstation)"
}

function Fix-KeyAcl([string]$path) {
  if (Test-Path $path) {
    & icacls.exe $path /inheritance:r /grant:r "${env:USERNAME}:(R,W)" | Out-Null
  }
}

function Set-VmxKeys([string]$vmxPath, [hashtable]$kv) {
  $lines = [System.Collections.Generic.List[string]]::new()
  foreach ($l in [System.IO.File]::ReadAllLines($vmxPath)) {
    $lines.Add($l)
  }
  foreach ($k in $kv.Keys) {
    $val = $kv[$k]
    $newLine = "$k = `"$val`""
    $found = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
      if ($lines[$i] -match ("^\s*" + [regex]::Escape($k) + "\s*=")) {
        $lines[$i] = $newLine
        $found = $true
      }
    }
    if (-not $found) {
      $lines.Add($newLine)
    }
  }
  [System.IO.File]::WriteAllText($vmxPath, (($lines -join "`r`n") + "`r`n"), [System.Text.UTF8Encoding]::new($false))
}

function Enable-CchouseVmwareHostKeySerial([string]$vmxPath, [string]$hostKeyLog) {
  if ([string]::IsNullOrWhiteSpace($hostKeyLog)) { return }
  Initialize-CchouseHostKeyLog -Path $hostKeyLog
  # serial0 文件模式对应客机 /dev/ttyS0。yieldOnMsrRead 避免客机在无人读取时空转。
  Set-VmxKeys $vmxPath @{
    "serial0.present" = "TRUE"
    "serial0.fileType" = "file"
    "serial0.fileName" = $hostKeyLog
    "serial0.yieldOnMsrRead" = "TRUE"
    "msg.autoAnswer" = "TRUE"
  }
}

function Start-VmDetached([string]$vmrPath, [string]$vmxPath) {
  # Spawn vmrun via WMI Win32_Process so vmware-vmx.exe is NOT killed when the OpenSSH session / caller exits
  $cmd = "`"$vmrPath`" -T ws start `"$vmxPath`" nogui"
  $res = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd }
  if ($res.ReturnValue -ne 0) {
    throw "Win32_Process.Create failed for vmrun start (rc=$($res.ReturnValue))"
  }
  Start-Sleep 4
}

function Import-CchouseOva([string]$ovaPath, [string]$name, [string]$destDir) {
  if (-not (Test-Path $ovaPath)) { throw "OVA not found: $ovaPath" }
  $ovf = Get-CchouseOvftool
  New-Item -ItemType Directory -Force -Path $destDir | Out-Null
  Write-Host "Importing $ovaPath -> $destDir\$name..."
  & $ovf --name=$name $ovaPath $destDir
  if ($LASTEXITCODE -ne 0) { throw "ovftool import failed" }
}

function Invoke-CchouseFirstboot([string]$vmxPath, [string]$seedIso, [int]$seconds = 55, [string]$hostKeyLog = "") {
  if (-not (Test-Path $seedIso)) { throw "seed.iso not found: $seedIso" }
  $vmr = Get-CchouseVmrun
  Enable-CchouseVmwareHostKeySerial $vmxPath $hostKeyLog
  Set-VmxKeys $vmxPath @{
    "ethernet0.startConnected" = "FALSE"
    "ide1:0.present" = "TRUE"
    "ide1:0.deviceType" = "cdrom-image"
    "ide1:0.autodetect" = "FALSE"
    "ide1:0.fileName" = $seedIso
    "ide1:0.startConnected" = "TRUE"
    "msg.autoAnswer" = "TRUE"
  }
  Write-Host "Starting offline firstboot (nogui, ${seconds}s)..."
  & $vmr -T ws start $vmxPath nogui
  if ($LASTEXITCODE -ne 0) { throw "firstboot start failed" }
  Start-Sleep $seconds
  & $vmr -T ws stop $vmxPath soft 2>$null | Out-Null
  Start-Sleep 5
  & $vmr -T ws stop $vmxPath hard 2>$null | Out-Null
  Start-Sleep 2
  Write-Host "FIRSTBOOT_STOPPED=OK"
}

# 卸载并删除 seed.iso、接通网卡、后台开机，等 SSH 就绪后写 known_hosts 与 IP 文件；返回客机 IP
function Invoke-CchouseBoot([string]$vmxPath, [string]$seedIso, [string]$keyPath, [string]$khPath, [string]$ipFile, [string]$hostKeyLog = "") {
  $vmr = Get-CchouseVmrun
  Enable-CchouseVmwareHostKeySerial $vmxPath $hostKeyLog
  # 开机前读串口日志。冲突的主机公钥直接失败，不连网。
  $serialText = Read-CchouseSerialText -Path $hostKeyLog
  $null = Get-CchouseHostKeyFromSerialText -Text $serialText
  Set-VmxKeys $vmxPath @{
    "ide1:0.fileName" = ""
    "ide1:0.startConnected" = "FALSE"
    "ethernet0.startConnected" = "TRUE"
  }
  if ($seedIso) { Remove-Item -Force $seedIso -ErrorAction SilentlyContinue }
  Write-Host "Detached and deleted seed.iso; starting networked VM (detached)..."
  Start-VmDetached $vmr $vmxPath

  Fix-KeyAcl $keyPath
  if (-not (Test-Path -LiteralPath $script:CchouseSystem32Ssh)) { throw "System32 OpenSSH ssh.exe not found" }
  $ip = ""
  $ok = $false
  $pinnedNow = $false
  $startedAt = Get-Date
  while (-not (Test-CchouseSshDeadlineReached -StartedAt $startedAt -LimitSeconds $script:CchouseSshWaitSeconds)) {
    $remaining = [int][Math]::Floor($script:CchouseSshWaitSeconds - ((Get-Date) - $startedAt).TotalSeconds)
    if ($remaining -lt 1) { break }
    $sleepFor = [int][Math]::Min(3, $remaining)
    if ($sleepFor -lt 1) { break }
    Start-Sleep -Seconds $sleepFor
    $ipOut = & $vmr -T ws getGuestIPAddress $vmxPath 2>$null
    if ($LASTEXITCODE -eq 0 -and $ipOut -match '^\d+\.\d+\.\d+\.\d+$' -and $ipOut -notmatch '^127\.') {
      $ip = $ipOut.Trim()
    }
    if (-not $ip) {
      $ml = Select-String -Path $vmxPath -Pattern 'ethernet0\.generatedAddress\s*=\s*"([^"]+)"' -ErrorAction SilentlyContinue
      if ($ml) {
        $mac = $ml.Matches[0].Groups[1].Value.ToLower()
        $lt = Get-Content "C:\ProgramData\VMware\vmnetdhcp.leases" -Raw -ErrorAction SilentlyContinue
        foreach ($b in [regex]::Matches($lt, 'lease\s+(\d+\.\d+\.\d+\.\d+)\s*\{[^}]*hardware\s+ethernet\s+([0-9a-fA-F:]+);', [System.Text.RegularExpressions.RegexOptions]::Singleline)) {
          if ($b.Groups[2].Value.ToLower() -eq $mac) { $ip = $b.Groups[1].Value }
        }
      }
    }
    if ($ip -notmatch '^\d+\.\d+\.\d+\.\d+$' -or $ip -match '^127\.') { continue }
    # IP 只来自这台 vmx 的 vmrun 或与其 MAC 对应的租约。
    $trust = Resolve-CchouseHostKeyTrust -SerialText $serialText -HostName $ip.Trim() -Port 22
    $pinnedNow = [bool]$trust.Pinned
    if ($pinnedNow) {
      Write-CchouseKnownHostsFile -Path $khPath -Line $trust.KnownHostsLine
    }
    $timeout = $remaining
    if ($timeout -gt 5) { $timeout = 5 }
    $verify = @(Get-CchouseSshVerifyOptions -KnownHostsPath $khPath -Pinned:$pinnedNow -ConnectTimeout $timeout)
    $sshOpts = @("-F", "none", "-i", $keyPath) + $verify
    if (Invoke-CchouseSshTrue -Target "claude@$ip" -SshOption $sshOpts) { $ok = $true; break }
  }
  if (-not $ok) { throw "Timed out waiting for SSH (last ip=$ip)" }
  if (-not $pinnedNow) {
    $recorded = ""
    if (Test-Path -LiteralPath $khPath) { $recorded = [System.IO.File]::ReadAllText($khPath).Trim() }
    if (-not $recorded) { throw "ssh did not record a host key" }
  }
  [System.IO.File]::WriteAllText($ipFile, $ip, [System.Text.UTF8Encoding]::new($false))
  Write-Host "GUEST_IP=$ip"
  return $ip
}

function New-CchouseSnapshot([string]$vmxPath, [string]$name) {
  $vmr = Get-CchouseVmrun
  & $vmr -T ws deleteSnapshot $vmxPath $name 2>$null | Out-Null
  & $vmr -T ws snapshot $vmxPath $name
  if ($LASTEXITCODE -ne 0) { throw "vmrun snapshot failed" }
  Write-Host "SNAPSHOT_OK=$name"
}

function Remove-CchouseVM([string]$vmxPath) {
  if (-not (Test-Path -LiteralPath $vmxPath -PathType Leaf)) { return }
  $vmr = Get-CchouseVmrun
  $vmxPath = (Get-Item -LiteralPath $vmxPath).FullName
  $running = & $vmr -T ws list
  if ($LASTEXITCODE -ne 0) { throw "vmrun list failed" }
  $runningPaths = $running | Select-Object -Skip 1 | Where-Object { $_.Trim() } | ForEach-Object {
    [System.IO.Path]::GetFullPath($_.Trim())
  }
  if ($runningPaths -icontains $vmxPath) {
    & $vmr -T ws stop $vmxPath hard
    if ($LASTEXITCODE -ne 0) { throw "vmrun stop failed for $vmxPath" }
  }
  # Workstation deletes the VM and snapshots together.
  & $vmr -T ws deleteVM $vmxPath
  if ($LASTEXITCODE -ne 0 -or (Test-Path -LiteralPath $vmxPath)) {
    throw "vmrun deleteVM failed for $vmxPath"
  }
}

if ($CcOp) {
  $ErrorActionPreference = "Stop"
  switch ($CcOp) {
    "exists" { if (Test-Path $CcVmx) { exit 0 } else { exit 1 } }
    "import" {
      if (Test-Path -LiteralPath $CcVmx) {
        if ($CcReplaceExisting -ne "yes") { throw "Existing VMX is not owned by this install: $CcVmx" }
        $vmr = Get-CchouseVmrun
        & $vmr -T ws stop $CcVmx hard 2>$null | Out-Null
        & $vmr -T ws deleteVM $CcVmx 2>$null | Out-Null
        Remove-Item -Recurse -Force (Split-Path $CcVmx -Parent) -ErrorAction SilentlyContinue
      }
      Import-CchouseOva $CcOva $CcVmName $CcVmDir
      if (-not (Test-Path $CcVmx)) { throw "Expected VMX not found: $CcVmx" }
      Write-Host "VMX_READY=$CcVmx"
    }
    "firstboot" { Invoke-CchouseFirstboot $CcVmx $CcSeedIso $CcFirstbootSeconds $CcHostKeyLog }
    "boot" { Invoke-CchouseBoot $CcVmx $CcSeedIso $CcKey $CcKnownHosts $CcIpFile $CcHostKeyLog | Out-Null }
    "snapshot" { New-CchouseSnapshot $CcVmx $CcSnapshot }
    "delete" { Remove-CchouseVM $CcVmx }
  }
  exit 0
}
