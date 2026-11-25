#!/bin/bash
# 启动客机（NAT 或桥接 + 共享文件夹）；已在运行则直接退出。
# 用法：host/run-vm.sh <instance.env>
# 开机自启、桌面快捷方式都调用它，见 docs/operations.md。
set -u
cd "$(dirname "$0")/../scripts" && . ./lib.sh
load_env "${1:?用法：run-vm.sh <instance.env>}"
: "${RUN_LOG:?}"
NET_ARGS=()
case "${NETWORK_MODE:-bridged}" in
  nat) ;;  # tart 默认 NAT，不改宿主网络。
  bridged) : "${BRIDGE_IF:?}"; NET_ARGS=("--net-bridged=$BRIDGE_IF") ;;
  *) echo "NETWORK_MODE 只能为 nat 或 bridged" >&2; exit 2 ;;
esac

# 虚拟机放外置盘时，开机后等盘挂上（最多 5 分钟）
if [ -n "${VM_STORAGE:-}" ]; then
  for i in $(seq 1 60); do [ -d "$VM_STORAGE" ] && break; sleep 5; done
  [ -d "$VM_STORAGE" ] || { echo "$(date '+%F %T') 等不到 $VM_STORAGE，放弃启动" >> "$RUN_LOG"; exit 1; }
fi

"$TART_BIN" list | grep -q "^local *$VM_NAME .*running" && { echo "$VM_NAME 已在运行"; exit 0; }

# 共享文件夹清单：一行一个「名字|Mac路径|ro(可选)」，# 开头是注释
DIRS=()
if [ -f "${SHARED_DIRS_FILE:-}" ]; then
  while IFS='|' read -r name path opt; do
    [[ -z "$name" || "$name" == \#* ]] && continue
    path="${path/#\~/$HOME}"
    [ -d "$path" ] || continue
    DIRS+=("--dir=$name:$path${opt:+:$opt}")
  done < "$SHARED_DIRS_FILE"
fi

mkdir -p "$(dirname "$RUN_LOG")"
exec "$TART_BIN" run "$VM_NAME" ${NET_ARGS[@]+"${NET_ARGS[@]}"} ${DIRS[@]+"${DIRS[@]}"} >> "$RUN_LOG" 2>&1
