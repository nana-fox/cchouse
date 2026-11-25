#!/bin/bash
# Debian 13 (v2 Linux) 客机自动验收脚本：全部通过退出码 0。
# Windows 请在 Git Bash 中运行；不依赖 TART_BIN，所有特权项走无通配符 /usr/local/sbin/cchouse-probe。
# 用法：scripts/verify-linux.sh <instance.env> [--fail-closed] [--json]
set -uo pipefail

ENV_FILE="${1:?用法：verify-linux.sh <instance.env> [--fail-closed] [--json]}"
shift
FAIL_CLOSED=0
JSON_OUT=0
for arg in "$@"; do
  case "$arg" in
    --fail-closed) FAIL_CLOSED=1 ;;
    --json) JSON_OUT=1 ;;
    *) echo "未知参数：$arg" >&2; exit 2 ;;
  esac
done

[ -f "$ENV_FILE" ] || { echo "找不到 $ENV_FILE" >&2; exit 1; }
# 独立加载 Linux env（兼容 macOS Bash 3.2，不用进程替换 . <(...)；同时清 UTF-8 BOM 与 Windows CRLF）
TMP_ENV="$(mktemp)"
chmod 600 "$TMP_ENV"
cleanup_all() {
  rm -f "$TMP_ENV"
  if ! restore_mihomo; then
    echo "无法自动恢复 cchouse-mihomo.service，请在客机控制台恢复" >&2
  fi
}
python3 -I -c 'import sys
from pathlib import Path
raw = Path(sys.argv[1]).read_text(encoding="utf-8-sig").replace("\r\n", "\n")
Path(sys.argv[2]).write_bytes(raw.encode("utf-8"))' "$ENV_FILE" "$TMP_ENV" 2>/dev/null \
  || tr -d '\r' <"$ENV_FILE" | sed '1s/^\xef\xbb\xbf//' >"$TMP_ENV"
set -a
# shellcheck disable=SC1090
. "$TMP_ENV"
set +a
rm -f "$TMP_ENV"

: "${TAG:?instance.env 缺 TAG}" "${GUEST_IP:?instance.env 缺 GUEST_IP}" \
  "${SSH_KEY:?instance.env 缺 SSH_KEY}" "${SSH_KNOWN_HOSTS:?instance.env 缺 SSH_KNOWN_HOSTS}" \
  "${EXPECTED_EGRESS_IP:?instance.env 缺 EXPECTED_EGRESS_IP}" \
  "${EXPECTED_EGRESS_ASN:?instance.env 缺 EXPECTED_EGRESS_ASN}" \
  "${ROUTER_IP:?instance.env 缺 ROUTER_IP}" "${TIMEZONE:?instance.env 缺 TIMEZONE}"
MIHOMO_USER="${MIHOMO_USER:-mihomo}"
EXIT_MODE="${EXIT_MODE:-chain}"
if [ -z "${STRIPE_EXIT_MODE:-}" ]; then
  if [ "$EXIT_MODE" = "single" ]; then
    if [ -n "${STRIPE_NODE_SERVER:-}" ]; then
      STRIPE_EXIT_MODE="node"
    else
      STRIPE_EXIT_MODE="off"
    fi
  else
    STRIPE_EXIT_MODE="front"
  fi
fi

# NAT 端口转发（VirtualBox）时 GUEST_IP 仍是 127.0.0.1，宿主端口不是 22。
# instance.env 可写 SSH_PORT；不写则保持直连客机的 22。
SSH_PORT="${SSH_PORT:-22}"
case "$SSH_PORT" in
  ''|*[!0-9]*|0*)
    echo "instance.env 的 SSH_PORT 必须是 1-65535 的整数" >&2
    exit 1
    ;;
esac
if [ "${#SSH_PORT}" -gt 5 ] || [ "$SSH_PORT" -gt 65535 ]; then
  echo "instance.env 的 SSH_PORT 必须是 1-65535 的整数" >&2
  exit 1
fi

