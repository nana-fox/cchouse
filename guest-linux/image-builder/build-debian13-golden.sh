#!/bin/bash
# Debian 13 (amd64) 黄金镜像构建脚本（由 Packer vmware-iso 或干净 Debian 13 客机以 root 执行）
# 不包含任何凭据，不预装 Claude Code（由首次开机从宿主校验后的 npm tarball 离线安装）。
set -euo pipefail

MIHOMO_VERSION="${MIHOMO_VERSION:-v1.19.19}"
MIHOMO_ARCH="${MIHOMO_ARCH:-amd64}"
MIHOMO_SHA256="${MIHOMO_SHA256:-}"
MIHOMO_URL="${MIHOMO_URL:-https://github.com/MetaCubeX/mihomo/releases/download/${MIHOMO_VERSION}/mihomo-linux-${MIHOMO_ARCH}-${MIHOMO_VERSION}.gz}"
TARGET_HYPERVISOR="${TARGET_HYPERVISOR:-vmware}"
SOURCE_COMMIT="${SOURCE_COMMIT:-unknown}"
BUILD_TIME="${BUILD_TIME:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
GUEST_LINUX_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

export DEBIAN_FRONTEND=noninteractive

rewrite_apt_sources() {
  local apt_dir="$1" expression="$2" source
  for source in "$apt_dir"/sources.list "$apt_dir"/sources.list.d/* "$apt_dir"/mirrors/*; do
    [ -f "$source" ] || continue
    perl -pi -e "$expression" -- "$source" || return
  done
}

if [ "${USE_USTC_MIRROR:-0}" = "1" ]; then
  rewrite_apt_sources /etc/apt 's|deb\.debian\.org|mirrors.ustc.edu.cn|g; s|security\.debian\.org|mirrors.ustc.edu.cn|g'
fi

echo "== [1/6] 安装基础桌面、Chromium、中文字体、fcitx5 拼音、tint2、nftables 与虚拟化工具（${TARGET_HYPERVISOR}/${MIHOMO_ARCH}，无需 Node.js）"
HYPERVISOR_PKGS=()
if [ "$TARGET_HYPERVISOR" = "vmware" ]; then
  HYPERVISOR_PKGS=(open-vm-tools open-vm-tools-desktop)
elif [ "$TARGET_HYPERVISOR" = "tart" ]; then
  HYPERVISOR_PKGS=(spice-vdagent)
fi
apt-get update
apt-get install -y --no-install-recommends \
  openbox xorg lightdm dbus-user-session tint2 lxterminal x11-xserver-utils xclip \
  chromium \
  fonts-noto-cjk fonts-noto-color-emoji \
  fcitx5 fcitx5-chinese-addons fcitx5-frontend-gtk3 fcitx5-frontend-gtk4 \
  nftables curl ca-certificates dnsutils openssh-server sudo \
  "${HYPERVISOR_PKGS[@]}" \
  locales tzdata iproute2 procps gzip

echo "== [2/6] 创建普通用户 claude 与完整 Openbox 桌面配置（自动登录、任务栏、拼音输入法、终端）"
if ! id -u claude >/dev/null 2>&1; then
  useradd -m -s /bin/bash claude
fi
gpasswd -d claude sudo 2>/dev/null || true
mkdir -p /etc/lightdm/lightdm.conf.d \
  /etc/chromium.d \
  /home/claude/.config/openbox \
  /home/claude/.config/fcitx5/conf \
  /home/claude/.config/tint2 \
  /home/claude/.config/lxterminal
cat >/etc/lightdm/lightdm.conf.d/50-cchouse-autologin.conf <<'EOF'
[Seat:*]
autologin-user=claude
autologin-user-timeout=0
user-session=openbox
EOF
cat >/etc/profile.d/cchouse-ime.sh <<'EOF'
export GTK_IM_MODULE=fcitx
export QT_IM_MODULE=fcitx
export XMODIFIERS=@im=fcitx
export SDL_IM_MODULE=fcitx
EOF
chmod 644 /etc/profile.d/cchouse-ime.sh
cat >/home/claude/.config/openbox/environment <<'EOF'
[ -f /etc/profile.d/cchouse-ime.sh ] && . /etc/profile.d/cchouse-ime.sh
EOF
# 统一通过 /etc/chromium.d/cchouse 注入启动参数，避免 autostart / 右键菜单 / BROWSER 三处漂移
cat >/etc/chromium.d/cchouse <<'EOF'
export CHROMIUM_FLAGS="${CHROMIUM_FLAGS:-} --start-maximized --no-first-run --hide-crash-restore-bubble"
EOF
chmod 644 /etc/chromium.d/cchouse
# 移除 Openbox 默认 A-space (Alt+Space / macOS Option+Space) 窗口菜单热键，避免拦截 fcitx5 切换
cp /etc/xdg/openbox/rc.xml /home/claude/.config/openbox/rc.xml
perl -0777 -i -pe 's|<keybind\s+key="A-space">.*?</keybind>\s*||gsi' /home/claude/.config/openbox/rc.xml
if grep -qi 'key="A-space"' /home/claude/.config/openbox/rc.xml; then
  echo "错误：未能从 /home/claude/.config/openbox/rc.xml 移除 A-space 快捷键绑定" >&2
  exit 1
fi
# 仅放行图形右键菜单的关机与重启（无通配符，免 polkitd 且兼容存在 SSH 会话场景）
cat >/etc/sudoers.d/cchouse-power <<'EOF'
claude ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff
claude ALL=(root) NOPASSWD: /usr/bin/systemctl reboot
EOF
chmod 440 /etc/sudoers.d/cchouse-power
visudo -cf /etc/sudoers.d/cchouse-power
cat >/home/claude/.config/openbox/menu.xml <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<openbox_menu xmlns="http://openbox.org/3.4/menu">
  <menu id="root-menu" label="cchouse">
    <item label="Chromium Browser">
      <action name="Execute"><command>chromium</command></action>
    </item>
    <item label="Terminal (Claude Code)">
      <action name="Execute"><command>lxterminal</command></action>
    </item>
    <separator />
    <item label="Toggle Pinyin / EN">
      <action name="Execute"><command>fcitx5-remote -t</command></action>
    </item>
    <separator />
    <item label="Power Off (Safe Shutdown)">
      <action name="Execute"><command>sudo -n /usr/bin/systemctl poweroff</command></action>
    </item>
    <item label="Reboot">
      <action name="Execute"><command>sudo -n /usr/bin/systemctl reboot</command></action>
    </item>
  </menu>
</openbox_menu>
EOF
# 注：fcitx5/profile 不在黄金镜像写死 us 布局，由 firstboot.sh 按实例 GUEST_KEYBOARD 生成
cat >/home/claude/.config/fcitx5/config <<'EOF'
[Hotkey]
EnumerateWithTriggerKeys=True
EnumerateSkipFirst=False

[Hotkey/TriggerKeys]
0=Control+space
1=Alt+space
2=Shift_L

[Hotkey/EnumerateForwardKeys]
0=Control+Shift_L
EOF
# 显式关闭云拼音，避免误开后经出口请求第三方输入法云接口
cat >/home/claude/.config/fcitx5/conf/pinyin.conf <<'EOF'
CloudPinyinEnabled=False
EOF
cat >/home/claude/.config/tint2/tint2rc <<'EOF'
rounded = 0
border_width = 0
background_color = #1e222a 100

rounded = 4
border_width = 1
background_color = #2c323c 100
border_color = #3e4451 100

rounded = 4
border_width = 1
background_color = #3b82f6 100
border_color = #60a5fa 100

panel_items = TSC
panel_size = 100% 34
panel_margin = 0 0
panel_padding = 6 4 6
panel_background_id = 1
panel_position = bottom center horizontal
panel_layer = top
panel_monitor = all
strut_policy = follow_size

taskbar_mode = single_desktop
taskbar_padding = 4 0 4
task_text = 1
task_icon = 1
task_centered = 0
task_maximum_size = 240 28
task_padding = 8 2 4
task_font = Noto Sans CJK SC 10
task_font_color = #e5e7eb 100
task_active_font_color = #ffffff 100
task_background_id = 2
task_active_background_id = 3
mouse_left = toggle_iconify
mouse_middle = close

systray_padding = 6 4 6
systray_icon_size = 20
systray_sort = ascending

time1_format = %H:%M  %m-%d
time1_font = Noto Sans CJK SC 10
clock_font_color = #e5e7eb 100
clock_padding = 8 0
EOF
cat >/home/claude/.config/lxterminal/lxterminal.conf <<'EOF'
[general]
fontname=Noto Sans Mono CJK SC 12
selchars=-A-Za-z0-9,./?%&#:_
scrollback=10000
bgcolor=rgb(24,27,32)
fgcolor=rgb(235,238,245)
hidemenubar=false
hidescrollbar=false
geometry_columns=100
geometry_rows=28
EOF
cat >/home/claude/.config/openbox/autostart <<'EOF'
xset s off -dpms s noblank &
xsetroot -solid "#2b2f3a" &
cchouse_vendor=""
cchouse_product=""
if [ -r /sys/class/dmi/id/sys_vendor ]; then
  cchouse_vendor=$(cat /sys/class/dmi/id/sys_vendor)
fi
if [ -r /sys/class/dmi/id/product_name ]; then
  cchouse_product=$(cat /sys/class/dmi/id/product_name)
fi
case "$cchouse_vendor" in
  "VMware, Inc.")
    command -v vmware-user-suid-wrapper >/dev/null 2>&1 && vmware-user-suid-wrapper &
    ;;
esac
case "$cchouse_vendor|$cchouse_product" in
  "innotek GmbH|VirtualBox")
    command -v VBoxClient >/dev/null 2>&1 && VBoxClient --clipboard &
    command -v VBoxClient >/dev/null 2>&1 && VBoxClient --vmsvga &
    ;;
esac
unset cchouse_vendor cchouse_product
command -v spice-vdagent >/dev/null 2>&1 && spice-vdagent &
tint2 &
fcitx5 -d &
[ -f /etc/issue.d/cchouse.issue ] && lxterminal -e sh -c 'cat /etc/issue.d/cchouse.issue /var/lib/cchouse/firstboot.log 2>/dev/null; exec bash' &
(sleep 1 && chromium) &
(sleep 1 && lxterminal) &
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

echo "== [5/6] 配置持久化 IPv4 DHCP（防移除 cloud-init 后断网），清理局域网广播与自动更新"
mkdir -p /etc/systemd/network
cat >/etc/systemd/network/20-cchouse-dhcp.network <<'EOF'
[Match]
Name=en* eth*

[Network]
DHCP=ipv4
IPv6AcceptRA=no
LinkLocalAddressing=no

[DHCPv4]
ClientIdentifier=mac
UseDNS=no
UseNTP=no
EOF
systemctl enable systemd-networkd.service
apt-get purge -y avahi-daemon cups-browsed unattended-upgrades cloud-init cloud-guest-utils 2>/dev/null || true
rm -rf /etc/cloud /var/lib/cloud
rm -f /etc/netplan/50-cloud-init.yaml /etc/ssh/sshd_config.d/50-cloud-init.conf /etc/sudoers.d/90-cloud-init-users
apt-get autoremove -y
apt-get clean
# 恢复普通仓库与安全更新各自的官方地址，再执行最终镜像断言。
rewrite_apt_sources /etc/apt 's|mirrors\.ustc\.edu\.cn/debian-security|security.debian.org/debian-security|g; s|mirrors\.ustc\.edu\.cn|deb.debian.org|g'
mkdir -p /etc/NetworkManager/conf.d
cat >/etc/NetworkManager/conf.d/99-cchouse-no-connectivity-check.conf <<'EOF'
[connectivity]
enabled=false
EOF
systemctl disable apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true

cchouse_vboxguest_matches() {
  vermagic=$(modinfo -k "$(uname -r)" -F vermagic vboxguest 2>/dev/null || true)
  case "$vermagic" in
    "$(uname -r)"*)
      [ -n "$vermagic" ]
      ;;
    *)
      return 1
      ;;
  esac
}

cchouse_unit_dropin() {
  unit="$1"
  condition="$2"
  unit_path=""
  for candidate in "/lib/systemd/system/${unit}" "/usr/lib/systemd/system/${unit}"; do
    if [ -f "$candidate" ]; then
      unit_path="$candidate"
      break
    fi
  done
  [ -n "$unit_path" ] || return 0
  mkdir -p "/etc/systemd/system/${unit}.d"
  printf '[Unit]\nConditionVirtualization=%s\n' "$condition" >"/etc/systemd/system/${unit}.d/cchouse.conf"
}

# amd64 OVA 同时带 open-vm-tools 与 Guest Additions。服务按 DMI 启动，编译器不留在镜像里。
# 镜像已 purge unattended-upgrades 并关掉 apt-daily，内核不会自己升级，所以编完就删 dkms。
install_vbox_guest_additions() {
  iso="${VBOX_GUEST_ADDITIONS_ISO:-}"
  ga_sha="${VBOX_GUEST_ADDITIONS_SHA256:-}"
  [ -n "$iso" ] && [ -f "$iso" ] || { echo "错误：amd64 镜像必须提供 VBOX_GUEST_ADDITIONS_ISO" >&2; exit 1; }
  printf '%s' "$ga_sha" | grep -Eq '^[0-9a-f]{64}$' || { echo "错误：VBOX_GUEST_ADDITIONS_SHA256 无效" >&2; exit 1; }
  echo "${ga_sha}  ${iso}" | sha256sum -c -
  apt-get update
  apt-get install -y --no-install-recommends "linux-headers-$(uname -r)" build-essential dkms bzip2 tar
  mnt=$(mktemp -d)
  mount -o loop,ro "$iso" "$mnt"
  set +e
  sh "$mnt/VBoxLinuxAdditions.run" --nox11
  set -e
  umount "$mnt"
  rmdir "$mnt" || true
  if ! cchouse_vboxguest_matches; then
    setup=""
    for candidate in /opt/VBoxGuestAdditions-*/init/vboxadd /sbin/rcvboxadd /usr/sbin/rcvboxadd; do
      if [ -x "$candidate" ]; then
        setup=$candidate
        break
      fi
    done
    [ -n "$setup" ] || { echo "错误：Guest Additions 没有留下 vboxadd setup" >&2; exit 1; }
    "$setup" setup
  fi
  cchouse_vboxguest_matches || { echo "错误：vboxguest 没有对上 $(uname -r)" >&2; exit 1; }
  apt-get purge -y build-essential dkms
  header_list=$(mktemp)
  dpkg-query -W -f '${Package}\n' 'linux-headers-*' >"$header_list" 2>/dev/null || true
  if [ -s "$header_list" ]; then
    xargs -r apt-get purge -y <"$header_list"
  fi
  rm -f "$header_list"
  apt-get autoremove -y
  apt-get clean
  cchouse_vboxguest_matches || { echo "错误：清掉编译依赖后 vboxguest 不在了" >&2; exit 1; }
  rm -f "$iso"
  cchouse_unit_dropin vboxadd.service oracle
  cchouse_unit_dropin vboxadd-service.service oracle
  cchouse_unit_dropin open-vm-tools.service vmware
  cchouse_unit_dropin vgauth.service vmware
  cat >/usr/local/bin/cchouse-vmware-user <<'EOF'
#!/bin/sh
vendor=""
if [ -r /sys/class/dmi/id/sys_vendor ]; then
  vendor=$(cat /sys/class/dmi/id/sys_vendor)
fi
case "$vendor" in
  "VMware, Inc.")
    exec vmware-user-suid-wrapper
    ;;
