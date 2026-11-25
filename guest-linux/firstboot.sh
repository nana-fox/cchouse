#!/bin/bash
# 首次开机离线注入脚本（由镜像内置 cchouse-firstboot-loader.sh 在 network-pre.target 前调用）
set -euo pipefail

MNT="${1:-/run/cchouse-seed}"
TAG="<TAG>"
MIHOMO_USER="<MIHOMO_USER>"
TIMEZONE="<TIMEZONE>"
GUEST_LOCALE="<GUEST_LOCALE>"
GUEST_KEYBOARD="<GUEST_KEYBOARD>"
GUEST_HOSTNAME="<GUEST_HOSTNAME>"
DST="/usr/local/${TAG}"

mkdir -p "$DST" /var/lib/cchouse /etc/chromium/policies/managed /etc/ssh/sshd_config.d /etc/NetworkManager/conf.d

# 1. 创建 mihomo 专用系统用户
if ! id -u "$MIHOMO_USER" >/dev/null 2>&1; then
  useradd --system --no-create-home --shell /usr/sbin/nologin "$MIHOMO_USER"
fi

# 2. 部署配置、规则、无通配符受控探针与最小 sudoers
install -m 640 -o root -g "$MIHOMO_USER" "$MNT/config.yaml" "$DST/config.yaml"
install -m 600 -o root -g root "$MNT/nftables.conf" "$DST/nftables.conf"
install -m 644 -o root -g root "$MNT/check.html" "$DST/check.html"
install -m 755 -o root -g root "$MNT/timesync.sh" "$DST/timesync.sh"
install -m 755 -o root -g root "$MNT/cchouse-probe.sh" /usr/local/sbin/cchouse-probe
install -m 644 -o root -g root "$MNT/chromium-policy.json" /etc/chromium/policies/managed/cchouse.json
install -m 644 -o root -g root "$MNT/cchouse-nftables.service" /etc/systemd/system/cchouse-nftables.service
install -m 644 -o root -g root "$MNT/cchouse-mihomo.service" /etc/systemd/system/cchouse-mihomo.service
install -m 644 -o root -g root "$MNT/cchouse-timesync.service" /etc/systemd/system/cchouse-timesync.service
install -m 644 -o root -g root "$MNT/cchouse-timesync.timer" /etc/systemd/system/cchouse-timesync.timer

cat >/etc/sudoers.d/cchouse-verify <<'EOF'
# 仅允许无参数固定子命令的受控探针，禁止任何通配符
claude ALL=(root) NOPASSWD: /usr/local/sbin/cchouse-probe nft-loaded
claude ALL=(root) NOPASSWD: /usr/local/sbin/cchouse-probe nft-live-sha
claude ALL=(root) NOPASSWD: /usr/local/sbin/cchouse-probe direct-count
claude ALL=(root) NOPASSWD: /usr/local/sbin/cchouse-probe mihomo-stop
claude ALL=(root) NOPASSWD: /usr/local/sbin/cchouse-probe mihomo-start
claude ALL=(root) NOPASSWD: /usr/local/sbin/cchouse-probe root-egress
EOF
chmod 440 /etc/sudoers.d/cchouse-verify
visudo -cf /etc/sudoers.d/cchouse-verify

# 3. 机器身份去撞脸：重新生成 machine-id、SSH 主机密钥与随机主机名
if [ ! -s /etc/machine-id ]; then
  systemd-machine-id-setup
fi
if [ ! -f /etc/ssh/ssh_host_ed25519_key ]; then
  ssh-keygen -A
fi
if [ -z "$GUEST_HOSTNAME" ]; then
  RAND_HEX=$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')
  GUEST_HOSTNAME="pc-${RAND_HEX}"
fi
printf '%s\n' "$GUEST_HOSTNAME" >/etc/hostname
hostname "$GUEST_HOSTNAME" || true
if grep -q '^127\.0\.1\.1' /etc/hosts; then
  sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t${GUEST_HOSTNAME}/" /etc/hosts
else
  printf '127.0.1.1\t%s\n' "$GUEST_HOSTNAME" >>/etc/hosts
fi

# 4. 时区、语言（locale-gen 离线生效）与键盘对齐出口
ln -sf "/usr/share/zoneinfo/${TIMEZONE}" /etc/localtime
printf '%s\n' "$TIMEZONE" >/etc/timezone
if [ -f /etc/locale.gen ]; then
  sed -i "s/^# *${GUEST_LOCALE}/${GUEST_LOCALE}/" /etc/locale.gen || true
  locale-gen || true
fi
printf 'LANG=%s\nLC_ALL=%s\n' "$GUEST_LOCALE" "$GUEST_LOCALE" >/etc/default/locale
cat >/etc/default/keyboard <<EOF
XKBMODEL="pc105"
XKBLAYOUT="${GUEST_KEYBOARD}"
XKBVARIANT=""
XKBOPTIONS=""
EOF

# 5. 锁定静态 resolv.conf，防止 VMware NAT DHCP 下发 vmnet .2 绕过 TUN dns-hijack
cat >/etc/NetworkManager/conf.d/98-cchouse-static-dns.conf <<'EOF'
[main]
dns=none
EOF
rm -f /etc/resolv.conf
printf 'nameserver 1.1.1.1\n' >/etc/resolv.conf
chmod 644 /etc/resolv.conf

# 6. SSH 仅允许公钥登录 + 写入公钥
cat >"/etc/ssh/sshd_config.d/100-${TAG}.conf" <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
PubkeyAuthentication yes
EOF
if [ -f "$MNT/authorized_keys" ] && id -u claude >/dev/null 2>&1; then
  install -d -m 700 -o claude -g claude /home/claude/.ssh
  install -m 600 -o claude -g claude "$MNT/authorized_keys" /home/claude/.ssh/authorized_keys
fi

# 7. Claude Code 环境变量与防自动更新
cat >/etc/profile.d/cchouse-claude.sh <<'EOF'
export DISABLE_AUTOUPDATER=1
export BROWSER=/usr/bin/chromium
EOF
chmod 644 /etc/profile.d/cchouse-claude.sh

# 8. 禁用局域网广播与自动更新（--no-block 防 sysinit 阶段死锁）
systemctl disable --no-block avahi-daemon.service avahi-daemon.socket cups-browsed.service apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true

# 9. 加载实例 nftables、记录内核实际生效规则摘要、启用并拉起服务、写 done 标记
/usr/sbin/nft -f "$DST/nftables.conf"
/usr/local/sbin/cchouse-probe nft-live-sha >/var/lib/cchouse/expected-nft-live.sha256
systemctl daemon-reload
systemctl enable cchouse-nftables.service cchouse-mihomo.service cchouse-timesync.timer
systemctl start --no-block cchouse-mihomo.service cchouse-timesync.timer
touch /var/lib/cchouse/firstboot.done
systemctl disable cchouse-firstboot.service || true

# 10. 离线安装 Claude Code CLI（置于 done 之后且非致命，避免 npm 异常拖垮整台客机）
if [ -f "$MNT/claude-code.tgz" ]; then
  if ! npm install -g --offline --no-audit --no-fund "$MNT/claude-code.tgz"; then
    echo "警告：Claude Code 离线安装失败，请连网后按手册重试" >&2
  fi
fi
