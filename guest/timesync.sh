#!/bin/bash
# NTP(UDP) is blocked; set clock from HTTPS Date header via the proxy (TUN)
d=$(/usr/bin/curl -sI --max-time 15 https://www.cloudflare.com/cdn-cgi/trace | awk -F': ' 'tolower($1)=="date"{print $2}' | tr -d '\r')
[ -n "$d" ] || exit 0
t=$(/bin/date -j -u -f "%a, %d %b %Y %T GMT" "$d" "+%m%d%H%M%y.%S") && /bin/date -u "$t" >/dev/null
