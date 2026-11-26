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

function Get-CchouseInstallFreeGb {
  # 安装目录：%LOCALAPPDATA%\cchouse。目录还不存在时检查其已存在的父目录，不创建。
  $root = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { Join-Path $env:USERPROFILE "AppData\Local" }
  $probe = Join-Path $root "cchouse"
  while (-not (Test-Path -LiteralPath $probe)) {
    $parent = Split-Path -Parent $probe
    if (-not $parent -or $parent -eq $probe) { break }
    $probe = $parent
  }
  $item = Get-Item -LiteralPath $probe
  $drive = Get-PSDrive -Name $item.PSDrive.Name
  if ($null -eq $drive.Free) { return $null }
  # 与 preflight.py 一样按十进制 GB 检查，先比较后取显示精度。
  return $drive.Free / 1000000000.0
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
# 与 preflight.py 的 VirtualBox 阈值保持一致。
$diskMinGb = 7
$diskRecommendedGb = 10
$diskFreeGb = $null
$nextAction = "install_python_or_uv"
if ($Route -in @("auto", "virtualbox-linux")) {
  try {
    $diskFreeGb = Get-CchouseInstallFreeGb
    if ($null -ne $diskFreeGb) {
      $freeDisplay = [math]::Floor($diskFreeGb * 10) / 10
      if ($diskFreeGb -lt $diskMinGb) {
        $errors += "INSUFFICIENT_DISK: 安装目录所在卷剩余 $freeDisplay GB，完整安装至少需要 $diskMinGb GB（含下载的镜像）。请清理该卷空间后再安装。"
        $nextAction = "free_disk"
      } elseif ($diskFreeGb -lt $diskRecommendedGb) {
        $warnings += "LOW_DISK: 安装目录所在卷剩余 $freeDisplay GB，建议至少 $diskRecommendedGb GB。"
      }
      $diskFreeGb = [math]::Round($diskFreeGb, 1)
    }
  } catch {
    $warnings += "DISK_CHECK_FAILED: 未能读取安装目录所在卷的剩余空间。"
  }
}
$payload = [ordered]@{
  ok = $false
  status = "HUMAN_WAIT"
  host = [ordered]@{
    os = "Windows"
    arch = $env:PROCESSOR_ARCHITECTURE
    python = $null
    disk_free_gb = $diskFreeGb
    disk_min_gb = $diskMinGb
    disk_recommended_gb = $diskRecommendedGb
    disk_estimate = $false
  }
  route = $Route
  errors = $errors
  warnings = $warnings
  next_action = $nextAction
}
$payload | ConvertTo-Json -Depth 6
exit 1
