#!/usr/bin/env bash
# 在 Apple Silicon macOS 上基于 Debian 13 官方 arm64 云镜像构建 Tart Linux 黄金镜像
# 输出本地 Tart VM（默认名 cchouse-debian13-arm64），可直接 tart clone 或 tart push 到 GHCR。
set -euo pipefail

VM_NAME="${VM_NAME:-cchouse-debian13-arm64}"
BUILD_VM_NAME="${VM_NAME}-building"
WORK_DIR="${WORK_DIR:-$HOME/.cache/cchouse-tart-linux-build}"
DEFAULT_CLOUD_IMAGE_VERSION="20261123-2671"
DEFAULT_CLOUD_IMAGE_SHA512="800e63be2901b32689ea00a4a6eaab0df5b7ef43eb543cf59627b575cd9c279d93d791be96ba9306617d28e1973f9222d3ab490138117ee41eb7432f61e5f55f"
CLOUD_IMAGE_VERSION="${CLOUD_IMAGE_VERSION:-$DEFAULT_CLOUD_IMAGE_VERSION}"
CLOUD_IMAGE_URL="${CLOUD_IMAGE_URL:-https://cloud.debian.org/images/cloud/trixie/${CLOUD_IMAGE_VERSION}/debian-13-generic-arm64-${CLOUD_IMAGE_VERSION}.tar.xz}"
CLOUD_IMAGE_SHA512="${CLOUD_IMAGE_SHA512:-$DEFAULT_CLOUD_IMAGE_SHA512}"
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
REPO_ROOT="$(cd "${GUEST_LINUX_DIR}/.." && pwd)"
if [ -z "${SOURCE_COMMIT:-}" ]; then
  SOURCE_COMMIT="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
  if [ "$SOURCE_COMMIT" != "unknown" ] && [ -n "$(git -C "$REPO_ROOT" status --porcelain --untracked-files=no 2>/dev/null || true)" ]; then
    SOURCE_COMMIT="${SOURCE_COMMIT}-dirty"
  fi
fi
BUILD_TIME="${BUILD_TIME:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"

[ -x "$TART_BIN" ] || { echo "错误：未找到 tart：$TART_BIN" >&2; exit 1; }
[ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ] || {
  echo "错误：本脚本需在 Apple Silicon macOS (arm64) 上运行" >&2
  exit 1
}

if [ "$CLOUD_IMAGE_VERSION" != "$DEFAULT_CLOUD_IMAGE_VERSION" ] && [ "$CLOUD_IMAGE_SHA512" = "$DEFAULT_CLOUD_IMAGE_SHA512" ]; then
  echo "错误：自定义 CLOUD_IMAGE_VERSION=${CLOUD_IMAGE_VERSION} 时必须同步传入对应的 CLOUD_IMAGE_SHA512" >&2
  exit 1
fi

mkdir -p "$WORK_DIR"
CLOUD_BASENAME="$(basename "$CLOUD_IMAGE_URL")"
case "$CLOUD_BASENAME" in
  *.tar.xz|*.raw) ;;
  *)
    echo "错误：不支持的云镜像文件格式：${CLOUD_BASENAME}（仅支持 .tar.xz 或 .raw）" >&2
    exit 1
    ;;
esac
CLOUD_IMG="${WORK_DIR}/${CLOUD_BASENAME}"
MIHOMO_GZ="${WORK_DIR}/mihomo-linux-arm64-${MIHOMO_VERSION}.gz"

if [ ! -f "$CLOUD_IMG" ]; then
  echo "== [1/7] 下载 Debian 13 官方 arm64 压缩云镜像 (${CLOUD_BASENAME}, ~288 MB)..."
  curl -fL "$CLOUD_IMAGE_URL" -o "${CLOUD_IMG}.part"
  if ! echo "${CLOUD_IMAGE_SHA512}  ${CLOUD_IMG}.part" | shasum -a 512 -c -; then
    rm -f "${CLOUD_IMG}.part"
    echo "错误：下载的云镜像 SHA-512 校验失败，已清理临时文件" >&2
    exit 1
  fi
  mv "${CLOUD_IMG}.part" "$CLOUD_IMG"
else
  if ! echo "${CLOUD_IMAGE_SHA512}  ${CLOUD_IMG}" | shasum -a 512 -c -; then
    rm -f "$CLOUD_IMG"
    echo "错误：本地缓存云镜像 SHA-512 校验失败，已自动删除损坏缓存，请重新运行脚本" >&2
    exit 1
  fi
fi
echo "  OK  Debian 13 arm64 云镜像 SHA-512 校验通过"

if [ ! -f "$MIHOMO_GZ" ]; then
  echo "== [2/7] 下载 mihomo arm64 ${MIHOMO_VERSION}..."
  curl -fL "$MIHOMO_URL" -o "${MIHOMO_GZ}.part"
  if ! echo "${MIHOMO_SHA256}  ${MIHOMO_GZ}.part" | shasum -a 256 -c -; then
    rm -f "${MIHOMO_GZ}.part"
    echo "错误：下载的 mihomo SHA-256 校验失败，已清理临时文件" >&2
    exit 1
  fi
  mv "${MIHOMO_GZ}.part" "$MIHOMO_GZ"
