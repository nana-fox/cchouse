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
# VM lifecycle helpers shared with scripts/cchouse.py up (uploaded next to this script by run-gtr.sh)
. (Join-Path $PSScriptRoot "vm-vmware.ps1")
$vmr = Get-CchouseVmrun
$vmDir = Join-Path $RunDir "vm"
$vmx = Join-Path $vmDir "$VmName\$VmName.vmx"
$keyPath = Join-Path $RunDir "id_ed25519"
$khPath = Join-Path $RunDir "known_hosts"
$ipFile = Join-Path $RunDir "guest_ip.txt"
$hostKeyLog = Join-Path $RunDir "hostkey.serial"

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

switch ($Action) {
  "clone" {
    Fix-KeyAcl $keyPath
    New-Item -ItemType Directory -Force -Path $vmDir | Out-Null
    if ($Mode -eq "release") {
      Import-CchouseOva $OvaPath $VmName $vmDir
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
        $baseName = [System.IO.Path]::GetFileNameWithoutExtension($BaseVmx)
        $baseParent = Split-Path (Split-Path $BaseVmx -Parent) -Parent
        Import-CchouseOva $OvaPath $baseName $baseParent
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
    Invoke-CchouseFirstboot $vmx (Join-Path $RunDir "seed.iso") 55 $hostKeyLog
  }

  "reboot" {
    $ip = Invoke-CchouseBoot $vmx (Join-Path $RunDir "seed.iso") $keyPath $khPath $ipFile $hostKeyLog
    $envFile = Join-Path $RunDir "gtr-verify.env"
    Add-Content -Path $envFile -Value "`nGUEST_IP=$ip" -Encoding UTF8
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
