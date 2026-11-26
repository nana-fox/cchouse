<#
.SYNOPSIS
  Windows 宿主机 preflight 入口（避开微软商店 python3 占位程序，强制 UTF-8，检测 Win11 VBS）。
#>
param(
  [string]$Route = "virtualbox-linux",
  [string]$EnvPath = "",
  [string]$StatePath = ""
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PreflightPy = Join-Path $ScriptDir "preflight.py"
. (Join-Path $ScriptDir "windows-native.ps1")

function Get-VbsNote {
  try {
    $dg = Get-CimInstance -ClassName Win32_DeviceGuard -Namespace root\Microsoft\Windows\DeviceGuard -ErrorAction SilentlyContinue
    if ($dg -and $dg.VirtualizationBasedSecurityStatus -eq 2) {
      return "VBS_RUNNING: 检测到 Win11 已启用 VBS/内存完整性，VirtualBox 可能通过 Windows Hypervisor Platform 运行（性能可能降低，请勿关闭 VBS）"
    }
  } catch {}
  return ""
}

function Find-RealPython {
  $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
  if ($pyLauncher) {
    return @("py", @("-3", "-I", "-X", "utf8", $PreflightPy, "--route", $Route))
  }
  foreach ($name in @("python3", "python")) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -and ($cmd.Source -notlike "*\WindowsApps\*")) {
      return @($cmd.Source, @("-I", "-X", "utf8", $PreflightPy, "--route", $Route))
    }
  }
  $uvCmd = Get-Command uv -ErrorAction SilentlyContinue
  if ($uvCmd) {
    return @($uvCmd.Source, @("run", "python", "-I", "-X", "utf8", $PreflightPy, "--route", $Route))
  }
  return $null
}

$vbsNote = Get-VbsNote
$found = Find-RealPython
if ($found) {
  $pyExe = $found[0]
  $pyArgs = $found[1]
  if ($EnvPath) { $pyArgs += @("--env", $EnvPath) }
  if ($StatePath) { $pyArgs += @("--state", $StatePath) }
  $result = Invoke-CchouseNative -FilePath $pyExe -ArgumentList $pyArgs -ShowStderr
  $rc = $result.ExitCode
  $joined = ($result.Output -join "`n")
  if ($vbsNote -and $joined) {
    try {
      $obj = $joined | ConvertFrom-Json
      $obj.warnings = @($obj.warnings) + $vbsNote
      $obj | ConvertTo-Json -Depth 6
      exit $rc
    } catch {}
  }
  Write-Output $joined
  exit $rc
}

$warnings = @()
if ($vbsNote) { $warnings += $vbsNote }
$errors = @("MISSING_PYTHON: Windows 未检测到真实 Python 3（已排除 WindowsApps 商店占位）或 uv，请先安装后重跑 preflight")
$payload = [ordered]@{
  ok = $false
  status = "HUMAN_WAIT"
  host = [ordered]@{
    os = "Windows"
    arch = $env:PROCESSOR_ARCHITECTURE
    python = $null
  }
  route = $Route
  errors = $errors
  warnings = $warnings
  next_action = "install_python_or_uv"
}
$payload | ConvertTo-Json -Depth 6
exit 1