# cchouse.py 在 Windows 上把 System32\OpenSSH 加到 PATH 前面。Git Bash 启动时仍把
# /usr/bin 放在最前，/usr/bin/ssh 会盖住无扩展名 shim；C:\Program Files\OpenSSH\ssh.exe
# 还会忽略 ConnectTimeout。这里按绝对路径调用 System32 的 ssh.exe / scp.exe。
# VERIFY_OPENSSH_DIR 只在单测里使用，且仅当上面两个 System32 路径都不存在时才生效。
resolve_openssh() {
  local _name="$1" _hit
  for _hit in \
    "/c/Windows/System32/OpenSSH/${_name}.exe" \
    "/mnt/c/Windows/System32/OpenSSH/${_name}.exe"
  do
    if [ -x "$_hit" ]; then
      printf '%s\n' "$_hit"
      return 0
    fi
  done
  if [ -n "${VERIFY_OPENSSH_DIR:-}" ] && [ -x "${VERIFY_OPENSSH_DIR}/${_name}.exe" ]; then
    printf '%s\n' "${VERIFY_OPENSSH_DIR}/${_name}.exe"
    return 0
  fi
  printf '%s\n' "$_name"
}
SSH_BIN="$(resolve_openssh ssh)"
SCP_BIN="$(resolve_openssh scp)"

SSH_OPTS=(-F none -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=\"$SSH_KNOWN_HOSTS\"" -p "$SSH_PORT")
# scp 的端口是 -P；-p 在 scp 里表示保留时间戳，不能复用 SSH_OPTS。
SCP_OPTS=(-F none -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=\"$SSH_KNOWN_HOSTS\"" -P "$SSH_PORT")
guest() { "$SSH_BIN" "${SSH_OPTS[@]}" "claude@$GUEST_IP" "$@"; }
guest_scp() { "$SCP_BIN" "${SCP_OPTS[@]}" "$@"; }

# 单测探针：正常验收不设置。退出码 86，避免被当成验收通过。
if [ "${VERIFY_LINUX_TRANSPORT_PROBE:-}" = 1 ]; then
  "$SSH_BIN" "${SSH_OPTS[@]}" "claude@$GUEST_IP" true
  guest_scp -- "probe-local" "claude@${GUEST_IP}:probe-remote"
  exit 86
fi

TRACE=https://www.cloudflare.com/cdn-cgi/trace
pass=0; fail=0; warn_count=0; stripe_ok="unknown"
log() { [ "$JSON_OUT" = 1 ] || echo "$@"; }
ok()  { log "  PASS  $1"; pass=$((pass+1)); }
bad() { log "  FAIL  $1"; fail=$((fail+1)); }
warn() { log "  WARN  $1"; warn_count=$((warn_count+1)); }
expect() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1（期望 $3，实际 ${2:-无}）"; fi; }

expect_blocked() {
  local rc
  guest "$2 >/dev/null 2>&1; rc=\$?; case \$rc in 6|7|28) exit 42 ;; 0) exit 0 ;; *) exit 43 ;; esac" >/dev/null 2>&1
  rc=$?
  case $rc in
    42) ok "$1" ;;
    0) bad "$1（仍可连接）" ;;
    *) bad "$1（无法确认阻断，远程状态 ${rc}）" ;;
  esac
}

restore_needed=0
restore_mihomo() {
  [ "$restore_needed" = 1 ] || return 0
  guest "sudo -n /usr/local/sbin/cchouse-probe mihomo-start" >/dev/null 2>&1 || return 1
  restore_needed=0
}
trap 'cleanup_all' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