else
  if ! echo "${MIHOMO_SHA256}  ${MIHOMO_GZ}" | shasum -a 256 -c -; then
    rm -f "$MIHOMO_GZ"
    echo "错误：本地缓存 mihomo SHA-256 校验失败，已自动删除损坏缓存，请重新运行脚本" >&2
    exit 1
  fi
fi
echo "  OK  mihomo arm64 SHA-256 校验通过"

echo "== [3/7] 创建临时 Tart Linux 构建 VM (${BUILD_VM_NAME}, ${DISK_SIZE_GB} GB) 并写入基础盘..."
"$TART_BIN" stop "$BUILD_VM_NAME" 2>/dev/null || true
"$TART_BIN" delete "$BUILD_VM_NAME" 2>/dev/null || true
"$TART_BIN" create "$BUILD_VM_NAME" --linux --disk-size "$DISK_SIZE_GB"
"$TART_BIN" set "$BUILD_VM_NAME" --cpu 4 --memory 4096 --display 1440x900

DISK_IMG="${TART_HOME_DIR}/vms/${BUILD_VM_NAME}/disk.img"
[ -f "$DISK_IMG" ] || { echo "错误：未找到 Tart 磁盘文件：$DISK_IMG" >&2; exit 1; }
if [[ "$CLOUD_IMG" == *.tar.xz ]]; then
  EXTRACT_DIR="$(mktemp -d "$(dirname "$DISK_IMG")/.extract.XXXXXX")"
  (
    trap 'rm -rf "$EXTRACT_DIR"' EXIT
    tar -xJf "$CLOUD_IMG" -C "$EXTRACT_DIR" disk.raw
    mv -f "${EXTRACT_DIR}/disk.raw" "$DISK_IMG"
  )
elif [[ "$CLOUD_IMG" == *.raw ]]; then
  cp -c "$CLOUD_IMG" "$DISK_IMG" 2>/dev/null || cp "$CLOUD_IMG" "$DISK_IMG"
else
  echo "错误：不支持的云镜像文件格式：${CLOUD_IMG}" >&2
  exit 1
fi
python3 -c "import os; os.truncate('${DISK_IMG}', ${DISK_SIZE_GB} * 1024 * 1024 * 1024)"

echo "== [4/7] 生成一次性 cidata ISO 与临时 SSH 密钥..."
RUN_DIR="$(mktemp -d "${WORK_DIR}/run.XXXXXX")"
TART_PID=""
BUILD_OK=0
cleanup() {
  if [ -n "$TART_PID" ] && kill -0 "$TART_PID" 2>/dev/null; then
    "$TART_BIN" stop "$BUILD_VM_NAME" 2>/dev/null || true
    wait "$TART_PID" 2>/dev/null || true
  fi
  if [ "$BUILD_OK" -ne 1 ]; then
    "$TART_BIN" delete "$BUILD_VM_NAME" 2>/dev/null || true
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
"$TART_BIN" run "$BUILD_VM_NAME" --no-graphics --disk="${CI_ISO}:ro" >"${RUN_DIR}/tart.log" 2>&1 &
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
  ip="$("$TART_BIN" ip --resolver=arp "$BUILD_VM_NAME" --wait 2 2>/dev/null || "$TART_BIN" ip "$BUILD_VM_NAME" --wait 2 2>/dev/null || true)"
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
BUILD_CMD="${MIRROR_CMD}TARGET_HYPERVISOR=tart MIHOMO_ARCH=arm64 MIHOMO_VERSION=${MIHOMO_VERSION} MIHOMO_SHA256=${MIHOMO_SHA256} MIHOMO_LOCAL_GZ=/tmp/mihomo.gz SOURCE_COMMIT=${SOURCE_COMMIT} BUILD_TIME=${BUILD_TIME} bash /tmp/cchouse-guest-linux/image-builder/build-debian13-golden.sh && rm -rf /tmp/cchouse-guest-linux /tmp/mihomo.gz && sync && { nohup sh -c 'sleep 2 && systemctl poweroff' </dev/null >/dev/null 2>&1 & }"
ssh "${SSH_OPTS[@]}" "root@${GUEST_IP}" "$BUILD_CMD"

echo "== [7/7] 等待正常关机与 Tart 落盘..."
for _ in $(seq 1 30); do
  if ! kill -0 "$TART_PID" 2>/dev/null; then
    break
  fi
  sleep 2
done
if kill -0 "$TART_PID" 2>/dev/null; then
  "$TART_BIN" stop "$BUILD_VM_NAME" || true
  wait "$TART_PID" 2>/dev/null || true
fi
TART_PID=""

"$TART_BIN" stop "$VM_NAME" 2>/dev/null || true
"$TART_BIN" delete "$VM_NAME" 2>/dev/null || true
"$TART_BIN" rename "$BUILD_VM_NAME" "$VM_NAME"
BUILD_OK=1

echo ""
echo "=== Tart Linux (Debian 13 arm64) 黄金镜像构建完成 ==="
echo "COMMIT: ${SOURCE_COMMIT}"
"$TART_BIN" list | grep -E "Source|${VM_NAME}" || true
