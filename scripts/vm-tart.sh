#!/bin/bash
# Tart (Apple Silicon) 客机生命周期公共函数：tools/e2e/run-mac.sh 以 source 方式复用，
# scripts/cchouse.py up 以子命令方式调用（bash scripts/vm-tart.sh <op> ...）。
# 兼容 macOS 自带 bash 3.2；不打印任何凭据。
#
# 子命令：
#   exists    <VM>
#   status    <VM>                            # 输出 running | stopped | missing
#   import    <VM> <file.tvm>                 # 已存在则跳过
#   import-new <VM> <file.tvm>                # 已存在则拒绝
#   clone     <SRC> <DST>                     # DST 已存在先删除
#   clone-new <SRC> <DST>                     # DST 已存在则拒绝
#   firstboot <VM> <seed.iso> <SSH_KEY> [LOG]  # 挂只读 seed.iso 离线首启注入，完成后关机
#   boot      <VM> <SSH_KEY> <KNOWN_HOSTS> <IP_FILE> [LOG]  # 联网后台开机，等 SSH，写 known_hosts 与 IP
#   gui       <VM> [LOG]                      # 带图形界面后台开机（交给用户登录）
#   open      <VM> [LOG]                      # 已运行则不重复开；已关机则带图形界面启动
#   stop      <VM>
#   restore   <VM> <SNAP>                     # 停机后从干净快照克隆恢复 <VM>
#   snapshot  <VM> <SNAP>                     # 关机后 tart clone 成干净快照（已存在先删除）
#   snapshot-new <VM> <SNAP>                  # 已存在则拒绝
#   delete    <VM>

CCHOUSE_TART_PID=""
CCHOUSE_TART_CMD=""

cchouse_resolve_tart() {
  if [ -n "${CCHOUSE_TART_BIN:-}" ]; then
    if [ ! -x "$CCHOUSE_TART_BIN" ]; then
      echo "Pinned tart binary not found or not executable: $CCHOUSE_TART_BIN" >&2
      return 1
    fi
    CCHOUSE_TART_CMD="$CCHOUSE_TART_BIN"
    return 0
  fi
  if command -v tart >/dev/null 2>&1; then
    CCHOUSE_TART_CMD="$(command -v tart)"
    return 0
  fi
  local cand
  for cand in "$HOME/.local/bin/tart" "/opt/homebrew/bin/tart"; do
    if [ -x "$cand" ]; then
      CCHOUSE_TART_CMD="$cand"
      return 0
    fi
  done
  for cand in "$HOME/Library/Application Support/cchouse/bootstrap/tools"/tart-*/Contents/MacOS/tart; do
    if [ -x "$cand" ]; then
      CCHOUSE_TART_CMD="$cand"
      return 0
    fi
  done
  CCHOUSE_TART_CMD="tart"
  return 0
}

cchouse_ensure_tart_in_path() {
  cchouse_resolve_tart
}

cchouse_tart() {
  if [ -z "$CCHOUSE_TART_CMD" ]; then
    cchouse_resolve_tart || return 1
  fi
  "$CCHOUSE_TART_CMD" "$@"
}

cchouse_tart_ip() {
  cchouse_tart ip "$1" --wait 2 2>/dev/null || cchouse_tart ip --resolver=arp "$1" --wait 2 2>/dev/null || true
}

# cchouse_tart_ssh <SSH_KEY> <IP> <CMD> — 首启/开机探测用，不信任主机公钥（尚未写 known_hosts）
cchouse_tart_ssh() {
  ssh -F none -i "$1" -o BatchMode=yes -o ConnectTimeout=3 \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR \
    "claude@$2" "$3" 2>/dev/null
}

cchouse_tart_exists() {
  cchouse_tart get "$1" >/dev/null 2>&1
}

cchouse_tart_import() {
  local vm="$1" tvm="$2"
  if cchouse_tart_exists "$vm"; then
    echo "Tart VM $vm already exists; skip import."
    return 0
  fi
  [ -f "$tvm" ] || { echo "TVM not found: $tvm"; return 1; }
  echo "Importing $tvm -> $vm..."
  cchouse_tart import "$tvm" "$vm"  # tart import <path> <name>
}

