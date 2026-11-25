#!/bin/bash
# 自动验收：从宿主机 SSH 进客机逐项检查，全部通过退出码 0。
# 用法：scripts/verify.sh <instance.env> [--fail-closed]
#   --fail-closed  额外做断网测试：临时停掉 mihomo，确认公网全断，再恢复并确认回到同一出口。
#                  会让客机断网几十秒，客机里有正在跑的任务时别加。
# 只覆盖能自动查的项；浏览器指纹、剪贴板、冷启动顺序见 docs/verify.md 的手工项。
# 兼容 macOS 自带的 bash 3.2。
set -uo pipefail
cd "$(dirname "$0")" && . ./lib.sh
load_env "${1:?用法：verify.sh <instance.env> [--fail-closed]}"
FAIL_CLOSED=0; [ "${2:-}" = "--fail-closed" ] && FAIL_CLOSED=1
: "${GUEST_IP:?}" "${EXPECTED_EGRESS_IP:?instance.env 缺 EXPECTED_EGRESS_IP}" "${EXPECTED_EGRESS_ASN:?instance.env 缺 EXPECTED_EGRESS_ASN}" "${ROUTER_IP:?}" "${TIMEZONE:?}"
ssh_opts
TRACE=https://www.cloudflare.com/cdn-cgi/trace
pass=0; fail=0
ok()  { echo "  PASS  $1"; pass=$((pass+1)); }
bad() { echo "  FAIL  $1"; fail=$((fail+1)); }
expect() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1（期望 $3，实际 ${2:-无}）"; fi; }
# 在客机内把 curl 网络失败编码为固定状态；SSH、TLS 和命令失败不能算阻断。
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
  if guest "sudo -n launchctl print system/net.$TAG.mihomo" >/dev/null 2>&1; then
    guest "sudo -n launchctl kickstart -k system/net.$TAG.mihomo" >/dev/null 2>&1 || return 1
  else
    guest "sudo -n launchctl bootstrap system /Library/LaunchDaemons/net.$TAG.mihomo.plist" >/dev/null 2>&1 || return 1
  fi
  restore_needed=0
}
trap 'if ! restore_mihomo; then echo "无法自动恢复 mihomo，请通过客机桌面恢复" >&2; fi' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

