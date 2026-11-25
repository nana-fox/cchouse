#!/bin/bash
# 实时记录客机每个域名走哪条线路（读 mihomo 的 /connections），同时收 mihomo 警告日志。
# 用法：host/monitor.sh <instance.env>   Ctrl-C 结束
# 结果写在 <PRIVATE_DIR>/monitor/<时间>/，里面有访问过的域名，属于隐私，不进仓库。
set -u
cd "$(dirname "$0")/../scripts" && . ./lib.sh
load_env "${1:?用法：monitor.sh <instance.env>}"
ssh_opts
OUT="${PRIVATE_DIR:?}/monitor/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUT"; echo "$OUT"
guest "sudo -n tail -n0 -F /var/log/$TAG-mihomo.log" > "$OUT/mihomo-warn.log" 2>&1 &
trap 'kill %1 2>/dev/null' EXIT
guest 'while true; do curl -s 127.0.0.1:9090/connections; echo; sleep 2; done' 2>/dev/null | \
python3 -I -u -c '
import sys,json,time
seen={}
for line in sys.stdin:
    try: d=json.loads(line)
    except Exception: continue
    for c in d.get("connections") or []:
        m=c["metadata"]; h=m.get("host") or m.get("destinationIP")
        ch=" > ".join(reversed(c["chains"]))
        if seen.get(h)!=ch:
            seen[h]=ch
            print(time.strftime("%H:%M:%S"), f"{h:40}", c["rule"]+":"+c["rulePayload"], "|", ch, flush=True)
' | tee "$OUT/connections.log"
