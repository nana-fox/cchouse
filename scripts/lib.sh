# 公共函数：读 instance.env、拼 SSH 参数。被其他脚本 source。

load_env() {
  [ -f "$1" ] || { echo "找不到 instance.env：$1" >&2; exit 2; }
  set -a; . "$1"; set +a
  : "${TAG:?instance.env 缺 TAG}" "${VM_NAME:?}" "${TART_BIN:?}"
  # NAT 地址可能在重启后变化；只对明确选择 NAT 的实例查当前地址。
  if [ "${NETWORK_MODE:-bridged}" = nat ]; then
    local current_ip
    current_ip=$("$TART_BIN" ip "$VM_NAME" 2>/dev/null || true)
    case "$current_ip" in
      [0-9]*.[0-9]*.[0-9]*.[0-9]*) GUEST_IP="$current_ip" ;;
    esac
  fi
}

# -F none：不读 ~/.ssh/config。那里可能有代理、Include 了别的文件（launchd 下读不到会直接报错退出），
# 客机连接只用这里的参数，结果不随宿主配置变化
ssh_opts() {
  # ssh 会再次解析 -o 的值；内层引号防止 Application Support 被拆成两个文件。
  SSH_OPTS=(-F none -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15
            -o StrictHostKeyChecking=accept-new -o "UserKnownHostsFile=\"$SSH_KNOWN_HOSTS\""
            -o IdentitiesOnly=yes -i "$SSH_KEY")
}

guest() { ssh "${SSH_OPTS[@]}" "claude@$GUEST_IP" "$@"; }
