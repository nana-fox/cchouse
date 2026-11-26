# Debian 13 黄金镜像构建说明（amd64 VMware / arm64 Tart）

黄金镜像由官方 Debian 13 云镜像 + `build-debian13-golden.sh` 自动化产出，不手工维护。

## 包含与排除
- **包含**：Debian 13 基础系统、Openbox + LightDM 自动登录、`dbus-user-session`、`tint2` 任务栏、`lxterminal`、Chromium、思源中文字体与 Emoji、`fcitx5` 简体拼音、`xclip`、`mihomo`、`nftables`、持久化 `systemd-networkd` IPv4 DHCP（`ClientIdentifier=mac`）、`open-vm-tools-desktop`（VMware）或 `spice-vdagent`（Tart）、amd64 OVA 另含 VirtualBox Guest Additions（与 `open-vm-tools` 按 DMI 二选一启动；编完模块后删除 ISO 与 `dkms`）、出厂默认全断 `nftables`、`cchouse-firstboot.service`。
- **排除**：任何实例凭据、Node.js（Claude Code 为独立原生二进制）、Claude Code 本身（首次开机由宿主拉取官方 npm 平台包校验 SHA-512 SRI 后随 `CCHOUSE` 配置 ISO 离线安装）。

## 构建步骤

### 1. Windows + VMware Workstation（`amd64` `.ova`）
```powershell
powershell -ExecutionPolicy Bypass -File guest-linux\image-builder\build-ova-from-cloud-image.ps1
# 国内网络需代理时加：-ProxyUrl http://127.0.0.1:7897
```
脚本自动下载并校验带日期钉死的 Debian 13 `amd64` `qcow2`、`mihomo`，以及 `virtualbox-manifest.json` 里的 Guest Additions ISO（官方 SHA-256，用完即删），转换 VMDK、启动无头构建机执行 `build-debian13-golden.sh` 与 `assert-empty-state.sh`，最后导出压缩 `.ova` 并打印 SHA-256。

### 2. Apple Silicon Mac + Tart（`arm64` `.tvm`）
```bash
USE_USTC_MIRROR=1 bash guest-linux/image-builder/build-tart-linux-from-cloud-image.sh
tart export cchouse-debian13-arm64 cchouse-debian13-arm64.tvm
shasum -a 256 cchouse-debian13-arm64.tvm
```

> `debian13-vmware.pkr.hcl` 与 `http/preseed.cfg` 仅作为备选离线 ISO 构建参考保留。

构建完成后上传 `.ova` 与 `.tvm` 至 GitHub Releases，并更新 [../image-manifest.json](../image-manifest.json)。
