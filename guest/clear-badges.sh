#!/bin/bash
# 清掉系统设置上的提醒角标（软件更新可用、完成 Apple 账户设置等），免得用户点进去
# 下载系统更新（几个 GB，走固定出口很慢且吃流量）或登录 Apple ID。
# macOS 26 上命令行关不掉自动检查更新（写入的 AutomaticCheckEnabled 会被系统删掉），只能清提醒。
# 由 /Library/LaunchAgents/net.<TAG>.nobadge.plist 在登录时和之后每 5 分钟运行。
DB="$HOME/Library/CoreFollowUp/items.db"
n=0
[ -f "$DB" ] && n=$(sqlite3 "$DB" "select count(*) from items" 2>/dev/null || echo 0)
if [ "${n:-0}" -gt 0 ]; then
  sqlite3 "$DB" "delete from items" 2>/dev/null
  killall followupd 2>/dev/null
fi
if defaults read com.apple.systempreferences AttentionPrefBundleIDs >/dev/null 2>&1; then
  defaults delete com.apple.systempreferences AttentionPrefBundleIDs
  killall Dock 2>/dev/null
fi
exit 0
