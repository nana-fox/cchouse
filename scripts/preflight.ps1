<#
.SYNOPSIS
  Windows 宿主机 preflight 入口（避开微软商店 python3 占位程序，强制 UTF-8，检测 Win11 VBS）。
#>
param(
  [string]$Route = "vmware-linux",
  [string]$EnvPath = "",
  [string]$StatePath = ""
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PreflightPy = Join-Path $ScriptDir "preflight.py"

function Get-VbsNote {
  try {
    $dg = Get-CimInstance -ClassName Win32_DeviceGuard -Namespace root\Microsoft\Windows\DeviceGuard -ErrorAction SilentlyContinue
    if ($dg -and $dg.VirtualizationBasedSecurityStatus -eq 2) {
      return "VBS_RUNNING: 检测到 Win11 已启用 VBS/内存完整性，VMware 将自动运行于 WHP 模式（性能略降但可用，请勿关闭 VBS）"
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
  # 1GB 为 1024^3，与 preflight.py 的 disk_free_gb 相同。
  return [math]::Round($drive.Free / 1GB, 1)
}

$vbsNote = Get-VbsNote
$found = Find-RealPython
if ($found) {
  $pyExe = $found[0]
  $pyArgs = $found[1]
  if ($EnvPath) { $pyArgs += @("--env", $EnvPath) }
  if ($StatePath) { $pyArgs += @("--state", $StatePath) }
  $outLines = & $pyExe @pyArgs
  $rc = $LASTEXITCODE
  $joined = ($outLines -join "`n")
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

$vmrunFound = (Test-Path "C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe") -or `
              (Test-Path "C:\Program Files\VMware\VMware Workstation\vmrun.exe")
$warnings = @()
if ($vbsNote) { $warnings += $vbsNote }
$errors = @("MISSING_PYTHON: Windows 未检测到真实 Python 3（已排除 WindowsApps 商店占位）或 uv，请先安装后重跑 preflight")
if (-not $vmrunFound) {
  $errors += "MISSING_VMWARE: 未检测到 VMware Workstation Pro (vmrun.exe)"
}
# 与 preflight.py 保持一致。磁盘数字不看 vmrun 在不在。
# VirtualBox 默认路线：卷减少 6196707328 字节，十进制 GB 向上取整为 7/10。#39 接入前本脚本不按它检查。
$virtualBoxDiskMinGb = 7
$virtualBoxDiskRecommendedGb = 10
# 手动 vmware-linux：卷减少 9418551296 字节（8.772 GiB），向上取整为 9/12。当前 $Route 仍是这一条。
$vmwareDiskMinGb = 9
$vmwareDiskRecommendedGb = 12
$diskMinGb = $vmwareDiskMinGb
$diskRecommendedGb = $vmwareDiskRecommendedGb
$diskFreeGb = $null
try {
  $diskFreeGb = Get-CchouseInstallFreeGb
  if ($null -ne $diskFreeGb -and $diskFreeGb -lt $diskMinGb) {
    $errors += "INSUFFICIENT_DISK: 安装目录所在卷剩余 $diskFreeGb GB，完整安装至少需要 $diskMinGb GB（含下载的镜像）。请清理该卷空间后再安装。"
  } elseif ($null -ne $diskFreeGb -and $diskFreeGb -lt $diskRecommendedGb) {
    $warnings += "LOW_DISK: 安装目录所在卷剩余 $diskFreeGb GB，可以安装。建议至少 $diskRecommendedGb GB。"
  }
} catch {
  $warnings += "DISK_CHECK_FAILED: 未能读取安装目录所在卷的剩余空间。"
}

$nextAction = if ($vmrunFound) { "install_python_or_uv" } else { "install_python_or_uv_and_vmware" }
$payload = [ordered]@{
  ok = $false
  status = "HUMAN_WAIT"
  host = [ordered]@{
    os = "Windows"
    arch = $env:PROCESSOR_ARCHITECTURE
    python = $null
    vmrun_found = $vmrunFound
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
