#!/bin/bash
# run as root inside guest: sudo bash setup-root.sh <claude-password>
set -euo pipefail
PW="$1"; S=/tmp/<TAG>
[ ! -d /usr/local/<TAG>/guard.lock ] || { echo "已有 guard，请先核实" >&2; exit 1; }
rm -f "$S/ready"
nohup bash $S/guard.sh >/dev/null 2>&1 &
GUARD_PID=$!
for i in $(seq 1 100); do
  kill -0 "$GUARD_PID" 2>/dev/null || { echo "guard 未能启动，停止安装" >&2; exit 1; }
  [ -f "$S/ready" ] && break
  sleep 0.1
done
[ -f "$S/ready" ] || { echo "guard 备份未就绪，停止安装" >&2; exit 1; }
# 1. network lockdown first
mkdir -p /usr/local/<TAG>
install -o root -g wheel -m 600 $S/config.yaml $S/pf.conf /usr/local/<TAG>/
install -o root -g wheel -m 700 $S/mihomo $S/timesync.sh $S/guard.sh /usr/local/<TAG>/
install -o root -g wheel -m 644 $S/net.<TAG>.pf.plist $S/net.<TAG>.mihomo.plist $S/net.<TAG>.timesync.plist /Library/LaunchDaemons/
pfctl -nf /usr/local/<TAG>/pf.conf
launchctl bootstrap system /Library/LaunchDaemons/net.<TAG>.pf.plist || launchctl kickstart -k system/net.<TAG>.pf
launchctl bootstrap system /Library/LaunchDaemons/net.<TAG>.mihomo.plist || launchctl kickstart -k system/net.<TAG>.mihomo
networksetup -listallnetworkservices | tail -n +2 | sed 's/^\*//' | while read -r s; do
  networksetup -setv6off "$s" || true; networksetup -setdnsservers "$s" 1.1.1.1 || true; done
launchctl bootstrap system /Library/LaunchDaemons/net.<TAG>.timesync.plist || true
# 2. region / time
systemsetup -settimezone <TIMEZONE> >/dev/null
systemsetup -setusingnetworktime off >/dev/null 2>&1 || true
defaults write /Library/Preferences/com.apple.timezone.auto Active -bool false   # 不让系统按 Wi-Fi/定位自动改时区
/usr/sbin/languagesetup -langspec en >/dev/null 2>&1 || true
defaults write /Library/Preferences/.GlobalPreferences AppleLanguages -array en-US
defaults write /Library/Preferences/.GlobalPreferences AppleLocale en_US
scutil --set ComputerName "MacBook Pro"; scutil --set LocalHostName "MacBook-Pro"; scutil --set HostName "MacBook-Pro.local"
# 3. power / updates / sharing noise
pmset -a sleep 0 displaysleep 0 disksleep 0
defaults write /Library/Preferences/com.apple.SoftwareUpdate AutomaticDownload -bool false
defaults write /Library/Preferences/com.apple.SoftwareUpdate AutomaticallyInstallMacOSUpdates -bool false
defaults write /Library/Preferences/com.apple.mDNSResponder.plist NoMulticastAdvertisements -bool true
defaults write /Library/Application\ Support/CrashReporter/DiagnosticMessagesHistory.plist AutoSubmit -bool false
# NAT 阶段系统可能已经查到过更新并缓存，重启后会据此反复推「有可用更新」；更新服务器之后被 mihomo 拒绝，清掉缓存就不会再出现
for k in RecommendedUpdates LastRecommendedUpdatesAvailable LastUpdatesAvailable LastRecommendedMajorOSBundleIdentifier; do
  defaults delete /Library/Preferences/com.apple.SoftwareUpdate $k 2>/dev/null || true; done
# /Library/Updates/ProductMetadata.plist 里的缓存受 SIP 保护删不掉，开机仍会推一条；由 net.<TAG>.nobadge 每 5 分钟清掉
killall softwareupdated 2>/dev/null || true
# 3b. 浏览器出口检查页：登录后自动用 Safari 打开，全部绿色才提示可以登录
install -o root -g wheel -m 644 $S/check.html $S/at-login.sh $S/clear-badges.sh /usr/local/<TAG>/
install -o root -g wheel -m 644 $S/net.<TAG>.check.plist $S/net.<TAG>.nobadge.plist /Library/LaunchAgents/
# 4. user claude (admin, auto-login, no lock)
if ! id claude >/dev/null 2>&1; then sysadminctl -addUser claude -fullName "Claude" -password "$PW" -admin; fi
sysadminctl -autologin set -userName claude -password "$PW" || true
# 无图形会话时上面会报 SACSetAutoLoginPassword error:22，镜像里旧的 kcpassword（admin 的）还在；
# 用宿主上 scripts/make-kcpassword.py 生成的文件覆盖，否则开机停在密码界面
if [ -f $S/kcpassword ]; then
  install -o root -g wheel -m 600 $S/kcpassword /etc/kcpassword
  defaults write /Library/Preferences/com.apple.loginwindow autoLoginUser claude
fi
sysadminctl -screenLock off -password "$PW" >/dev/null 2>&1 || true
install -d -o claude -g staff -m 700 /Users/claude/.ssh
install -o claude -g staff -m 600 $S/authorized_keys /Users/claude/.ssh/authorized_keys
echo "claude ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/claude; chmod 440 /etc/sudoers.d/claude; visudo -cf /etc/sudoers.d/claude
printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no\nAllowUsers claude\n' > /etc/ssh/sshd_config.d/100-<TAG>.conf
echo setup-root-done
