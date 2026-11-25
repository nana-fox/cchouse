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

SSH_OPTS=(-F none -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=\"$SSH_KNOWN_HOSTS\"")
guest() { ssh "${SSH_OPTS[@]}" "claude@$GUEST_IP" "$@"; }

TRACE=https://www.cloudflare.com/cdn-cgi/trace
pass=0; fail=0
log() { [ "$JSON_OUT" = 1 ] || echo "$@"; }
ok()  { log "  PASS  $1"; pass=$((pass+1)); }
bad() { log "  FAIL  $1"; fail=$((fail+1)); }
expect() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1（期望 $3，实际 ${2:-无}）"; fi; }

expect_blocked() {
  local rc
  guest "$2 >/dev/null 2>&1; rc=\$?; case \$rc in 6|7|28) exit 42 ;; 0) exit 0 ;; *) exit 43 ;; esac" >/dev/null 2>&1
  rc=$?
  case $rc in
    42) ok "$1" ;;
    0) bad "$1（仍可连接）" ;;
    *) bad "$1（无法确认阻断，远程状态 $rc）" ;;
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
  expect "Cloudflare 看到的国家" "$(guest "curl -s --max-time 15 $TRACE" 2>/dev/null | trace_field loc)" "$EXPECTED_EGRESS_LOC"
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

log "== 结果：$pass PASS / $fail FAIL"
if [ "$JSON_OUT" = 1 ]; then
  ok_bool="false"; next_act="fix_failed_checks"
  if [ "$fail" = 0 ]; then ok_bool="true"; next_act="human_check_html_and_login"; fi
  printf '{"ok":%s,"pass":%d,"fail":%d,"next_action":"%s"}\n' "$ok_bool" "$pass" "$fail" "$next_act"
fi
[ "$fail" = 0 ]
