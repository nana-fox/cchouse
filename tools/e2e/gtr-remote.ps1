param(
  [Parameter(Mandatory=$true)][ValidateSet("clone","firstboot","reboot","verify","desktop","cleanup")] [string]$Action,
  [Parameter(Mandatory=$true)][string]$RunDir,
  [Parameter(Mandatory=$true)][string]$VmName,
  [string]$Mode = "fast",
  [string]$OvaPath = "",
  [string]$BaseVmx = "",
  [string]$Tag = "e2egtr",
  [string]$StripeMode = "front"
)

$ErrorActionPreference = "Stop"
$vmr = "C:\Program Files\VMware\VMware Workstation\vmrun.exe"
$ovf = "C:\Program Files\VMware\VMware Workstation\OVFTool\ovftool.exe"
$vmDir = Join-Path $RunDir "vm"
$vmx = Join-Path $vmDir "$VmName\$VmName.vmx"
$keyPath = Join-Path $RunDir "id_ed25519"
$khPath = Join-Path $RunDir "known_hosts"
$ipFile = Join-Path $RunDir "guest_ip.txt"

function Fix-KeyAcl([string]$path) {
  if (Test-Path $path) {
    & icacls.exe $path /inheritance:r /grant:r "${env:USERNAME}:(R,W)" | Out-Null
  }
}

function Find-GitBash {
  foreach ($p in @(
    "C:\Program Files\Git\bin\bash.exe",
    "C:\Program Files\Git\usr\bin\bash.exe",
    "C:\Program Files (x86)\Git\bin\bash.exe"
  )) {
    if (Test-Path $p) { return $p }
  }
  $cmd = Get-Command bash.exe -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  throw "Git Bash not found on GTR"
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

function Start-VmDetached([string]$vmrPath, [string]$vmxPath) {
  # Spawn vmrun via WMI Win32_Process so vmware-vmx.exe is NOT killed when the OpenSSH session exits
  $cmd = "`"$vmrPath`" -T ws start `"$vmxPath`" nogui"
  $res = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd }
  if ($res.ReturnValue -ne 0) {
    throw "Win32_Process.Create failed for vmrun start (rc=$($res.ReturnValue))"
  }
  Start-Sleep 4
}

