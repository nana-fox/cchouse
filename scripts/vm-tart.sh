#!/bin/bash
# Tart (Apple Silicon) 客机生命周期公共函数：tools/e2e/run-mac.sh 以 source 方式复用，
# scripts/cchouse.py up 以子命令方式调用（bash scripts/vm-tart.sh <op> ...）。
# 兼容 macOS 自带 bash 3.2；不打印任何凭据。
#
# 子命令：
#   exists    <VM>
#   import    <VM> <file.tvm>                 # 已存在则跳过
#   clone     <SRC> <DST>                     # DST 已存在先删除
#   firstboot <VM> <seed.iso> <SSH_KEY> [LOG]  # 挂只读 seed.iso 离线首启注入，完成后关机
#   boot      <VM> <SSH_KEY> <KNOWN_HOSTS> <IP_FILE> [LOG]  # 联网后台开机，等 SSH，写 known_hosts 与 IP
#   gui       <VM> [LOG]                      # 带图形界面后台开机（交给用户登录）
#   stop      <VM>
#   snapshot  <VM> <SNAP>                     # 关机后 tart clone 成干净快照（已存在先删除）
#   delete    <VM>

CCHOUSE_TART_PID=""

cchouse_tart_ip() {
  tart ip "$1" --wait 2 2>/dev/null || tart ip --resolver=arp "$1" --wait 2 2>/dev/null || true
}

# cchouse_tart_ssh <SSH_KEY> <IP> <CMD> — 首启/开机探测用，不信任主机公钥（尚未写 known_hosts）
cchouse_tart_ssh() {
  ssh -F none -i "$1" -o BatchMode=yes -o ConnectTimeout=3 \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR \
    "claude@$2" "$3" 2>/dev/null
}

cchouse_tart_exists() {
  tart get "$1" >/dev/null 2>&1
}

cchouse_tart_import() {
  local vm="$1" tvm="$2"
  if cchouse_tart_exists "$vm"; then
    echo "Tart VM $vm already exists; skip import."
    return 0
  fi
  [ -f "$tvm" ] || { echo "TVM not found: $tvm"; return 1; }
  echo "Importing $tvm -> $vm..."
  tart import "$vm" "$tvm"
}

cchouse_tart_clone() {
  local src="$1" dst="$2"
  if cchouse_tart_exists "$dst"; then
    tart stop "$dst" >/dev/null 2>&1 || true
    tart delete "$dst" || return 1
  fi
  echo "Cloning $src -> $dst..."
  tart clone "$src" "$dst"
}

cchouse_tart_firstboot() {
  local vm="$1" iso="$2" key="$3" runlog="${4:-/dev/null}" tries="${CCHOUSE_FIRSTBOOT_TRIES:-30}"
  local ip="" done_ok=0 i
  [ -f "$iso" ] || { echo "seed.iso not found: $iso"; return 1; }
  tart run --no-graphics --disk "$iso:ro" "$vm" >"$runlog" 2>&1 &
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
  tart stop "$vm" || true
  wait "$CCHOUSE_TART_PID" 2>/dev/null || true
  CCHOUSE_TART_PID=""
  if [ "$done_ok" = "0" ]; then
    echo "Note: firstboot SSH probe did not complete within $((tries * 2))s (may still have completed offline); proceeding to reboot check."
  fi
  return 0
}

cchouse_tart_boot() {
  local vm="$1" key="$2" kh="$3" ip_file="$4" runlog="${5:-/dev/null}" tries="${CCHOUSE_BOOT_TRIES:-40}"
  local ip="" ssh_ok=0 i w
  nohup tart run --no-graphics "$vm" >"$runlog" 2>&1 &
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
         -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$kh" \
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
  nohup tart run "$vm" >"$runlog" 2>&1 &
  CCHOUSE_TART_PID=$!
  echo "Started $vm with graphics (pid=$CCHOUSE_TART_PID)."
}

cchouse_tart_stop() {
  local vm="$1" i
  tart stop "$vm" >/dev/null 2>&1 || true
  for i in $(seq 1 15); do
    tart list 2>/dev/null | awk -v n="$vm" '$2 == n && $NF == "running" {f=1} END {exit !f}' || return 0
    sleep 2
  done
  echo "VM $vm still running after stop"
  return 1
}

cchouse_tart_snapshot() {
  local vm="$1" snap="$2"
  cchouse_tart_stop "$vm" || return 1
  if cchouse_tart_exists "$snap"; then
    tart delete "$snap" || return 1
  fi
  echo "Saving clean snapshot $vm -> $snap..."
  tart clone "$vm" "$snap"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  set -uo pipefail
  op="${1:-}"
  [ $# -gt 0 ] && shift
  case "$op" in
    exists) cchouse_tart_exists "$@" ;;
    import) cchouse_tart_import "$@" ;;
    clone) cchouse_tart_clone "$@" ;;
    firstboot) cchouse_tart_firstboot "$@" ;;
    boot) cchouse_tart_boot "$@" ;;
    gui) cchouse_tart_gui "$@" ;;
    stop) cchouse_tart_stop "$@" ;;
    snapshot) cchouse_tart_snapshot "$@" ;;
    delete) tart stop "$1" >/dev/null 2>&1 || true; tart delete "$1" ;;
    *) echo "Usage: $0 exists|import|clone|firstboot|boot|gui|stop|snapshot|delete ..." >&2; exit 2 ;;
  esac
fi