cchouse_tart_clone() {
  local src="$1" dst="$2"
  if cchouse_tart_exists "$dst"; then
    cchouse_tart stop "$dst" >/dev/null 2>&1 || true
    cchouse_tart delete "$dst" || return 1
  fi
  echo "Cloning $src -> $dst..."
  cchouse_tart clone "$src" "$dst"
}

cchouse_tart_firstboot() {
  local vm="$1" iso="$2" key="$3" runlog="${4:-/dev/null}" tries="${CCHOUSE_FIRSTBOOT_TRIES:-30}"
  local ip="" done_ok=0 i
  [ -f "$iso" ] || { echo "seed.iso not found: $iso"; return 1; }
  cchouse_resolve_tart || return 1
  cchouse_tart run --no-graphics --disk "$iso:ro" "$vm" >"$runlog" 2>&1 &
  CCHOUSE_TART_PID=$!
  echo "Started firstboot VM (pid=$CCHOUSE_TART_PID), waiting for injection..."
  for i in $(seq 1 "$tries"); do
    sleep 2
    if ! kill -0 "$CCHOUSE_TART_PID" 2>/dev/null; then
      echo "Tart firstboot process exited early"
      break
    fi
    [ -n "$ip" ] || ip="$(cchouse_tart_ip "$vm")"
    if [ -n "$ip" ] && cchouse_tart_ssh "$key" "$ip" "test -f /var/lib/cchouse/firstboot.done && test -x /usr/local/bin/claude"; then
      echo "Firstboot completed at $((i * 2))s (ip=$ip)"
      done_ok=1
      break
    fi
  done
  cchouse_tart stop "$vm" || true
  wait "$CCHOUSE_TART_PID" 2>/dev/null || true
  CCHOUSE_TART_PID=""
  if [ "$done_ok" = "0" ]; then
    echo "Note: firstboot SSH probe did not complete within $((tries * 2))s (may still have completed offline); proceeding to reboot check."
  fi
  return 0
}

cchouse_tart_dir_args() {
  CCHOUSE_TART_DIR_ARGS=()
  if [ -n "${CCHOUSE_SHARE_PATH:-}" ]; then
    if [ "${CCHOUSE_SHARE_RO:-0}" = "1" ]; then
      CCHOUSE_TART_DIR_ARGS=("--dir=shared:${CCHOUSE_SHARE_PATH}:ro")
    else
      CCHOUSE_TART_DIR_ARGS=("--dir=shared:${CCHOUSE_SHARE_PATH}")
    fi
  fi
}

cchouse_tart_boot() {
  local vm="$1" key="$2" kh="$3" ip_file="$4" runlog="${5:-/dev/null}" tries="${CCHOUSE_BOOT_TRIES:-40}"
  local ip="" ssh_ok=0 i w
  cchouse_resolve_tart || return 1
  cchouse_tart_dir_args
  nohup "$CCHOUSE_TART_CMD" run --no-graphics ${CCHOUSE_TART_DIR_ARGS[@]+"${CCHOUSE_TART_DIR_ARGS[@]}"} "$vm" >"$runlog" 2>&1 &
  CCHOUSE_TART_PID=$!
  echo "Started networked VM (pid=$CCHOUSE_TART_PID), waiting for IP + SSH..."
  for i in $(seq 1 "$tries"); do
    sleep 2
    if ! kill -0 "$CCHOUSE_TART_PID" 2>/dev/null; then
      echo "Tart VM exited unexpectedly!"
      cat "$runlog" 2>/dev/null || true
      return 1
    fi
    [ -n "$ip" ] || ip="$(cchouse_tart_ip "$vm")"
    if [ -n "$ip" ] && cchouse_tart_ssh "$key" "$ip" "true"; then
      ssh_ok=1
      break
    fi
  done
  if [ "$ssh_ok" != "1" ]; then
    echo "Timed out waiting for SSH on $vm (last ip=${ip:-none})"
    return 1
  fi
  echo "SSH ready: claude@$ip"
  ssh-keyscan -t ed25519 "$ip" >"$kh" 2>/dev/null
  chmod 600 "$kh"
  printf '%s' "$ip" >"$ip_file"
  # Wait briefly for mihomo TUN fallback probe to warm up before verify-linux.sh
  for w in $(seq 1 10); do
    if ssh -F none -i "$key" -o BatchMode=yes -o ConnectTimeout=3 \
         -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=\"$kh\"" \
         "claude@$ip" "curl -s --max-time 6 https://www.cloudflare.com/cdn-cgi/trace | grep -q '^ip='" 2>/dev/null; then
      echo "mihomo egress warmed up (attempt $w)"
      break
    fi
    sleep 2
  done
  return 0
}