trace_field() { awk -F= -v k="$1" '$1==k{print $2}'; }
# Cloudflare trace 偶发超时/空响应：最多重试 2 次（间隔 3 秒），只在取不到 loc 时重试；
# 取到即交给 expect 严格比对（国家不符不重试）。三次都取不到时 CF_LOC_REASON 记录每次是超时、空响应还是 SSH 无返回
cf_trace_loc() {
  local i out rc loc reasons=""
  CF_LOC=""
  CF_LOC_REASON=""
  for i in 1 2 3; do
    out="$(guest "curl -s --max-time 15 $TRACE; echo __curl_rc=\$?" 2>/dev/null)"
    rc="$(printf '%s\n' "$out" | sed -n 's/^__curl_rc=//p' | tail -n 1)"
    loc="$(printf '%s\n' "$out" | trace_field loc)"
    if [ -n "$loc" ]; then
      CF_LOC="$loc"
      return 0
    fi
    case "$rc" in
      28) reasons="${reasons}第${i}次超时；" ;;
      0)  reasons="${reasons}第${i}次空响应；" ;;
      "") reasons="${reasons}第${i}次 SSH 无返回；" ;;
      *)  reasons="${reasons}第${i}次 curl 退出码 ${rc}；" ;;
    esac
    if [ "$i" -lt 3 ]; then sleep "${CF_TRACE_RETRY_SLEEP:-3}"; fi
  done
  CF_LOC_REASON="${reasons%；}"
  return 1
}
egress_claude() {
  local ip="" i
  for i in 1 2 3; do
    ip=$(guest "curl -s --max-time 15 $TRACE" 2>/dev/null | trace_field ip)
    [ -n "$ip" ] && break
  done
  printf '%s' "$ip"
}
egress_root() {
  local ip="" i
  for i in 1 2 3; do
    ip=$(guest "sudo -n /usr/local/sbin/cchouse-probe root-egress" 2>/dev/null)
    [ -n "$ip" ] && break
  done
  printf '%s' "$ip"
}

PY_CMD=""
if command -v py >/dev/null 2>&1; then
  PY_CMD="py -3 -I"
elif command -v python3 >/dev/null 2>&1 && [[ "$(command -v python3)" != *WindowsApps* ]]; then
  PY_CMD="python3 -I"
fi
json_field() {
  if [ -n "$PY_CMD" ]; then
    $PY_CMD -c 'import sys,json
try: print(json.load(sys.stdin).get(sys.argv[1],""))
except Exception: print("")' "$1"
  else
    sed -n 's/.*"'"$1"'"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1
  fi
}

log "== ${VM_NAME:-$TAG} ($GUEST_IP) 期望出口 $EXPECTED_EGRESS_IP"
if ! guest true; then
  bad "SSH 连不上客机"
  if [ "$JSON_OUT" = 1 ]; then
    printf '{"ok":false,"pass":0,"fail":1,"next_action":"check_ssh_and_guest_boot"}\n'
  fi
  exit 1
fi
img_rel=$(guest "tr '\n' ' ' </etc/cchouse-image-release 2>/dev/null || true")
[ -n "$img_rel" ] && log "  INFO  镜像来源：$img_rel"

log "-- 出口"
got_c="$(egress_claude) $(egress_claude) $(egress_claude)"
expect "claude 出口连测 3 次一致" "$got_c" "$EXPECTED_EGRESS_IP $EXPECTED_EGRESS_IP $EXPECTED_EGRESS_IP"
got_r="$(egress_root) $(egress_root) $(egress_root)"
expect "root 出口连测 3 次一致" "$got_r" "$EXPECTED_EGRESS_IP $EXPECTED_EGRESS_IP $EXPECTED_EGRESS_IP"
if [ -n "${EXPECTED_EGRESS_LOC:-}" ]; then
  if cf_trace_loc; then
    expect "Cloudflare 看到的国家" "$CF_LOC" "$EXPECTED_EGRESS_LOC"
  else
    bad "Cloudflare 看到的国家（期望 ${EXPECTED_EGRESS_LOC}，3 次均未取到：${CF_LOC_REASON}）"
  fi
fi

