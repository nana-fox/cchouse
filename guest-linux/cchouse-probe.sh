#!/bin/bash
# 受控验收探针（root 所有，仅接受固定无参子命令，严禁通配符 sudoers）
set -euo pipefail

TAG="<TAG>"
CFG="/usr/local/${TAG}/config.yaml"

case "${1:-}" in
  nft-loaded)
    /usr/sbin/nft list table inet cchouse_guard >/dev/null 2>&1 && echo ok || echo no
    ;;
  nft-live-sha)
    /usr/sbin/nft list table inet cchouse_guard | sha256sum | awk '{print $1}'
    ;;
  nft-egress-sha)
    /usr/sbin/nft -n list table inet cchouse_guard | awk '/meta skuid/ && /ip daddr/ && /tcp dport/ && /accept/ {
      ip=""; port=""
      for (i=1; i<=NF; i++) {
        if ($i=="daddr" && i+1<=NF) ip=$(i+1)
        if ($i=="dport" && i+1<=NF) port=$(i+1)
      }
      if (ip != "" && port != "") print ip ":" port
    }' | sort -u | sha256sum | awk '{print $1}'
    ;;
  direct-count)
    grep -Eic '^[[:space:]]*-[[:space:]]*[^#]*,[[:space:]]*DIRECT([[:space:]]|$)' "$CFG" || true
    ;;
  mihomo-stop)
    systemctl stop cchouse-mihomo.service
    ;;
  mihomo-start)
    systemctl start cchouse-mihomo.service
    ;;
  root-egress)
    curl -s --max-time 15 https://www.cloudflare.com/cdn-cgi/trace | awk -F= '$1=="ip"{print $2}'
    ;;
  *)
    echo "unsupported subcommand" >&2
    exit 2
    ;;
esac
