#!/bin/bash
# 定时出口巡检：客机在运行时，确认出口还是 EXPECTED_EGRESS_IP；不对或连不上就弹系统通知并记日志。
# 用法：host/watch-egress.sh <instance.env>   由 ~/Library/LaunchAgents/com.cchouse.<TAG>.watch.plist 每小时运行
# 只读，不改客机任何东西。Webshare 换了 IP、机场/前置失效、代理到期，都会在这里第一时间暴露。
set -u
cd "$(dirname "$0")/../scripts" && . ./lib.sh
load_env "${1:?用法：watch-egress.sh <instance.env>}"
: "${EXPECTED_EGRESS_IP:?}" "${GUEST_IP:?}"
ssh_opts
LOG="$HOME/Library/Logs/cchouse-$TAG-watch.log"
say() { echo "$(date '+%F %T') $1" >> "$LOG"; }
alert() {
  say "ALERT $1"
  /usr/bin/osascript -e "display notification \"$1\" with title \"cchouse $TAG\" sound name \"Basso\"" 2>/dev/null
}

# 客机没在运行就不查（关机是人为的，不算故障）
"$TART_BIN" list 2>/dev/null | grep -q "^local *$VM_NAME .*running" || { say "skip: $VM_NAME 未运行"; exit 0; }

ip=""
for i in 1 2 3; do
  ip=$(guest "/usr/bin/curl -s --max-time 15 https://www.cloudflare.com/cdn-cgi/trace" 2>/dev/null | awk -F= '$1=="ip"{print $2}')
  [ -n "$ip" ] && break
  sleep 10
done

if [ -z "$ip" ]; then
  if guest true 2>/dev/null; then alert "客机断网：取不到出口（前置、代理或订阅可能失效）"
  else alert "SSH 连不上客机 $GUEST_IP"; fi
  exit 1
elif [ "$ip" != "$EXPECTED_EGRESS_IP" ]; then
  alert "出口变了：实际 ${ip}，期望 ${EXPECTED_EGRESS_IP}。先别登录账号"
  exit 1
fi
say "ok"
