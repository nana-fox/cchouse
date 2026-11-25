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

check "/etc/machine-id 为空文件（0 字节）" test ! -s /etc/machine-id
check "/etc/hostname 为空文件（0 字节）" test ! -s /etc/hostname
check "无 /var/lib/dbus/machine-id" test ! -e /var/lib/dbus/machine-id
check "无 /var/lib/systemd/random-seed" test ! -e /var/lib/systemd/random-seed
check "无 SSH 主机密钥" bash -c '! ls /etc/ssh/ssh_host_* >/dev/null 2>&1'
check "无 Chromium 用户目录" test ! -d /home/claude/.config/chromium
check "无 ~/.claude* 目录或文件" bash -c '! ls -d /home/claude/.claude* >/dev/null 2>&1'
check "未预装 Claude Code（合规：首次开机由宿主注入）" bash -c '! command -v claude >/dev/null 2>&1'
check "无 npm 缓存" bash -c 'test ! -d /home/claude/.npm && test ! -d /root/.npm'
check "已彻底移除 cloud-init" bash -c '! command -v cloud-init >/dev/null 2>&1 && test ! -d /var/lib/cloud'
check "root 无可用密码哈希" bash -c 'awk -F: "\$1==\"root\"{exit (\$2==\"*\"||\$2==\"!\")?0:1}" /etc/shadow'
check "LightDM 自动登录已配置" test -f /etc/lightdm/lightdm.conf.d/50-cchouse-autologin.conf
check "出厂默认全断 nftables 规则存在" test -f /etc/cchouse/nftables-default-drop.conf
check "cchouse-default-drop.service 已启用" systemctl is-enabled cchouse-default-drop.service >/dev/null 2>&1
check "cchouse-firstboot.service 已启用" systemctl is-enabled cchouse-firstboot.service >/dev/null 2>&1
check "firstboot.done 尚未生成" test ! -e /var/lib/cchouse/firstboot.done

[ "$fail" = 0 ] || { echo "空状态断言失败：$fail 项未通过" >&2; exit 1; }
