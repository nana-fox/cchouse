#!/bin/bash
# Exercise the shipped VMDK on a disposable overlay, with no real proxy credentials.
set -euo pipefail

repo=$(cd "$(dirname "$0")/../.." && pwd)
disk=$(realpath "${1:?path to the packaged VMDK}")
work=$(mktemp -d)
pid=""
cleanup() {
  local rc=$?
  if [ -n "$pid" ]; then kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true; fi
  if [ "$rc" -ne 0 ]; then
    for log in "$work"/*.serial; do
      [ ! -f "$log" ] || tail -n 80 "$log" >&2
    done
  fi
  rm -rf "$work"
}
trap cleanup EXIT

qemu-img create -q -f qcow2 -F vmdk -b "$disk" "$work/test.qcow2"
ssh-keygen -q -t ed25519 -N '' -f "$work/key"
cat >"$work/instance.env" <<EOF
GUEST_OS=linux
EXIT_MODE=single
STRIPE_EXIT_MODE=off
TAG=ovasmoke
GUEST_IF=enp0s3
VMNET_CIDR=10.0.2.0/24
HOME_LAN_CIDR=192.168.100.0/24
TIMEZONE=Etc/UTC
EXPECTED_EGRESS_IP=198.51.100.10
EXPECTED_EGRESS_ASN=AS64512
SINGLE_TYPE=socks5
SINGLE_SERVER=203.0.113.55
SINGLE_PORT=1080
SSH_PUBKEY='$(cat "$work/key.pub")'
EOF
python3 -I "$repo/scripts/render.py" "$work/instance.env" "$work/rendered"
python3 -I "$repo/scripts/make-seed-iso.py" "$work/rendered" "$work/seed.iso" --poweroff-after-provision
rm -rf "$work/rendered" "$work/instance.env"

qemu=(qemu-system-x86_64 -machine pc,accel=kvm -cpu host -smp 2 -m 2048 -display none
      -drive "file=$work/test.qcow2,format=qcow2,if=ide")
timeout -k 5s 180s "${qemu[@]}" -nic none -serial "file:$work/firstboot.serial" \
  -drive "file=$work/seed.iso,media=cdrom,readonly=on"
tr -d '\r' <"$work/firstboot.serial" >"$work/firstboot.txt"
grep -qx CCHOUSE_FIRSTBOOT_DONE "$work/firstboot.txt"
awk '/^CCHOUSE_HOSTKEY ssh-ed25519 [A-Za-z0-9+/=]+$/ { print "[127.0.0.1]:2222", $2, $3; exit }' \
  "$work/firstboot.txt" >"$work/known_hosts"
test -s "$work/known_hosts"
rm "$work/seed.iso"
echo "PASS: Offline provisioning confirmed completion and powered off"

"${qemu[@]}" -serial "file:$work/reboot.serial" \
  -netdev user,id=nic0,restrict=on,hostfwd=tcp:127.0.0.1:2222-:22 \
  -device e1000,netdev=nic0,addr=3 &
pid=$!
timeout -k 5s 180s bash -c '
  key=$1
  pid=$2
  while kill -0 "$pid" 2>/dev/null; do
    if timeout 10s ssh -F none -p 2222 -i "$key" -o BatchMode=yes -o ConnectTimeout=5 \
      -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=\"${key%/*}/known_hosts\"" claude@127.0.0.1 true; then
      exit 0
    fi
    sleep 2
  done
  exit 1
' _ "$work/key" "$pid"
timeout 10s ssh -F none -p 2222 -i "$work/key" -o BatchMode=yes -o ConnectTimeout=5 \
  -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=\"$work/known_hosts\"" claude@127.0.0.1 '
    set -e
    test -f /var/lib/cchouse/firstboot.done
    test "$(systemctl show -p ActiveState --value cchouse-firstboot.service)" = inactive
    test "$(sudo -n /usr/local/sbin/cchouse-probe nft-loaded)" = ok
    test "$(sudo -n /usr/local/sbin/cchouse-probe nft-live-sha)" = "$(cat /var/lib/cchouse/expected-nft-live.sha256)"
  '
echo "PASS: Seedless reboot accepted pinned SSH and loaded the instance firewall"