log "-- 环境与出口一致"
info=$(guest "curl -s --max-time 15 https://ipinfo.io/json" 2>/dev/null)
ip_tz=$(printf '%s' "$info" | json_field timezone)
expect "出口 IP 的运营商 ASN = EXPECTED_EGRESS_ASN" "$(printf '%s' "$info" | json_field org | awk '{print $1}')" "$EXPECTED_EGRESS_ASN"
expect "出口 IP 所在时区 = TIMEZONE" "$ip_tz" "$TIMEZONE"
tz=$(guest "readlink /etc/localtime" 2>/dev/null); tz=${tz##*/zoneinfo/}
expect "客机系统时区 = 出口时区" "$tz" "${ip_tz:-$TIMEZONE}"
if [ -n "${GUEST_LOCALE:-}" ]; then
  expect "客机默认语言 = GUEST_LOCALE" "$(guest "awk -F= '/^LANG=/{print \$2}' /etc/default/locale 2>/dev/null")" "$GUEST_LOCALE"
fi

log "-- Stripe 可达性"
stripe_http_code() {
  local code=""
  code=$(guest "curl -s --max-time 12 -o /dev/null -w '%{http_code}' '$1'" 2>/dev/null || true)
  printf '%s' "${code:-000}"
}
stripe_code_ok() {
  case "$1" in
    [234][0-9][0-9]) return 0 ;;
    *) return 1 ;;
  esac
}
stripe_js_code=$(stripe_http_code "https://js.stripe.com/v3/")
stripe_api_code=$(stripe_http_code "https://api.stripe.com/v1")
if stripe_code_ok "$stripe_js_code" && stripe_code_ok "$stripe_api_code"; then
  stripe_ok="true"
  ok "Stripe 可达（js.stripe.com=$stripe_js_code, api.stripe.com=${stripe_api_code}）"
else
  stripe_ok="false"
  # 只提示不判失败：Stripe 仅影响 claude.ai 网页付款，已订阅或别处付款的用户不受影响
  if [ "$EXIT_MODE" = "single" ] && [ "$STRIPE_EXIT_MODE" = "off" ]; then
    warn "Stripe 不可达（js.stripe.com=$stripe_js_code, api.stripe.com=${stripe_api_code}）：单节点出口可能屏蔽 Stripe，claude.ai 网页付款会失败（不影响已订阅账号使用）；如需在此付款，可配置 Stripe 备用节点（STRIPE_EXIT_MODE=node + STRIPE_NODE_*）或改链式（EXIT_MODE=chain）"
  else
    warn "Stripe 不可达（js.stripe.com=$stripe_js_code, api.stripe.com=${stripe_api_code}，当前 STRIPE_EXIT_MODE=${STRIPE_EXIT_MODE}）：claude.ai 网页付款会失败（不影响已订阅账号使用）"
  fi
fi

log "-- 不该通的"
expect "客机没有公网 IPv6 地址" "$(guest "ip -6 addr show scope global | grep -c inet6 || true" 2>/dev/null | tr -d ' ')" "0"
expect_blocked "直连 IPv6 地址不通" "curl -g -s --max-time 8 -o /dev/null 'https://[2606:4700:4700::1111]/'"
expect_blocked "访问不到路由器 $ROUTER_IP" "curl -s --max-time 6 -o /dev/null http://$ROUTER_IP"
real_if=$(guest "ip -4 route show default | awk 'NR==1{print \$5}'" 2>/dev/null)
if [ -n "$real_if" ] && [ "$real_if" != "cchouse0" ]; then
  expect_blocked "绕过 TUN 直接绑定物理网卡 $real_if 不通" "curl --interface $real_if -s --max-time 6 -o /dev/null https://1.1.1.1/cdn-cgi/trace"
else
  bad "无法识别用于物理直连阻断测试的物理网卡（实际：${real_if:-无}）"
fi
dns=$(guest "dig +short +time=5 +tries=1 example.com @8.8.8.8" 2>/dev/null | head -1)
expect "外部 DNS 被接管成 198.18 假 IP" "${dns%.*.*}" "198.18"

log "-- 规则红线与服务"
expect "nftables table inet cchouse_guard 已加载" "$(guest "sudo -n /usr/local/sbin/cchouse-probe nft-loaded" 2>/dev/null)" "ok"
expect "config.yaml 无 DIRECT 规则" "$(guest "sudo -n /usr/local/sbin/cchouse-probe direct-count" 2>/dev/null | tr -d ' ')" "0"
if guest "test -f /var/lib/cchouse/expected-nft-live.sha256" >/dev/null 2>&1; then
  exp_sha=$(guest "cat /var/lib/cchouse/expected-nft-live.sha256" 2>/dev/null)
  cur_sha=$(guest "sudo -n /usr/local/sbin/cchouse-probe nft-live-sha" 2>/dev/null)
  expect "内核生效 nftables 规则哈希与首次注入一致" "$cur_sha" "$exp_sha"
