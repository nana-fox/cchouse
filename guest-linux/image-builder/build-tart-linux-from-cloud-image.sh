#!/usr/bin/env bash
# 在 Apple Silicon macOS 上基于 Debian 13 官方 arm64 云镜像构建 Tart Linux 黄金镜像
# 输出本地 Tart VM（默认名 cchouse-debian13-arm64），可直接 tart clone 或 tart push 到 GHCR。
set -euo pipefail

VM_NAME="${VM_NAME:-cchouse-debian13-arm64}"
WORK_DIR="${WORK_DIR:-$HOME/.cache/cchouse-tart-linux-build}"
CLOUD_IMAGE_URL="${CLOUD_IMAGE_URL:-https://cloud.debian.org/images/cloud/trixie/latest/debian-13-generic-arm64.raw}"
CLOUD_IMAGE_SHA512="${CLOUD_IMAGE_SHA512:-056644b92b6e70c96b8f6bd4b9c5635d61d8193f768489dd62ee33dd7251055ead9db6c83619345beae146f8782e5877a660590320c060471814ee0e88246495}"
MIHOMO_VERSION="${MIHOMO_VERSION:-v1.19.19}"
MIHOMO_SHA256="${MIHOMO_SHA256:-5035df7d6ae01ab33090fe612ec1b98ccc68917f03a97fb62ed474dc0b99cdcc}"
MIHOMO_URL="https://github.com/MetaCubeX/mihomo/releases/download/${MIHOMO_VERSION}/mihomo-linux-arm64-${MIHOMO_VERSION}.gz"
DISK_SIZE_GB="${DISK_SIZE_GB:-24}"
TART_BIN="${TART_BIN:-$(command -v tart || echo "$HOME/.local/bin/tart")}"
if [ -n "${TART_HOME:-}" ]; then
  TART_HOME_DIR="$TART_HOME"
