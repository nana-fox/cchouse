#!/bin/bash
# 经代理读取 Cloudflare HTTPS Date 对时（NTP UDP 已被封）
set -euo pipefail
d=$(curl -sI --max-time 15 https://www.cloudflare.com/ | awk -F': ' 'tolower($1)=="date"{gsub("\r",""); print $2; exit}')
[ -n "$d" ] || exit 1
date -u -s "$d" >/dev/null