else
  bad "缺少 /var/lib/cchouse/expected-nft-live.sha256，无法核对内核 nftables 规则摘要"
fi
for svc in cchouse-default-drop cchouse-nftables cchouse-mihomo; do
  expect "$svc.service active" "$(guest "systemctl is-active $svc.service 2>/dev/null")" "active"
done
expect "mihomo 以专用用户 $MIHOMO_USER 运行" "$(guest "ps -eo user=,comm= | awk '\$2==\"mihomo\"{print \$1; exit}'")" "$MIHOMO_USER"
expect "SSH 禁止密码登录" "$(guest "awk '/^PasswordAuthentication/{print \$2}' /etc/ssh/sshd_config.d/100-$TAG.conf 2>/dev/null")" "no"
sudo_audit=$(guest "sudo -n -l 2>/dev/null | awk '
  /User .* may run/ { p=1; next }
  p && /^[[:space:]]*\(/ {
    gsub(/^[[:space:]]+|[[:space:]]+$/, \"\")
    n++
    if (\$0 ~ /\*/) { bad=1 }
    if (\$0 ~ /^\(root\) NOPASSWD: \/usr\/bin\/systemctl poweroff\$/) { poweroff=1 }
    else if (\$0 ~ /^\(root\) NOPASSWD: \/usr\/bin\/systemctl reboot\$/) { reboot=1 }
    else if (\$0 !~ /^\(root\) NOPASSWD: \/usr\/local\/sbin\/cchouse-probe (nft-loaded|nft-live-sha|direct-count|mihomo-stop|mihomo-start|root-egress)\$/) { bad=1 }
  }
  END { if (n >= 8 && poweroff && reboot && !bad) print \"ok\"; else print \"bad(\" n \",power=\" (poweroff+reboot) \")\" }
'" 2>/dev/null || echo "err")
expect "sudoers 仅放行固定无通配符白名单（cchouse-verify/cchouse-power）" "$sudo_audit" "ok"
expect "Claude Code CLI 可执行（claude --version）" "$(guest "claude --version >/dev/null 2>&1 && echo ok || echo missing")" "ok"
expect "Claude Code 自动更新已禁用（profile.d + managed-settings.json）" "$(guest "grep -q 'DISABLE_AUTOUPDATER=1' /etc/profile.d/cchouse-claude.sh 2>/dev/null && grep -q 'DISABLE_AUTOUPDATER' /etc/claude-code/managed-settings.json 2>/dev/null && echo ok || echo missing")" "ok"

if [ "$FAIL_CLOSED" = 1 ]; then
  log "-- 断网测试（停 cchouse-mihomo）"
  restore_needed=1
  if ! guest "sudo -n /usr/local/sbin/cchouse-probe mihomo-stop" >/dev/null 2>&1; then
    bad "无法停止 cchouse-mihomo.service"
  else
    sleep 2
    expect_blocked "停 mihomo 后直连公网 IP 不通" "curl -s --max-time 6 -o /dev/null https://1.1.1.1/cdn-cgi/trace"
    expect_blocked "停 mihomo 后域名不通" "curl -s --max-time 6 -o /dev/null $TRACE"
    restore_mihomo || bad "恢复 cchouse-mihomo.service 失败"
    sleep 3
    expect "恢复后回到同一出口" "$(egress_claude)" "$EXPECTED_EGRESS_IP"
  fi
fi

log "== 结果：$pass PASS / $fail FAIL / $warn_count WARN"
if [ "$JSON_OUT" = 1 ]; then
  ok_bool="false"; next_act="fix_failed_checks"
  if [ "$fail" = 0 ]; then ok_bool="true"; next_act="human_check_html_and_login"; fi
  stripe_json="null"
  if [ "$stripe_ok" = "true" ] || [ "$stripe_ok" = "false" ]; then stripe_json="$stripe_ok"; fi
  printf '{"ok":%s,"pass":%d,"fail":%d,"warn":%d,"stripe_reachable":%s,"next_action":"%s"}\n' \
    "$ok_bool" "$pass" "$fail" "$warn_count" "$stripe_json" "$next_act"
fi
[ "$fail" = 0 ]
