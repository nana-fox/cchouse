# Shared Windows host-key helpers for vm-virtualbox.ps1.
# Dot-source only. The guest prints one public CCHOUSE_HOSTKEY line on the
# serial log during firstboot. The host writes known_hosts from that line.
# Do not print the serial log or any private key.

. (Join-Path $PSScriptRoot "windows-native.ps1")

$script:CchouseSystem32Ssh = "C:\Windows\System32\OpenSSH\ssh.exe"
$script:CchouseSshWaitSeconds = 180

function Fix-KeyAcl {
  param([string]$Path)
  if ($Path -and (Test-Path -LiteralPath $Path)) {
    Invoke-CchouseNativeChecked -FilePath "icacls.exe" -ArgumentList @(
      $Path, "/inheritance:r", "/grant:r", "${env:USERNAME}:(R,W)"
    ) -FailureMessage "SSH key ACL update failed" | Out-Null
  }
}

function ConvertTo-CchouseOpenSshPath {
  param([string]$Path)
  if ([string]::IsNullOrWhiteSpace($Path)) { return "" }
  if ($Path.Contains('"')) { throw "known_hosts path is invalid" }
  return ($Path -replace '\\', '/')
}

function Initialize-CchouseHostKeyLog {
  param([string]$Path)
  if ([string]::IsNullOrWhiteSpace($Path)) { return }
  $parent = Split-Path -Parent $Path
  if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
  if (-not (Test-Path -LiteralPath $Path)) {
    [System.IO.File]::WriteAllText($Path, "", [System.Text.UTF8Encoding]::new($false))
  }
}

function Read-CchouseSerialText {
  param([string]$Path)
  if ([string]::IsNullOrWhiteSpace($Path)) { return "" }
  if (-not (Test-Path -LiteralPath $Path)) { return "" }
  try {
    $bytes = [System.IO.File]::ReadAllBytes($Path)
  } catch {
    throw "host key serial log is not readable"
  }
  return [System.Text.Encoding]::UTF8.GetString($bytes)
}

function Get-CchouseHostKeyFromSerialText {
  param([string]$Text)
  if ([string]::IsNullOrWhiteSpace($Text)) { return $null }
  $found = $null
  foreach ($raw in ($Text -split '\r?\n')) {
    $line = $raw.Trim()
    if ($line -match '^CCHOUSE_HOSTKEY (ssh-ed25519) ([A-Za-z0-9+/]+=*)$') {
      $keyType = $Matches[1]
      $keyData = $Matches[2]
      $key = "$keyType $keyData"
      if ($null -eq $found) {
        $found = $key
        continue
      }
      if ($found -ne $key) {
        throw "serial log has conflicting CCHOUSE_HOSTKEY lines"
      }
    }
  }
  return $found
}

function Format-CchouseKnownHostsLine {
  param([string]$HostName, [int]$Port, [string]$Key)
  if ([string]::IsNullOrWhiteSpace($HostName) -or [string]::IsNullOrWhiteSpace($Key)) {
    throw "known_hosts host and key are required"
  }
  if ($Port -gt 0 -and $Port -ne 22) {
    return ('[{0}]:{1} {2}' -f $HostName, $Port, $Key)
  }
  return ('{0} {1}' -f $HostName, $Key)
}

function Resolve-CchouseHostKeyTrust {
  param([string]$SerialText, [string]$HostName, [int]$Port)
  $key = Get-CchouseHostKeyFromSerialText -Text $SerialText
  if ([string]::IsNullOrWhiteSpace($key)) {
    return [pscustomobject]@{ Pinned = $false; KnownHostsLine = "" }
  }
  $line = Format-CchouseKnownHostsLine -HostName $HostName -Port $Port -Key $key
  return [pscustomobject]@{ Pinned = $true; KnownHostsLine = $line }
}

