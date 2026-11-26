#!/bin/bash
# 黄金镜像打包前空状态硬断言：任何一项不满足则拒绝导出 .ova（防止批量交付撞脸或残留凭据）
set -euo pipefail

fail=0
check() {
  local desc="$1"; shift
  if "$@"; then
    echo "  PASS  $desc"
  else
    echo "  FAIL  $desc" >&2
    fail=$((fail + 1))
  fi
}

apt_without_ustc() {
  local status=0
  grep -Rqi 'ustc' "$1" || status=$?
  [ "$status" -eq 1 ]
}

guest_additions_build_deps_gone() {
  if dpkg -s dkms >/dev/null 2>&1; then
    return 1
  fi
  if dpkg -s build-essential >/dev/null 2>&1; then
    return 1
  fi
  if dpkg-query -W -f '${Package}\n' 'linux-headers-*' 2>/dev/null | grep -q .; then
    return 1
  fi
  if ls /tmp/VBoxGuestAdditions*.iso >/dev/null 2>&1; then
    return 1
  fi
  return 0
}

check "/etc/machine-id 为空文件（0 字节）" test ! -s /etc/machine-id
check "/etc/hostname 为空文件（0 字节）" test ! -s /etc/hostname
check "无 /var/lib/dbus/machine-id" test ! -e /var/lib/dbus/machine-id
check "无 /var/lib/systemd/random-seed" test ! -e /var/lib/systemd/random-seed
check "无 SSH 主机密钥" bash -c '! ls /etc/ssh/ssh_host_* >/dev/null 2>&1'
check "无 Chromium 用户目录" test ! -d /home/claude/.config/chromium
check "无 fcitx5 用户词库/输入历史残留" test ! -d /home/claude/.local/share/fcitx5
check "无硬编码 fcitx5 键盘 profile" test ! -e /home/claude/.config/fcitx5/profile
check "无 ~/.claude* 目录或文件" bash -c '! ls -d /home/claude/.claude* >/dev/null 2>&1'
check "未预装 Claude Code（合规：首次开机由宿主注入）" bash -c '! command -v claude >/dev/null 2>&1'
check "无 npm 缓存" bash -c 'test ! -d /home/claude/.npm && test ! -d /root/.npm'
check "已彻底移除 cloud-init" bash -c '! command -v cloud-init >/dev/null 2>&1 && test ! -d /var/lib/cloud'
check "无 cloud-init 残留配置" bash -c 'test ! -e /etc/netplan/50-cloud-init.yaml && test ! -e /etc/ssh/sshd_config.d/50-cloud-init.conf && test ! -e /etc/sudoers.d/90-cloud-init-users'
check "/etc/apt 不含 USTC 镜像记录" apt_without_ustc /etc/apt
check "持久化 IPv4 DHCP 配置已就绪" bash -c 'test -f /etc/systemd/network/20-cchouse-dhcp.network && systemctl is-enabled systemd-networkd.service >/dev/null 2>&1'
check "无构建期 root SSH 授权密钥" bash -c 'test ! -s /root/.ssh/authorized_keys'
check "root 无可用密码哈希" bash -c 'awk -F: "\$1==\"root\"{exit (\$2==\"*\"||\$2==\"!\")?0:1}" /etc/shadow'
check "LightDM 自动登录已配置" test -f /etc/lightdm/lightdm.conf.d/50-cchouse-autologin.conf
check "中文桌面与输入法组件已就绪" bash -c 'dpkg -s fonts-noto-cjk fcitx5-chinese-addons tint2 lxterminal dbus-user-session >/dev/null 2>&1 && test -x /usr/bin/fcitx5 && test -f /home/claude/.config/openbox/autostart && [ "$(stat -c %U /home/claude/.config)" = "claude" ]'
check "出厂默认全断 nftables 规则存在" test -f /etc/cchouse/nftables-default-drop.conf
check "cchouse-default-drop.service 已启用" bash -c 'systemctl is-enabled cchouse-default-drop.service >/dev/null 2>&1'
check "cchouse-firstboot.service 已启用" bash -c 'systemctl is-enabled cchouse-firstboot.service >/dev/null 2>&1'
check "firstboot.done 尚未生成" test ! -e /var/lib/cchouse/firstboot.done
check "未安装 unattended-upgrades" bash -c '! dpkg -s unattended-upgrades >/dev/null 2>&1'
check "apt 每日自动升级已关闭" bash -c '! systemctl is-enabled apt-daily.timer >/dev/null 2>&1 && ! systemctl is-enabled apt-daily-upgrade.timer >/dev/null 2>&1'

cchouse_image_arch=""
if [ -f /etc/cchouse-image-release ]; then
  cchouse_image_arch=$(sed -n 's/^ARCH=//p' /etc/cchouse-image-release | head -n 1)
fi
if [ "$cchouse_image_arch" = "amd64" ]; then
  check "vboxadd.service 存在且只在 VirtualBox 启动" bash -c 'test -f /etc/systemd/system/vboxadd.service.d/cchouse.conf && grep -qx "ConditionVirtualization=oracle" /etc/systemd/system/vboxadd.service.d/cchouse.conf && { test -f /lib/systemd/system/vboxadd.service || test -f /usr/lib/systemd/system/vboxadd.service; }'
  check "vboxadd-service.service 存在且只在 VirtualBox 启动" bash -c 'test -f /etc/systemd/system/vboxadd-service.service.d/cchouse.conf && grep -qx "ConditionVirtualization=oracle" /etc/systemd/system/vboxadd-service.service.d/cchouse.conf && { test -f /lib/systemd/system/vboxadd-service.service || test -f /usr/lib/systemd/system/vboxadd-service.service; }'
  check "open-vm-tools 仅在 VMware 启动" bash -c 'dpkg -s open-vm-tools open-vm-tools-desktop >/dev/null 2>&1 && grep -qx "ConditionVirtualization=vmware" /etc/systemd/system/open-vm-tools.service.d/cchouse.conf'
  check "vboxguest 对得上当前内核" bash -c 'vermagic=$(modinfo -k "$(uname -r)" -F vermagic vboxguest 2>/dev/null) && case "$vermagic" in "$(uname -r)"*) test -n "$vermagic" ;; *) exit 1 ;; esac'
  check "已删除 Guest Additions 编译依赖与 ISO" guest_additions_build_deps_gone
  check "没有 vboxsf 挂载" bash -c '! grep -q " vboxsf " /proc/mounts'
fi

[ "$fail" = 0 ] || { echo "空状态断言失败：$fail 项未通过" >&2; exit 1; }
