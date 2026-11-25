<#
.SYNOPSIS
  在 Windows + VMware Workstation 上从 Debian 13 官方云镜像直接构建黄金 .ova（跳过 VNC preseed，约 5–8 分钟）。
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File guest-linux\image-builder\build-ova-from-cloud-image.ps1 -ProxyUrl http://127.0.0.1:7897
#>
param(
  [string]$WorkDir = "$env:USERPROFILE\cchouse-ova-build",
  [string]$ProxyUrl = "",
  [string]$CloudImageUrl = "https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-amd64.qcow2",
  [string]$CloudImageSha512 = "562a4614952657a6ab8a89cda56eaf1bc85c8dfea51155184267bb6b6361eff215baf6afa2f8c2c8c2bad328ac6c30eead80664286ef4c8538368aeb7f87104f",
  [string]$MihomoVersion = "v1.19.19",
  [string]$MihomoSha256 = "d066f77fd9210d5f280877d4e35fd84f4711c43bd8905335946b9b0b1f128094",
  [switch]$UseUstcMirror = $true
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$GuestLinuxDir = Split-Path -Parent $ScriptDir

function Find-Exe($candidates, $name) {
  foreach ($c in $candidates) {
    if ($c -and (Test-Path $c)) { return $c }
  }
  $cmd = Get-Command $name -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  return $null
}

$vmrun = Find-Exe @(
  "C:\Program Files\VMware\VMware Workstation\vmrun.exe",
  "C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe"
) "vmrun"
$ovftool = Find-Exe @(
  "C:\Program Files\VMware\VMware Workstation\OVFTool\ovftool.exe",
  "C:\Program Files (x86)\VMware\VMware Workstation\OVFTool\ovftool.exe"
) "ovftool"
if (-not $vmrun -or -not $ovftool) { throw "未找到 vmrun.exe 或 ovftool.exe，请先安装 VMware Workstation Pro" }

$qemuImg = Find-Exe @("C:\Program Files\qemu\qemu-img.exe") "qemu-img"
if (-not $qemuImg) {
  Write-Host "== 未检测到 qemu-img，正在通过 winget 安装 QEMU..."
  winget install -e --id SoftwareFreedomConservancy.QEMU --silent --accept-package-agreements --accept-source-agreements
  $qemuImg = Find-Exe @("C:\Program Files\qemu\qemu-img.exe") "qemu-img"
  if (-not $qemuImg) { throw "安装 QEMU 后仍未找到 qemu-img.exe" }
}

New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
$qcow2 = Join-Path $WorkDir "debian-13-generic-amd64.qcow2"
$mihomoGz = Join-Path $WorkDir "mihomo-linux-amd64-$MihomoVersion.gz"
$mihomoUrl = "https://github.com/MetaCubeX/mihomo/releases/download/$MihomoVersion/mihomo-linux-amd64-$MihomoVersion.gz"

$curlProxy = @()
if ($ProxyUrl) { $curlProxy = @("--proxy", $ProxyUrl) }

if (-not (Test-Path $qcow2)) {
  Write-Host "== [1/7] 下载 Debian 13 官方云镜像 (~320 MB)..."
  & curl.exe -fL @curlProxy $CloudImageUrl -o $qcow2
  if ($LASTEXITCODE -ne 0) { throw "下载 Debian 13 qcow2 失败" }
}
$got512 = (Get-FileHash -Algorithm SHA512 $qcow2).Hash.ToLower()
if ($got512 -ne $CloudImageSha512.ToLower()) {
  throw "Debian 13 qcow2 SHA-512 校验失败：$got512"
}
Write-Host "  OK  Debian 13 qcow2 SHA-512 校验通过"

if (-not (Test-Path $mihomoGz)) {
  Write-Host "== [2/7] 宿主机下载 mihomo $MihomoVersion..."
  & curl.exe -fL @curlProxy $mihomoUrl -o $mihomoGz
  if ($LASTEXITCODE -ne 0) { throw "下载 mihomo 失败" }
}
$got256 = (Get-FileHash -Algorithm SHA256 $mihomoGz).Hash.ToLower()
if ($got256 -ne $MihomoSha256.ToLower()) {
  throw "mihomo SHA-256 校验失败：$got256"
}
Write-Host "  OK  mihomo SHA-256 校验通过"

$vmDir = Join-Path $WorkDir "vm"
if (Test-Path $vmDir) { Remove-Item -Recurse -Force $vmDir }
New-Item -ItemType Directory -Force -Path $vmDir | Out-Null

Write-Host "== [3/7] 转换 qcow2 -> VMDK (24 GiB)..."
$workQcow = Join-Path $vmDir "work.qcow2"
Copy-Item $qcow2 $workQcow
& $qemuImg resize $workQcow 24G
if ($LASTEXITCODE -ne 0) { throw "qemu-img resize 失败" }
$vmdk = Join-Path $vmDir "cchouse-debian13.vmdk"
& $qemuImg convert -f qcow2 -O vmdk -o adapter_type=lsilogic,subformat=monolithicSparse $workQcow $vmdk
if ($LASTEXITCODE -ne 0) { throw "qemu-img convert 失败" }
Remove-Item $workQcow -Force

Write-Host "== [4/7] 生成一次性 cidata ISO 与临时 SSH 密钥..."
$sshKey = Join-Path $vmDir "build_ed25519"
# PowerShell 5.1 会吞掉传给原生 exe 的空字符串 ""，改经 cmd /c 传递 -N ""
& cmd.exe /c "ssh-keygen.exe -q -t ed25519 -N `"`" -f `"$sshKey`""
if ($LASTEXITCODE -ne 0 -or -not (Test-Path "$sshKey.pub")) { throw "ssh-keygen 生成临时密钥失败" }
$pubKey = (Get-Content "$sshKey.pub" -Raw).Trim()

$ciDir = Join-Path $vmDir "cidata"
New-Item -ItemType Directory -Force -Path $ciDir | Out-Null
$metaData = "instance-id: cchouse-build-01`nlocal-hostname: debian`n"
$userData = @"
#cloud-config
disable_root: false
ssh_pwauth: false
users:
  - name: root
    ssh_authorized_keys:
      - $pubKey
growpart:
  mode: auto
  devices: ['/']
resize_rootfs: true
"@
[System.IO.File]::WriteAllText((Join-Path $ciDir "meta-data"), $metaData, [System.Text.UTF8Encoding]::new($false))
[System.IO.File]::WriteAllText((Join-Path $ciDir "user-data"), $userData, [System.Text.UTF8Encoding]::new($false))

Add-Type -TypeDefinition @"
using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
public static class CchouseCidataWriter {
    public static void Write(object comStream, string dstPath) {
        IStream stream = (IStream)comStream;
        byte[] buf = new byte[65536];
        IntPtr readPtr = Marshal.AllocCoTaskMem(sizeof(int));
        try {
            using (FileStream fs = File.Create(dstPath)) {
                while (true) {
                    stream.Read(buf, buf.Length, readPtr);
                    int read = Marshal.ReadInt32(readPtr);
                    if (read <= 0) break;
                    fs.Write(buf, 0, read);
                }
            }
        } finally {
            Marshal.FreeCoTaskMem(readPtr);
        }
    }
}
"@
$ciIso = Join-Path $vmDir "cidata.iso"
$img = New-Object -ComObject IMAPI2FS.MsftFileSystemImage
$img.VolumeName = "cidata"
$img.FileSystemsToCreate = 3
$img.Root.AddTree($ciDir, $false)
$res = $img.CreateResultImage()
[CchouseCidataWriter]::Write($res.ImageStream, $ciIso)

$vmx = Join-Path $vmDir "cchouse-debian13-amd64.vmx"
$vmxContent = @"
.encoding = "UTF-8"
config.version = "8"
virtualHW.version = "16"
displayName = "cchouse-debian13-amd64"
guestOS = "debian12-64"
numvcpus = "2"
memsize = "2048"
pciBridge0.present = "TRUE"
pciBridge4.present = "TRUE"
pciBridge4.virtualDev = "pcieRootPort"
pciBridge4.functions = "8"
pciBridge5.present = "TRUE"
pciBridge5.virtualDev = "pcieRootPort"
pciBridge5.functions = "8"
pciBridge6.present = "TRUE"
pciBridge6.virtualDev = "pcieRootPort"
pciBridge6.functions = "8"
pciBridge7.present = "TRUE"
pciBridge7.virtualDev = "pcieRootPort"
pciBridge7.functions = "8"
vmci0.present = "TRUE"
hpet0.present = "TRUE"
msg.autoAnswer = "TRUE"
scsi0.present = "TRUE"
scsi0.virtualDev = "lsilogic"
scsi0:0.present = "TRUE"
scsi0:0.fileName = "cchouse-debian13.vmdk"
ide1:0.present = "TRUE"
ide1:0.deviceType = "cdrom-image"
ide1:0.fileName = "cidata.iso"
ide1:0.startConnected = "TRUE"
ethernet0.present = "TRUE"
ethernet0.connectionType = "nat"
ethernet0.virtualDev = "vmxnet3"
ethernet0.addressType = "generated"
ethernet0.startConnected = "TRUE"
usb.present = "TRUE"
mks.enable3d = "FALSE"
"@
[System.IO.File]::WriteAllText($vmx, $vmxContent, [System.Text.UTF8Encoding]::new($false))

Write-Host "== [5/7] 启动临时构建机并等待 SSH..."
& $vmrun -T ws start $vmx nogui
if ($LASTEXITCODE -ne 0) { throw "vmrun start 失败" }

# 轮询 VMware NAT DHCP 租约 / getGuestIPAddress / arp 获取客机 IP 并探测 SSH 22 端口（最多 150 秒）
$guestIp = ""
$sshReady = $false
$sshOpts = @("-i", $sshKey, "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=NUL", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes")
$leaseFile = "C:\ProgramData\VMware\vmnetdhcp.leases"
for ($i = 0; $i -lt 50; $i++) {
  Start-Sleep 3
  $ipOut = & $vmrun -T ws getGuestIPAddress $vmx 2>$null
  if ($LASTEXITCODE -eq 0 -and $ipOut -match '^\d+\.\d+\.\d+\.\d+$') {
    $guestIp = $ipOut.Trim()
  } else {
    $macLine = (Select-String -Path $vmx -Pattern 'ethernet0\.generatedAddress\s*=\s*"([^"]+)"' -ErrorAction SilentlyContinue)
    if ($macLine) {
      $macColon = $macLine.Matches[0].Groups[1].Value.ToLower()
      if (Test-Path $leaseFile) {
        $leaseText = Get-Content $leaseFile -Raw -ErrorAction SilentlyContinue
        $blocks = [regex]::Matches($leaseText, 'lease\s+(\d+\.\d+\.\d+\.\d+)\s*\{[^}]*hardware\s+ethernet\s+([0-9a-fA-F:]+);', [System.Text.RegularExpressions.RegexOptions]::Singleline)
        foreach ($b in $blocks) {
          if ($b.Groups[2].Value.ToLower() -eq $macColon) {
            $guestIp = $b.Groups[1].Value
          }
        }
      }
      if (-not $guestIp) {
        $macDash = $macColon.Replace(":", "-")
        $arpHit = (& arp.exe -a | Select-String $macDash | Select-Object -First 1)
        if ($arpHit -and $arpHit.Line -match '(\d+\.\d+\.\d+\.\d+)') {
          $guestIp = $Matches[1]
        }
      }
    }
  }
  if ($guestIp) {
    & ssh.exe @sshOpts "root@$guestIp" "true" 2>$null
    if ($LASTEXITCODE -eq 0) { $sshReady = $true; break }
  }
}
if (-not $sshReady) { throw "150 秒内未获取到客机 IP 或 SSH 未就绪（当前 IP: $guestIp）" }
Write-Host "  OK  客机 SSH 已就绪：$guestIp"

Write-Host "== [6/7] 注入脚本与 mihomo，执行 build-debian13-golden.sh..."
& ssh.exe @sshOpts "root@$guestIp" "mkdir -p /tmp/cchouse-guest-linux"
& scp.exe @sshOpts -r "$GuestLinuxDir\*" "root@${guestIp}:/tmp/cchouse-guest-linux/"
& scp.exe @sshOpts $mihomoGz "root@${guestIp}:/tmp/mihomo.gz"

$mirrorCmd = ""
if ($UseUstcMirror) {
  $mirrorCmd = "sed -i 's|deb\.debian\.org|mirrors.ustc.edu.cn|g; s|security\.debian\.org|mirrors.ustc.edu.cn|g' /etc/apt/sources.list /etc/apt/sources.list.d/* 2>/dev/null || true; "
}
$buildCmd = "${mirrorCmd}MIHOMO_VERSION=$MihomoVersion MIHOMO_SHA256=$MihomoSha256 MIHOMO_LOCAL_GZ=/tmp/mihomo.gz bash /tmp/cchouse-guest-linux/image-builder/build-debian13-golden.sh && rm -rf /tmp/cchouse-guest-linux /tmp/mihomo.gz"
& ssh.exe @sshOpts "root@$guestIp" $buildCmd
if ($LASTEXITCODE -ne 0) { throw "build-debian13-golden.sh 执行失败" }

Write-Host "== [7/7] 关机、清理临时光驱并导出黄金 .ova..."
& ssh.exe @sshOpts "root@$guestIp" "systemctl poweroff" 2>$null
for ($i = 0; $i -lt 30; $i++) {
  Start-Sleep 2
  $running = & $vmrun -T ws list
  if (-not ($running -match [regex]::Escape($vmx))) { break }
}

# 清理 cidata 光驱，设置出厂物理断网与禁共享文件夹
$vmxFinal = (Get-Content $vmx -Raw) `
  -replace 'ide1:0\.fileName = "cidata\.iso"', 'ide1:0.fileName = ""' `
  -replace 'ide1:0\.startConnected = "TRUE"', 'ide1:0.startConnected = "FALSE"' `
  -replace 'ethernet0\.startConnected = "TRUE"', 'ethernet0.startConnected = "FALSE"'
$vmxFinal += "`nisolation.tools.hgfs.disable = `"TRUE`"`nisolation.tools.dnd.disable = `"TRUE`"`n"
[System.IO.File]::WriteAllText($vmx, $vmxFinal, [System.Text.UTF8Encoding]::new($false))
Remove-Item -Force $ciIso, $sshKey, "$sshKey.pub" -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force $ciDir -ErrorAction SilentlyContinue

$ovaOut = Join-Path $WorkDir "cchouse-debian13-amd64.ova"
if (Test-Path $ovaOut) { Remove-Item -Force $ovaOut }
& $ovftool --compress=9 $vmx $ovaOut
if ($LASTEXITCODE -ne 0) { throw "ovftool 导出 .ova 失败" }

$ovaSha = (Get-FileHash -Algorithm SHA256 $ovaOut).Hash.ToLower()
$ovaMb = [math]::Round((Get-Item $ovaOut).Length / 1MB, 1)
Write-Host ""
Write-Host "=== 黄金镜像构建完成 ==="
Write-Host "OVA:    $ovaOut"
Write-Host "SIZE:   $ovaMb MB"
Write-Host "SHA256: $ovaSha"
