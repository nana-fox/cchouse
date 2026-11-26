param(
  [Parameter(Mandatory=$true)][ValidateSet("import","firstboot","reboot","verify","desktop","cleanup")] [string]$Action,
  [Parameter(Mandatory=$true)][string]$RunDir,
  [Parameter(Mandatory=$true)][string]$VmName,
  [string]$OvaPath = "",
  [int]$SshPort = 0,
  [string]$Tag = "e2egtr",
  [string]$StripeMode = "front"
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
. (Join-Path $PSScriptRoot "vm-virtualbox.ps1")
$vmDir = Join-Path $RunDir "vm"
$keyPath = Join-Path $RunDir "id_ed25519"
$khPath = Join-Path $RunDir "known_hosts"
$hostKeyLog = Join-Path $RunDir "logs\hostkey.serial"
$ipFile = Join-Path $RunDir "guest_ip.txt"
$seedIso = Join-Path $RunDir "seed.iso"

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

if ($VmName -notmatch '^claude-gtr-[0-9]{10}$') { throw "Invalid GTR test VM name" }
switch ($Action) {
  "import" {
    if (Test-CchouseVmRegistered $VmName) { throw "GTR test VM already exists" }
    Import-CchouseVBoxOva -OvaPath $OvaPath -Name $VmName -DestDir $vmDir -SshPort $SshPort -HostKeyLog $hostKeyLog
  }

  "firstboot" {
    Invoke-CchouseVBoxFirstboot -Name $VmName -SeedIso $seedIso -Seconds 180 -HostKeyLog $hostKeyLog
  }

  "reboot" {
    Invoke-CchouseVBoxBoot -Name $VmName -SeedIso $seedIso -KeyPath $keyPath -KnownHostsPath $khPath `
      -IpFile $ipFile -SshPort $SshPort -HostKeyLog $hostKeyLog
  }

  "verify" {
    $bash = Find-GitBash
    $verifySh = (Join-Path $RunDir "verify-linux.sh") -replace '\\', '/'
    $envPath = (Join-Path $RunDir "gtr-verify.env") -replace '\\', '/'
    $env:PATH = "C:\Windows\System32\OpenSSH;$env:PATH"
    & $bash "$verifySh" "$envPath" --fail-closed
    exit $LASTEXITCODE
  }

  "desktop" {
    $verify = @(Get-CchouseSshVerifyOptions -KnownHostsPath $khPath -Pinned:$true)
    $sshOpts = @("-F", "none", "-p", "$SshPort", "-i", $keyPath) + $verify
    $scpOpts = @("-F", "none", "-P", "$SshPort", "-i", $keyPath) + $verify
    $target = "claude@127.0.0.1"
    $scp = Join-Path (Split-Path $script:CchouseSystem32Ssh -Parent) "scp.exe"
    $localCheck = Join-Path $RunDir "guest-checks.sh"
    $raw = (Get-Content $localCheck -Raw) -replace "`r`n", "`n"
    [System.IO.File]::WriteAllText($localCheck, $raw, [System.Text.UTF8Encoding]::new($false))
    Invoke-CchouseNativeChecked -FilePath $scp -ArgumentList ($scpOpts + @($localCheck, "${target}:/tmp/guest-checks.sh")) `
      -FailureMessage "scp guest-checks.sh failed"
    $check = Invoke-CchouseNative -FilePath $script:CchouseSystem32Ssh `
      -ArgumentList ($sshOpts + @($target, "bash /tmp/guest-checks.sh '$Tag' '$StripeMode'")) -ShowStderr
    $check.Output
    Invoke-CchouseNativeChecked -FilePath $scp `
      -ArgumentList ($scpOpts + @("${target}:/tmp/cchouse-desktop.xwd.gz", (Join-Path $RunDir "desktop.xwd.gz"))) `
      -FailureMessage "scp desktop screenshot failed"
    exit $check.ExitCode
  }

  "cleanup" {
    if (Test-CchouseVmRegistered $VmName) {
      $info = @(Get-CchouseShowvminfoLines -Name $VmName)
      $configs = @($info | Where-Object { $_ -like 'CfgFile=*' })
      $expected = [IO.Path]::GetFullPath((Join-Path $vmDir "$VmName\$VmName.vbox"))
      if ($configs.Count -ne 1 -or $configs[0].Substring(8).Trim('"').Replace('\\','\') -ine $expected) {
        throw "Test VM configuration is outside the E2E run directory"
      }
      Remove-CchouseVBoxVM -Name $VmName
    }
    Remove-Item -LiteralPath $RunDir -Recurse -Force
    Write-Host "CLEANUP_OK"
  }
}
