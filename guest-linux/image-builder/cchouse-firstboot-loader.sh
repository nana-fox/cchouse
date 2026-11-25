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
[ -n "$DEV" ] || { echo "未找到 LABEL=CCHOUSE 配置光盘，保持默认全断退出" >&2; exit 1; }

MNT="/run/cchouse-seed"
mkdir -p "$MNT"
mount -o ro "$DEV" "$MNT"
trap 'umount "$MNT" 2>/dev/null || true' EXIT

[ -f "$MNT/firstboot.sh" ] || { echo "配置光盘缺少 firstboot.sh，保持断网退出" >&2; exit 1; }
bash "$MNT/firstboot.sh" "$MNT"
