#!/bin/bash
# Debian 13 (amd64) 黄金镜像构建脚本（由 Packer vmware-iso 或干净 Debian 13 客机以 root 执行）
# 不包含任何凭据，不预装 Claude Code（由首次开机从宿主校验后的 npm tarball 离线安装）。
set -euo pipefail

MIHOMO_VERSION="${MIHOMO_VERSION:-v1.19.19}"
MIHOMO_SHA256="${MIHOMO_SHA256:-}"
MIHOMO_URL="${MIHOMO_URL:-https://github.com/MetaCubeX/mihomo/releases/download/${MIHOMO_VERSION}/mihomo-linux-amd64-${MIHOMO_VERSION}.gz}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
GUEST_LINUX_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

export DEBIAN_FRONTEND=noninteractive

echo "== [1/6] 安装基础桌面、Chromium、nftables 与 VMware 工具（Claude Code 为独立二进制，无需 Node.js）"
apt-get update
apt-get install -y --no-install-recommends \
  openbox xorg lightdm xterm \
  chromium \
  nftables curl ca-certificates dnsutils openssh-server sudo \
  open-vm-tools open-vm-tools-desktop \
  locales tzdata iproute2 procps gzip

echo "== [2/6] 创建普通用户 claude 与 LightDM 自动登录（Openbox 会话）"
if ! id -u claude >/dev/null 2>&1; then
  useradd -m -s /bin/bash claude
fi
gpasswd -d claude sudo 2>/dev/null || true
mkdir -p /etc/lightdm/lightdm.conf.d /home/claude/.config/openbox
cat >/etc/lightdm/lightdm.conf.d/50-cchouse-autologin.conf <<'EOF'
[Seat:*]
autologin-user=claude
autologin-user-timeout=0
user-session=openbox
EOF
cat >/home/claude/.config/openbox/autostart <<'EOF'
chromium &
xterm &
EOF
chown -R claude:claude /home/claude/.config

echo "== [3/6] 安装 mihomo 二进制（必须显式提供 MIHOMO_SHA256 校验）"
[ -n "$MIHOMO_SHA256" ] || { echo "错误：必须显式传入 MIHOMO_SHA256 以校验 mihomo 二进制" >&2; exit 1; }
tmp_gz="$(mktemp)"
trap 'rm -f "$tmp_gz"' EXIT
if [ -n "${MIHOMO_LOCAL_GZ:-}" ] && [ -f "$MIHOMO_LOCAL_GZ" ]; then
  cp "$MIHOMO_LOCAL_GZ" "$tmp_gz"
else
  curl -fsSL "$MIHOMO_URL" -o "$tmp_gz"
fi
echo "${MIHOMO_SHA256}  ${tmp_gz}" | sha256sum -c -
mkdir -p /usr/local/bin
gzip -dc "$tmp_gz" >/usr/local/bin/mihomo
chmod 755 /usr/local/bin/mihomo

echo "== [4/6] 安装每次开机必跑的默认全断 nftables 与 firstboot 加载器"
mkdir -p /etc/cchouse /usr/local/sbin /var/lib/cchouse
install -m 600 "${GUEST_LINUX_DIR}/nftables-default-drop.conf" /etc/cchouse/nftables-default-drop.conf
install -m 755 "${SCRIPT_DIR}/cchouse-firstboot-loader.sh" /usr/local/sbin/cchouse-firstboot.sh
install -m 644 "${GUEST_LINUX_DIR}/cchouse-firstboot.service" /etc/systemd/system/cchouse-firstboot.service
cat >/etc/systemd/system/cchouse-default-drop.service <<'EOF'
[Unit]
Description=cchouse out-of-box default-drop nftables (every boot before network and instance nft)
DefaultDependencies=no
Before=network-pre.target cchouse-nftables.service
Wants=network-pre.target
After=local-fs.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f /etc/cchouse/nftables-default-drop.conf

[Install]
WantedBy=sysinit.target
EOF
systemctl daemon-reload
systemctl enable cchouse-default-drop.service cchouse-firstboot.service

echo "== [5/6] 清理局域网广播、联网探针、cloud-init 与自动更新，恢复官方 apt 源"
apt-get purge -y avahi-daemon cups-browsed unattended-upgrades cloud-init cloud-guest-utils 2>/dev/null || true
rm -rf /etc/cloud /var/lib/cloud
apt-get autoremove -y
apt-get clean
sed -i 's|mirrors\.ustc\.edu\.cn|deb.debian.org|g' /etc/apt/sources.list /etc/apt/sources.list.d/* 2>/dev/null || true
mkdir -p /etc/NetworkManager/conf.d
cat >/etc/NetworkManager/conf.d/99-cchouse-no-connectivity-check.conf <<'EOF'
[connectivity]
enabled=false
EOF
systemctl disable apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true

echo "== [6/6] 清空机器身份、随机种子、临时 root 密码哈希并执行空状态断言"
usermod -p '*' root
truncate -s 0 /etc/machine-id /etc/hostname
rm -f /var/lib/dbus/machine-id /var/lib/systemd/random-seed
rm -f /etc/ssh/ssh_host_* /root/.ssh/authorized_keys /home/debian/.ssh/authorized_keys
rm -rf /var/lib/dhcp/* /var/lib/NetworkManager/* /var/log/journal/*
rm -rf /home/claude/.config/chromium /home/claude/.cache /home/claude/.claude* /home/claude/.npm /root/.npm /root/.bash_history /home/claude/.bash_history
rm -f /var/lib/cchouse/firstboot.done

bash "${SCRIPT_DIR}/assert-empty-state.sh"
echo "黄金镜像构建与空状态断言全部通过。"