trace_field() { awk -F= -v k="$1" '$1==k{print $2}'; }
# 单次取不到（Webshare 链路偶尔超时）重试两次；取到就是取到，不重试换值
egress() {
  local ip="" i
  for i in 1 2 3; do
    ip=$(guest "$1 /usr/bin/curl -s --max-time 15 $TRACE" 2>/dev/null | trace_field ip)
    [ -n "$ip" ] && break
  done
  printf '%s' "$ip"
}
json_field() { python3 -I -c 'import sys,json
try: print(json.load(sys.stdin).get(sys.argv[1],""))
except Exception: print("")' "$1"; }

echo "== $VM_NAME ($GUEST_IP) 期望出口 $EXPECTED_EGRESS_IP"
guest true || { echo "SSH 连不上客机，停止"; exit 1; }

echo "-- 出口"
for who in "" "sudo -n"; do
  label=claude; [ -n "$who" ] && label=root
  got="$(egress "$who") $(egress "$who") $(egress "$who")"
  expect "$label 出口连测 3 次一致" "$got" "$EXPECTED_EGRESS_IP $EXPECTED_EGRESS_IP $EXPECTED_EGRESS_IP"
done
if [ -n "${EXPECTED_EGRESS_LOC:-}" ]; then
  expect "Cloudflare 看到的国家" "$(guest "/usr/bin/curl -s --max-time 15 $TRACE" 2>/dev/null | trace_field loc)" "$EXPECTED_EGRESS_LOC"
fi

echo "-- 环境与出口一致"
info=$(guest "/usr/bin/curl -s --max-time 15 https://ipinfo.io/json" 2>/dev/null)
ip_tz=$(printf '%s' "$info" | json_field timezone)
echo "  INFO  出口归属：$(printf '%s' "$info" | json_field city), $(printf '%s' "$info" | json_field region), $(printf '%s' "$info" | json_field country) / $(printf '%s' "$info" | json_field org)"
expect "出口 IP 的运营商 ASN = instance.env 的 EXPECTED_EGRESS_ASN" "$(printf '%s' "$info" | json_field org | awk '{print $1}')" "$EXPECTED_EGRESS_ASN"
expect "出口 IP 所在时区 = instance.env 的 TIMEZONE" "$ip_tz" "$TIMEZONE"
tz=$(guest "readlink /etc/localtime" 2>/dev/null); tz=${tz##*/zoneinfo/}
expect "客机系统时区 = 出口时区" "$tz" "${ip_tz:-$TIMEZONE}"
expect "客机自动设置时区已关" "$(guest "sudo -n defaults read /Library/Preferences/com.apple.timezone.auto Active 2>/dev/null || echo 0")" "0"
expect "客机语言" "$(guest "defaults read -g AppleLanguages 2>/dev/null | tr -d ' \n()\"'")" "en-US"
expect "客机地区" "$(guest "defaults read -g AppleLocale 2>/dev/null")" "en_US"

echo "-- 不该通的"
# 注意：curl -6 访问域名会连到 ::ffff:198.18.x.x（IPv4 假 IP 的映射写法），仍走 IPv4 出口，不算泄漏；
# 所以直接连真实 IPv6 地址来测。
expect "客机没有公网 IPv6 地址" "$(guest "ifconfig | awk '/inet6/ && \$2 !~ /^fe80/ && \$2 != \"::1\"' | wc -l | tr -d ' '")" "0"
expect_blocked "直连 IPv6 地址不通" "/usr/bin/curl -g -s --max-time 8 -o /dev/null 'https://[2606:4700:4700::1111]/'"
expect_blocked "访问不到路由器 $ROUTER_IP" "/usr/bin/curl -s --max-time 6 -o /dev/null http://$ROUTER_IP"
dns=$(guest "/usr/bin/dig +short +time=5 +tries=1 example.com @8.8.8.8" 2>/dev/null | head -1)
expect "外部 DNS 被接管成 198.18 假 IP" "${dns%.*.*}" "198.18"

echo "-- 服务与账户"
expect "pf 已启用" "$(guest "sudo -n /sbin/pfctl -s info 2>/dev/null | awk '/^Status:/{print \$2}'")" "Enabled"
for svc in mihomo pf timesync; do
  if guest "sudo -n launchctl print system/net.$TAG.$svc" >/dev/null 2>&1; then ok "net.$TAG.$svc 已加载"; else bad "net.$TAG.$svc 未加载"; fi
done
expect "mihomo 在运行" "$(guest "sudo -n launchctl print system/net.$TAG.mihomo 2>/dev/null | awk '/^\tstate =/{print \$3}'")" "running"
expect "SSH 禁止密码登录" "$(guest "awk '/^PasswordAuthentication/{print \$2}' /etc/ssh/sshd_config.d/100-$TAG.conf 2>/dev/null")" "no"
expect "只有 claude 一个普通用户" "$(guest "dscl . -list /Users UniqueID" 2>/dev/null | awk '$2>=501{print $1}' | xargs)" "claude"
expect "tart-guest-agent 在图形用户会话运行" "$(guest "launchctl print gui/\$(id -u)/org.cirruslabs.tart-guest-agent 2>/dev/null | awk '/^\tstate =/{print \$3}'")" "running"
expect "tart exec 使用普通用户" "$("$TART_BIN" exec "$VM_NAME" id -un 2>/dev/null)" "claude"
if guest "command -v claude >/dev/null 2>&1"; then
  expect "Claude Code CLI 可执行（claude --version）" "$(guest "claude --version >/dev/null 2>&1 && echo ok || echo bad")" "ok"
fi

if [ $FAIL_CLOSED = 1 ]; then
  # 与实际网络调整共用 guard；只在即将停代理时开始 120 秒倒计时。
  guard_path="/usr/local/$TAG/guard.sh"
  guest "sudo -n test -f $guard_path" || guard_path="/tmp/$TAG/guard.sh"
  if ! guest "sudo -n test ! -d /usr/local/$TAG/guard.lock && sudo -n test -f $guard_path && sudo -n rm -f /tmp/$TAG/ready"; then
    bad "guard 不可用或已有任务运行，取消断网测试"
    exit 1
  fi
  if ! guest "sudo -n /bin/bash -c 'mkdir -p /tmp/$TAG; nohup /bin/bash $guard_path >/tmp/$TAG/verify-guard.log 2>&1 </dev/null &'" ||
     ! guest "for i in \$(seq 1 100); do sudo -n test -f /tmp/$TAG/ready && exit 0; sleep 0.1; done; exit 1"; then
    bad "guard 未就绪，取消断网测试"
    exit 1
  fi
  echo "-- 断网测试（停 mihomo）"
  restore_needed=1
  if ! guest "sudo -n launchctl bootout system/net.$TAG.mihomo" >/dev/null 2>&1; then
    bad "未确认 mihomo 已停止，取消断网测试"
    exit 1
  fi
  sleep 3
  expect_blocked "停掉 mihomo 后公网不通" "/usr/bin/curl -s --max-time 8 $TRACE"
  expect_blocked "停掉 mihomo 后直连 IP 也不通" "/usr/bin/curl -s --max-time 8 -o /dev/null http://1.1.1.1"
  if ! restore_mihomo; then
    bad "恢复 mihomo 失败"
    exit 1
  fi
  back=""
  for i in 1 2 3 4 5 6 7 8 9 10 11 12; do sleep 5; back=$(egress ""); [ -n "$back" ] && break; done
  expect "恢复 mihomo 后回到期望出口" "$back" "$EXPECTED_EGRESS_IP"
  if [ "$back" = "$EXPECTED_EGRESS_IP" ]; then
    guest "sudo -n touch /tmp/$TAG/ok" || { bad "无法确认 guard"; exit 1; }
  fi
fi

echo "== 通过 ${pass}，失败 ${fail}"
[ $fail = 0 ]
