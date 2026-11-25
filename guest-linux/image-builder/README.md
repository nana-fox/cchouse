# Debian 13 (amd64) 黄金镜像构建说明

黄金镜像由配方自动化产出，不手工维护。

## 包含与排除
- **包含**：Debian 13 基础系统、Openbox、Chromium、Node.js LTS、`mihomo`、`nftables`、`open-vm-tools`、出厂默认全断 `nftables`、`cchouse-firstboot.service`。
- **排除**：任何实例凭据、Claude Code（首次开机时由宿主拉取官方 npm tarball 校验 integrity 后随 `CCHOUSE` 配置 ISO 离线安装）。

## 构建步骤（Packer）

```bash
cd guest-linux/image-builder
packer init debian13-vmware.pkr.hcl
packer build \
  -var "debian_iso_url=<DEBIAN13_ISO_URL>" \
  -var "debian_iso_checksum=sha256:<ISO_SHA256>" \
  -var "mihomo_sha256=<MIHOMO_GZ_SHA256>" \
  debian13-vmware.pkr.hcl
```

构建完成后使用 `ovftool` 导出为 `.ova`，计算 `sha256sum`（可选 minisign 签名），上传至外部对象存储（如 Cloudflare R2 / GitHub Releases），并更新 [../image-manifest.json](../image-manifest.json)。