cchouse_tart_gui() {
  local vm="$1" runlog="${2:-/dev/null}"
  cchouse_resolve_tart || return 1
  cchouse_tart_dir_args
  nohup "$CCHOUSE_TART_CMD" run ${CCHOUSE_TART_DIR_ARGS[@]+"${CCHOUSE_TART_DIR_ARGS[@]}"} "$vm" >"$runlog" 2>&1 &
  CCHOUSE_TART_PID=$!
  echo "Started $vm with graphics (pid=$CCHOUSE_TART_PID)."
}

cchouse_tart_stop() {
  local vm="$1" local_vms i
  cchouse_tart stop "$vm" >/dev/null 2>&1 || true
  for i in $(seq 1 15); do
    local_vms="$(cchouse_tart list --source local)" || return 1
    printf '%s\n' "$local_vms" | awk -v n="$vm" '$2 == n && $NF == "running" {f=1} END {exit !f}' || return 0
    sleep 2
  done
  echo "VM $vm still running after stop"
  return 1
}

cchouse_tart_snapshot() {
  local vm="$1" snap="$2"
  cchouse_tart_stop "$vm" || return 1
  if cchouse_tart_exists "$snap"; then
    cchouse_tart delete "$snap" || return 1
  fi
  echo "Saving clean snapshot $vm -> $snap..."
  cchouse_tart clone "$vm" "$snap"
}

cchouse_tart_status() {
  local vm="$1" local_vms state
  if ! cchouse_tart_exists "$vm"; then
    echo "missing"
    return 0
  fi
  local_vms="$(cchouse_tart list --source local 2>/dev/null || true)"
  state="$(printf '%s\n' "$local_vms" | awk -v n="$vm" '$2 == n {print $NF; exit}')"
  if [ "$state" = "running" ]; then
    echo "running"
  else
    echo "stopped"
  fi
}

cchouse_tart_open() {
  local vm="$1" runlog="${2:-/dev/null}" st
  st="$(cchouse_tart_status "$vm")"
  if [ "$st" = "missing" ]; then
    echo "Tart VM $vm does not exist." >&2
    return 1
  fi
  if [ "$st" = "running" ]; then
    echo "VM $vm is already running."
    return 0
  fi
  cchouse_tart_gui "$vm" "$runlog"
}

cchouse_tart_restore() {
  local vm="$1" snap="$2"
  if ! cchouse_tart_exists "$snap"; then
    echo "Clean snapshot $snap does not exist." >&2
    return 1
  fi
  if cchouse_tart_exists "$vm"; then
    cchouse_tart_stop "$vm" || return 1
    cchouse_tart delete "$vm" || return 1
  fi
  echo "Restoring $vm from clean snapshot $snap..."
  cchouse_tart clone "$snap" "$vm"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  set -uo pipefail
  op="${1:-}"
  [ $# -gt 0 ] && shift
  cchouse_resolve_tart || exit 1
  case "$op" in
    exists) cchouse_tart_exists "$@" ;;
    status) cchouse_tart_status "$@" ;;
    import) cchouse_tart_import "$@" ;;
    import-new) cchouse_tart import "$2" "$1" ;;
    clone) cchouse_tart_clone "$@" ;;
    clone-new) cchouse_tart clone "$1" "$2" ;;
    firstboot) cchouse_tart_firstboot "$@" ;;
    boot) cchouse_tart_boot "$@" ;;
    gui) cchouse_tart_gui "$@" ;;
    open) cchouse_tart_open "$@" ;;
    stop) cchouse_tart_stop "$@" ;;
    restore) cchouse_tart_restore "$@" ;;
    snapshot) cchouse_tart_snapshot "$@" ;;
    snapshot-new) cchouse_tart_stop "$1" && cchouse_tart clone "$1" "$2" ;;
    delete) cchouse_tart_stop "$1" && cchouse_tart delete "$1" ;;
    *) echo "Usage: $0 exists|status|import|import-new|clone|clone-new|firstboot|boot|gui|open|stop|restore|snapshot|snapshot-new|delete ..." >&2; exit 2 ;;
  esac
fi
