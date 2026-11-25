#!/bin/bash
# 每次图形登录时运行（/Library/LaunchAgents/net.<TAG>.check.plist）：
# 用 Safari 打开出口检查页，全部绿色才提示可以登录。
sleep 3
/usr/bin/open -a Safari /usr/local/<TAG>/check.html