elif [ -f "$TART_BIN" ] && head -n 1 "$TART_BIN" 2>/dev/null | grep -q '^#!'; then
  TART_HOME_DIR="$(sed -nE 's/^[[:space:]]*(export[[:space:]]+)?TART_HOME="?([^"]+)"?.*$/\2/p' "$TART_BIN" | head -n 1)"
  TART_HOME_DIR="${TART_HOME_DIR:-$HOME/.tart}"
else
  TART_HOME_DIR="$HOME/.tart"
fi
USE_USTC_MIRROR="${USE_USTC_MIRROR:-0}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
GUEST_LINUX_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

[ -x "$TART_BIN" ] || { echo "错误：未找到 tart：$TART_BIN" >&2; exit 1; }
[ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ] || {
  echo "错误：本脚本需在 Apple Silicon macOS (arm64) 上运行" >&2
  exit 1
}

mkdir -p "$WORK_DIR"
RAW_IMG="${WORK_DIR}/debian-13-generic-arm64.raw"
MIHOMO_GZ="${WORK_DIR}/mihomo-linux-arm64-${MIHOMO_VERSION}.gz"

if [ ! -f "$RAW_IMG" ]; then
  echo "== [1/7] 下载 Debian 13 官方 arm64 云镜像 (~330 MB)..."
  curl -fL "$CLOUD_IMAGE_URL" -o "$RAW_IMG"
fi
echo "${CLOUD_IMAGE_SHA512}  ${RAW_IMG}" | shasum -a 512 -c -
echo "  OK  Debian 13 arm64 raw SHA-512 校验通过"

if [ ! -f "$MIHOMO_GZ" ]; then
  echo "== [2/7] 下载 mihomo arm64 ${MIHOMO_VERSION}..."
  curl -fL "$MIHOMO_URL" -o "$MIHOMO_GZ"
fi
echo "${MIHOMO_SHA256}  ${MIHOMO_GZ}" | shasum -a 256 -c -
echo "  OK  mihomo arm64 SHA-256 校验通过"

echo "== [3/7] 创建 Tart Linux VM (${VM_NAME}, ${DISK_SIZE_GB} GB) 并写入基础盘..."
"$TART_BIN" stop "$VM_NAME" 2>/dev/null || true
"$TART_BIN" delete "$VM_NAME" 2>/dev/null || true
"$TART_BIN" create "$VM_NAME" --linux --disk-size "$DISK_SIZE_GB"
"$TART_BIN" set "$VM_NAME" --cpu 4 --memory 4096 --display 1440x900

DISK_IMG="${TART_HOME_DIR}/vms/${VM_NAME}/disk.img"
[ -f "$DISK_IMG" ] || { echo "错误：未找到 Tart 磁盘文件：$DISK_IMG" >&2; exit 1; }
cp -c "$RAW_IMG" "$DISK_IMG" 2>/dev/null || cp "$RAW_IMG" "$DISK_IMG"
python3 -c "import os; os.truncate('${DISK_IMG}', ${DISK_SIZE_GB} * 1024 * 1024 * 1024)"

echo "== [4/7] 生成一次性 cidata ISO 与临时 SSH 密钥..."
RUN_DIR="$(mktemp -d "${WORK_DIR}/run.XXXXXX")"
TART_PID=""
cleanup() {
  if [ -n "$TART_PID" ] && kill -0 "$TART_PID" 2>/dev/null; then
    "$TART_BIN" stop "$VM_NAME" 2>/dev/null || true
    wait "$TART_PID" 2>/dev/null || true
  fi
  rm -rf "$RUN_DIR"
}
trap cleanup EXIT

SSH_KEY="${RUN_DIR}/build_ed25519"
ssh-keygen -q -t ed25519 -N "" -f "$SSH_KEY"
PUB_KEY="$(cat "${SSH_KEY}.pub")"

CI_DIR="${RUN_DIR}/cidata"
mkdir -p "$CI_DIR"
printf 'instance-id: cchouse-arm64-build-01\nlocal-hostname: debian\n' >"${CI_DIR}/meta-data"
cat >"${CI_DIR}/user-data" <<EOF
#cloud-config
disable_root: false
ssh_pwauth: false
users:
  - name: root
    ssh_authorized_keys:
      - ${PUB_KEY}
growpart:
  mode: auto
  devices: ['/']
resize_rootfs: true
EOF

CI_ISO="${RUN_DIR}/cidata.iso"
hdiutil makehybrid -o "$CI_ISO" -joliet -iso -default-volume-name cidata "$CI_DIR" >/dev/null

echo "== [5/7] 启动临时 Tart 构建机并等待 SSH..."
"$TART_BIN" run "$VM_NAME" --no-graphics --disk="${CI_ISO}:ro" >"${RUN_DIR}/tart.log" 2>&1 &
TART_PID=$!

SSH_OPTS=(-i "$SSH_KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=5 -o BatchMode=yes)
GUEST_IP=""
SSH_READY=0
for _ in $(seq 1 60); do
  if ! kill -0 "$TART_PID" 2>/dev/null; then
    echo "错误：Tart 构建机提前退出：" >&2
    cat "${RUN_DIR}/tart.log" >&2
    exit 1
  fi
  ip="$("$TART_BIN" ip --resolver=arp "$VM_NAME" --wait 2 2>/dev/null || "$TART_BIN" ip "$VM_NAME" --wait 2 2>/dev/null || true)"
  if [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    GUEST_IP="$ip"
    if ssh "${SSH_OPTS[@]}" "root@${GUEST_IP}" true 2>/dev/null; then
      SSH_READY=1
      break
    fi
  fi
  sleep 2
done
[ "$SSH_READY" -eq 1 ] || { echo "错误：120 秒内未获取到客机 SSH（IP: ${GUEST_IP}）" >&2; exit 1; }
echo "  OK  客机 SSH 已就绪：${GUEST_IP}"

echo "== [6/7] 注入脚本与 mihomo，执行 build-debian13-golden.sh (tart/arm64)..."
ssh "${SSH_OPTS[@]}" "root@${GUEST_IP}" "mkdir -p /tmp/cchouse-guest-linux/image-builder"
scp "${SSH_OPTS[@]}" \
  "${GUEST_LINUX_DIR}/nftables-default-drop.conf" \
  "${GUEST_LINUX_DIR}/cchouse-firstboot.service" \
  "root@${GUEST_IP}:/tmp/cchouse-guest-linux/"
scp "${SSH_OPTS[@]}" \
  "${SCRIPT_DIR}/build-debian13-golden.sh" \
  "${SCRIPT_DIR}/assert-empty-state.sh" \
  "${SCRIPT_DIR}/cchouse-firstboot-loader.sh" \
  "root@${GUEST_IP}:/tmp/cchouse-guest-linux/image-builder/"
scp "${SSH_OPTS[@]}" "$MIHOMO_GZ" "root@${GUEST_IP}:/tmp/mihomo.gz"

MIRROR_CMD=""
if [ "$USE_USTC_MIRROR" = "1" ]; then
  MIRROR_CMD="sed -i 's|deb\\.debian\\.org|mirrors.ustc.edu.cn|g; s|security\\.debian\\.org|mirrors.ustc.edu.cn|g' /etc/apt/sources.list /etc/apt/sources.list.d/* /etc/apt/mirrors/* 2>/dev/null || true; "
fi
BUILD_CMD="${MIRROR_CMD}TARGET_HYPERVISOR=tart MIHOMO_ARCH=arm64 MIHOMO_VERSION=${MIHOMO_VERSION} MIHOMO_SHA256=${MIHOMO_SHA256} MIHOMO_LOCAL_GZ=/tmp/mihomo.gz bash /tmp/cchouse-guest-linux/image-builder/build-debian13-golden.sh && rm -rf /tmp/cchouse-guest-linux /tmp/mihomo.gz"
ssh "${SSH_OPTS[@]}" "root@${GUEST_IP}" "$BUILD_CMD"

echo "== [7/7] 正常关机并等待 Tart 落盘..."
ssh "${SSH_OPTS[@]}" "root@${GUEST_IP}" "systemctl poweroff" 2>/dev/null || true
for _ in $(seq 1 30); do
  if ! kill -0 "$TART_PID" 2>/dev/null; then
    break
  fi
  sleep 2
done
if kill -0 "$TART_PID" 2>/dev/null; then
  "$TART_BIN" stop "$VM_NAME" || true
  wait "$TART_PID" 2>/dev/null || true
fi
TART_PID=""

echo ""
echo "=== Tart Linux (Debian 13 arm64) 黄金镜像构建完成 ==="
"$TART_BIN" list | grep -E "Source|${VM_NAME}" || true
