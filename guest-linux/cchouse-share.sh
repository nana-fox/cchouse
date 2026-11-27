#!/bin/bash
# 客机开机挂载 cchouse 配置的单个宿主共享文件夹至 /home/claude/shared。
# 兼容 Apple Silicon Tart (virtiofs) 与 Windows VirtualBox (vboxsf，含无 Guest Additions 的主线内核)。
set -euo pipefail

MOUNT_POINT="/home/claude/shared"
VIRTIOFS_ROOT="/run/cchouse-virtiofs"
VBOX_SHARE_NAME="cchouse-shared"

if ! id -u claude >/dev/null 2>&1; then
  exit 0
fi

CLAUDE_UID="$(id -u claude)"
CLAUDE_GID="$(id -g claude)"
install -d -m 755 -o claude -g claude "$MOUNT_POINT"

# 若已由 VBoxService 等挂载完成则直接退出
if mountpoint -q "$MOUNT_POINT" 2>/dev/null; then
  exit 0
fi

# 1. Apple Silicon Tart (virtiofs: com.apple.virtio-fs.automount)
if grep -qE '(^|[[:space:]])virtiofs$' /proc/filesystems 2>/dev/null || modprobe virtiofs 2>/dev/null; then
  mkdir -p "$VIRTIOFS_ROOT"
  if ! mountpoint -q "$VIRTIOFS_ROOT" 2>/dev/null; then
    mount -t virtiofs com.apple.virtio-fs.automount "$VIRTIOFS_ROOT" 2>/dev/null || true
  fi
  if mountpoint -q "$VIRTIOFS_ROOT" 2>/dev/null; then
    if [ -d "$VIRTIOFS_ROOT/shared" ]; then
      mount --bind "$VIRTIOFS_ROOT/shared" "$MOUNT_POINT"
      exit 0
    else
      umount "$VIRTIOFS_ROOT" 2>/dev/null || true
    fi
  fi
fi

# 2. VirtualBox (vboxsf: 主线内核模块或 Guest Additions)
if grep -qE '(^|[[:space:]])vboxsf$' /proc/filesystems 2>/dev/null || modprobe vboxsf 2>/dev/null; then
  if mount -t vboxsf -o "uid=${CLAUDE_UID},gid=${CLAUDE_GID},dmode=0755,fmode=0644" "$VBOX_SHARE_NAME" "$MOUNT_POINT" 2>/dev/null; then
    exit 0
  fi
  if mount -t vboxsf -o "ro,uid=${CLAUDE_UID},gid=${CLAUDE_GID},dmode=0755,fmode=0644" "$VBOX_SHARE_NAME" "$MOUNT_POINT" 2>/dev/null; then
    exit 0
  fi
fi

exit 0