switch ($Action) {
  "clone" {
    Fix-KeyAcl $keyPath
    New-Item -ItemType Directory -Force -Path $vmDir | Out-Null
    if ($Mode -eq "release") {
      if (-not (Test-Path $OvaPath)) { throw "OVA not found: $OvaPath" }
      Write-Host "Importing release OVA $OvaPath -> $vmDir..."
      & $ovf --name=$VmName $OvaPath $vmDir
      if ($LASTEXITCODE -ne 0) { throw "ovftool release import failed" }
    } else {
      $needBase = $true
      if (Test-Path $BaseVmx) {
        if ((Test-Path $OvaPath) -and ((Get-Item $OvaPath).LastWriteTimeUtc -gt (Get-Item $BaseVmx).LastWriteTimeUtc)) {
          Write-Host "OVA is newer than cached base VM; re-importing base VM..."
          $baseParent = Split-Path (Split-Path $BaseVmx -Parent) -Parent
          Remove-Item -Recurse -Force $baseParent -ErrorAction SilentlyContinue
        } else {
          $needBase = $false
        }
      }
      if ($needBase) {
        if (-not (Test-Path $OvaPath)) { throw "Golden OVA not found: $OvaPath" }
        $baseName = [System.IO.Path]::GetFileNameWithoutExtension($BaseVmx)
        $baseParent = Split-Path (Split-Path $BaseVmx -Parent) -Parent
        New-Item -ItemType Directory -Force -Path $baseParent | Out-Null
        Write-Host "Importing golden OVA to cached base VM: $BaseVmx..."
        & $ovf --name=$baseName $OvaPath $baseParent
        if ($LASTEXITCODE -ne 0) { throw "ovftool base import failed" }
        # Create a snapshot on base VM for fast linked clones
        & $vmr -T ws snapshot $BaseVmx "golden-base"
        if ($LASTEXITCODE -ne 0) { throw "vmrun base snapshot failed" }
      }
      Write-Host "Creating linked clone: $BaseVmx -> $vmx..."
      & $vmr -T ws clone $BaseVmx $vmx linked -snapshot="golden-base" -cloneName=$VmName
      if ($LASTEXITCODE -ne 0) {
        # Fallback without explicit snapshot name
        & $vmr -T ws clone $BaseVmx $vmx linked -cloneName=$VmName
        if ($LASTEXITCODE -ne 0) { throw "vmrun linked clone failed" }
      }
    }
    if (-not (Test-Path $vmx)) { throw "Expected VMX not found: $vmx" }
    Write-Host "VMX_READY=$vmx"
  }

  "firstboot" {
    $seedIso = Join-Path $RunDir "seed.iso"
    if (-not (Test-Path $seedIso)) { throw "seed.iso not found: $seedIso" }
    Set-VmxKeys $vmx @{
      "ethernet0.startConnected" = "FALSE"
      "ide1:0.present" = "TRUE"
      "ide1:0.deviceType" = "cdrom-image"
      "ide1:0.autodetect" = "FALSE"
      "ide1:0.fileName" = $seedIso
      "ide1:0.startConnected" = "TRUE"
      "msg.autoAnswer" = "TRUE"
    }

    Write-Host "Starting offline firstboot (nogui, 55s)..."
    & $vmr -T ws start $vmx nogui
    if ($LASTEXITCODE -ne 0) { throw "firstboot start failed" }
    Start-Sleep 55
    & $vmr -T ws stop $vmx soft 2>$null | Out-Null
    Start-Sleep 5
    & $vmr -T ws stop $vmx hard 2>$null | Out-Null
    Start-Sleep 2
    Write-Host "FIRSTBOOT_STOPPED=OK"
  }

  "reboot" {
    $seedIso = Join-Path $RunDir "seed.iso"
    Set-VmxKeys $vmx @{
      "ide1:0.fileName" = ""
      "ide1:0.startConnected" = "FALSE"
      "ethernet0.startConnected" = "TRUE"
    }

    Remove-Item -Force $seedIso -ErrorAction SilentlyContinue
    Write-Host "Detached and deleted seed.iso; starting networked VM (detached from SSH job)..."
    Start-VmDetached $vmr $vmx

    Fix-KeyAcl $keyPath
    $ip = ""; $ok = $false
    $sshOpts = @("-F","none","-i",$keyPath,"-o","StrictHostKeyChecking=no","-o","UserKnownHostsFile=NUL","-o","LogLevel=ERROR","-o","ConnectTimeout=5","-o","BatchMode=yes")
    for ($i = 0; $i -lt 45; $i++) {
      Start-Sleep 3
      $ipOut = & $vmr -T ws getGuestIPAddress $vmx 2>$null
      if ($LASTEXITCODE -eq 0 -and $ipOut -match '^\d+\.\d+\.\d+\.\d+$' -and $ipOut -notmatch '^127\.') {
        $ip = $ipOut.Trim()
      }
      if (-not $ip) {
        $ml = Select-String -Path $vmx -Pattern 'ethernet0\.generatedAddress\s*=\s*"([^"]+)"' -ErrorAction SilentlyContinue
        if ($ml) {
          $mac = $ml.Matches[0].Groups[1].Value.ToLower()
          $lt = Get-Content "C:\ProgramData\VMware\vmnetdhcp.leases" -Raw -ErrorAction SilentlyContinue
          foreach ($b in [regex]::Matches($lt, 'lease\s+(\d+\.\d+\.\d+\.\d+)\s*\{[^}]*hardware\s+ethernet\s+([0-9a-fA-F:]+);', [System.Text.RegularExpressions.RegexOptions]::Singleline)) {
            if ($b.Groups[2].Value.ToLower() -eq $mac) { $ip = $b.Groups[1].Value }
          }
        }
      }
      if ($ip) {
        & ssh.exe @sshOpts "claude@$ip" "true" 2>$null
        if ($LASTEXITCODE -eq 0) { $ok = $true; break }
      }
    }
    if (-not $ok) { throw "Timed out waiting for SSH (last ip=$ip)" }

    $scan = & ssh-keyscan.exe -t ed25519 $ip 2>$null
    [System.IO.File]::WriteAllText($khPath, (($scan -join "`n") + "`n"), [System.Text.UTF8Encoding]::new($false))
    [System.IO.File]::WriteAllText($ipFile, $ip, [System.Text.UTF8Encoding]::new($false))

    $envFile = Join-Path $RunDir "gtr-verify.env"
    Add-Content -Path $envFile -Value "`nGUEST_IP=$ip" -Encoding UTF8
    Write-Host "GUEST_IP=$ip"
  }

  "verify" {
    $bash = Find-GitBash
    $verifySh = (Join-Path $RunDir "verify-linux.sh") -replace '\\', '/'
    $envPath = (Join-Path $RunDir "gtr-verify.env") -replace '\\', '/'
    # Prefer Windows OpenSSH (same client verified in reboot step) over MSYS ssh
    $env:PATH = "C:\Windows\System32\OpenSSH;$env:PATH"
    & $bash "$verifySh" "$envPath" --fail-closed
    exit $LASTEXITCODE
  }

  "desktop" {
    $ip = (Get-Content $ipFile -Raw).Trim()
    Fix-KeyAcl $keyPath
    $sshOpts = @("-F","none","-i",$keyPath,"-o","StrictHostKeyChecking=yes","-o","UserKnownHostsFile=$khPath","-o","BatchMode=yes","-o","ConnectTimeout=10")
    $localCheck = Join-Path $RunDir "guest-checks.sh"
    $raw = (Get-Content $localCheck -Raw) -replace "`r`n", "`n"
    [System.IO.File]::WriteAllText($localCheck, $raw, [System.Text.UTF8Encoding]::new($false))
    & scp.exe @sshOpts $localCheck "claude@${ip}:/tmp/guest-checks.sh"
    if ($LASTEXITCODE -ne 0) { throw "scp guest-checks.sh failed" }
    & ssh.exe @sshOpts "claude@$ip" "bash /tmp/guest-checks.sh '$Tag' '$StripeMode'"
    $rc = $LASTEXITCODE
    & scp.exe @sshOpts "claude@${ip}:/tmp/cchouse-desktop.xwd.gz" (Join-Path $RunDir "desktop.xwd.gz")
    if ($LASTEXITCODE -ne 0) { $rc = 1 }
    exit $rc
  }

  "cleanup" {
    if (Test-Path $vmx) {
      & $vmr -T ws stop $vmx hard 2>$null | Out-Null
      Start-Sleep 2
      & $vmr -T ws deleteVM $vmx 2>$null | Out-Null
    }
    Remove-Item -Recurse -Force $RunDir -ErrorAction SilentlyContinue
    Write-Host "CLEANUP_OK"
  }
}
