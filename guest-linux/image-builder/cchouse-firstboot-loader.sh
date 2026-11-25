#!/bin/bash
# 出厂内置加载器（不含任何实例占位符）：在 network-pre.target 前挂载 LABEL=CCHOUSE 并运行渲染好的 firstboot.sh
set -euo pipefail

if [ -f /etc/cchouse/nftables-default-drop.conf ]; then
  /usr/sbin/nft -f /etc/cchouse/nftables-default-drop.conf
fi

if command -v udevadm >/dev/null 2>&1; then
  udevadm settle --timeout=15 || true
fi

DEV=""
for _ in $(seq 1 30); do
  if [ -b /dev/disk/by-label/CCHOUSE ]; then
    DEV="/dev/disk/by-label/CCHOUSE"
    break
  fi
  sleep 0.5
done
LOG="/var/lib/cchouse/firstboot.log"
ISSUE="/etc/issue.d/cchouse.issue"
mkdir -p /var/lib/cchouse /etc/issue.d

fail_visible() {
  local msg="$1"
  echo "$msg" | tee -a "$LOG" >&2
  printf '\n[cchouse firstboot FAILED] %s\nSee /var/lib/cchouse/firstboot.log or: systemctl status cchouse-firstboot\n\n' "$msg" >"$ISSUE"
  exit 1
}

[ -n "$DEV" ] || fail_visible "未找到 LABEL=CCHOUSE 配置光盘，保持默认全断退出"

MNT="/run/cchouse-seed"
mkdir -p "$MNT"
mount -o ro "$DEV" "$MNT" || fail_visible "挂载 LABEL=CCHOUSE 失败"
trap 'umount "$MNT" 2>/dev/null || true' EXIT

[ -f "$MNT/firstboot.sh" ] || fail_visible "配置光盘缺少 firstboot.sh，保持断网退出"
if ! bash "$MNT/firstboot.sh" "$MNT" 2>&1 | tee -a "$LOG"; then
  fail_visible "firstboot.sh 执行失败，已保持默认全断"
fi
rm -f "$ISSUE"
