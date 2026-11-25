#!/bin/bash
# 正常关闭客机：先 sync，再在客机里 shutdown，等 tart 显示 stopped；失败保留运行。
# 用法：host/stop-vm.sh <instance.env> [当前客机 IP，例如 NAT IP]
# 不要直接 `tart stop`：它会在客机把刚写的文件落盘之前就停掉虚拟机，
# 实测刚装进去几十秒的文件重启后全丢（2026-11-15 ex01）。
set -u
cd "$(dirname "$0")/../scripts" && . ./lib.sh
load_env "${1:?用法：stop-vm.sh <instance.env>}"
GUEST_IP="${2:-${GUEST_IP:-}}"
: "${GUEST_IP:?请提供当前客机 IP；NAT 阶段用第二个参数指定}"
ssh_opts
running() {
  local listing
  listing=$("$TART_BIN" list) || { echo "读取虚拟机状态失败，停止关机流程" >&2; exit 1; }
  printf '%s\n' "$listing" | grep -q "^local *$VM_NAME .*running"
}
running || { echo "$VM_NAME 没在运行"; exit 0; }
guest 'sync && sudo -n true' >/dev/null 2>&1 || {
  echo "无法确认客机 $GUEST_IP 可安全关机；保留运行，请核对地址或通过桌面关机" >&2
  exit 1
}
# 关机会断开这条 SSH，退出码非 0 是正常的，不据此判断
guest 'sudo -n shutdown -h now' >/dev/null 2>&1; echo "已请求客机关机，等待 $VM_NAME 停止"
for i in $(seq 1 24); do running || { echo "$VM_NAME 已关机"; exit 0; }; sleep 5; done
echo "120 秒内未确认关机；保留运行，请通过客机桌面核实，不自动强制停机" >&2
exit 1