esac
exit 0
EOF
  chmod 755 /usr/local/bin/cchouse-vmware-user
  if [ -f /etc/xdg/autostart/vmware-user.desktop ]; then
    sed -i 's|^Exec=.*|Exec=/usr/local/bin/cchouse-vmware-user|' /etc/xdg/autostart/vmware-user.desktop
  fi
  for desktop in /etc/xdg/autostart/vboxclient.desktop /usr/share/autostart/vboxclient.desktop; do
    if [ -f "$desktop" ]; then
      sed -i 's|^Exec=.*|Exec=/bin/true|' "$desktop"
    fi
  done
  systemctl daemon-reload
}

if [ "$MIHOMO_ARCH" = "amd64" ]; then
  echo "== 安装 VirtualBox Guest Additions（仅 amd64；Tart 不走这里）"
  install_vbox_guest_additions
fi

echo "== [6/6] 写入镜像版本元数据、清空机器身份并执行空状态断言"
cat >/etc/cchouse-image-release <<EOF
SOURCE_COMMIT=${SOURCE_COMMIT}
BUILD_TIME=${BUILD_TIME}
ARCH=${MIHOMO_ARCH}
HYPERVISOR=${TARGET_HYPERVISOR}
EOF
chmod 644 /etc/cchouse-image-release
usermod -p '*' root
truncate -s 0 /etc/machine-id /etc/hostname
rm -f /var/lib/dbus/machine-id /var/lib/systemd/random-seed
rm -f /etc/ssh/ssh_host_* /root/.ssh/authorized_keys /home/debian/.ssh/authorized_keys
rm -rf /var/lib/dhcp/* /var/lib/NetworkManager/* /var/log/journal/*
rm -rf /home/claude/.config/chromium /home/claude/.cache /home/claude/.claude* /home/claude/.npm /home/claude/.local/share/fcitx5 /root/.npm /root/.bash_history /home/claude/.bash_history
rm -f /home/claude/.config/fcitx5/profile /var/lib/cchouse/firstboot.done

bash "${SCRIPT_DIR}/assert-empty-state.sh"
echo "黄金镜像构建与空状态断言全部通过。"