function Write-CchouseHostKeyTrustMark {
  param([string]$SerialLogPath, [bool]$Pinned)
  if ([string]::IsNullOrWhiteSpace($SerialLogPath)) { return }
  $parent = Split-Path -Parent $SerialLogPath
  if ([string]::IsNullOrWhiteSpace($parent)) { return }
  New-Item -ItemType Directory -Force -Path $parent | Out-Null
  $path = Join-Path $parent "hostkey.trust"
  $value = "serial"
  if (-not $Pinned) { $value = "accept-new" }
  [System.IO.File]::WriteAllText($path, ($value + "`n"), [System.Text.UTF8Encoding]::new($false))
}

function Write-CchouseKnownHostsFile {
  param([string]$Path, [string]$Line)
  if ([string]::IsNullOrWhiteSpace($Path) -or [string]::IsNullOrWhiteSpace($Line)) {
    throw "known_hosts path and line are required"
  }
  $parent = Split-Path -Parent $Path
  if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
  $text = $Line.TrimEnd() + "`n"
  [System.IO.File]::WriteAllText($Path, $text, [System.Text.UTF8Encoding]::new($false))
}

function Get-CchouseSshVerifyOptions {
  param(
    [string]$KnownHostsPath,
    [bool]$Pinned,
    [int]$ConnectTimeout = 5
  )
  if ([string]::IsNullOrWhiteSpace($KnownHostsPath)) { throw "known_hosts path is required" }
  if ($ConnectTimeout -lt 1) { $ConnectTimeout = 1 }
  if ($ConnectTimeout -gt 5) { $ConnectTimeout = 5 }
  $mode = "yes"
  if (-not $Pinned) { $mode = "accept-new" }
  if ($mode -ne "yes" -and $mode -ne "accept-new") { throw "unsupported host key check mode" }
  $kh = ConvertTo-CchouseOpenSshPath -Path $KnownHostsPath
  "-o"
  "StrictHostKeyChecking=$mode"
  "-o"
  ('UserKnownHostsFile="{0}"' -f $kh)
  "-o"
  "ConnectTimeout=$ConnectTimeout"
  "-o"
  "ConnectionAttempts=1"
  "-o"
  "BatchMode=yes"
  "-o"
  "LogLevel=ERROR"
}

function Test-CchouseHostKeyMismatch {
  param([string]$Stderr)
  if ([string]::IsNullOrWhiteSpace($Stderr)) { return $false }
  if ($Stderr -match 'REMOTE HOST IDENTIFICATION HAS CHANGED') { return $true }
  if ($Stderr -match 'Host key verification failed') { return $true }
  return $false
}

function Invoke-CchouseSshTrue {
  param(
    [string]$Target,
    [string[]]$SshOption,
    [int]$TimeoutMs = 10000
  )
  if (-not (Test-Path -LiteralPath $script:CchouseSystem32Ssh)) {
    throw "System32 OpenSSH ssh.exe not found"
  }
  $startInfo = [System.Diagnostics.ProcessStartInfo]::new($script:CchouseSystem32Ssh)
  # Use the Windows argv quoting rules on both PowerShell 5.1 and 7. Preserve
  # the quotes SSH needs inside UserKnownHostsFile="path with spaces".
  $startInfo.Arguments = (($SshOption + @($Target, "true") | ForEach-Object {
    '"' + ($_ -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
  }) -join ' ')
  $startInfo.UseShellExecute = $false
  $startInfo.RedirectStandardError = $true
  $process = [System.Diagnostics.Process]::Start($startInfo)
  try {
    $stderr = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit($TimeoutMs)) {
      try { $process.Kill() } catch { if (-not $process.HasExited) { throw } }
      return $false
    }
    if (Test-CchouseHostKeyMismatch -Stderr ($stderr.GetAwaiter().GetResult())) {
      throw "host key verification failed for $Target"
    }
    return ($process.ExitCode -eq 0)
  } finally {
    $process.Dispose()
  }
}
