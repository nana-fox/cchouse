#!/bin/bash
# 改网络前在后台运行（sudo）：先备份当前 config.yaml / pf.conf，120 秒内没人 touch /tmp/<TAG>/ok 就恢复备份并重载。
# 不关 pf、不停 mihomo：回滚的结果只能是「回到改动前的配置」，不能变成家宽直连。
# 首次安装时还没有旧配置，没有可恢复的，就什么都不做（pf 本来就放行局域网 SSH，不会把自己锁在外面）。
set -euo pipefail
D=/usr/local/<TAG>; B=$D/backup; S=/tmp/<TAG>
mkdir -p "$D"
mkdir -p "$S"; chmod 700 "$S"
mkdir "$D/guard.lock" || { echo "已有 guard 或残留锁，请先核实" >&2; exit 1; }
trap 'rm -f "$S/ready"; rmdir "$D/guard.lock"' EXIT
rm -f "$S/ok" "$S/ready" "$S/rolled-back"
rm -rf "$B"; mkdir -m 700 "$B"
if [ -f "$D/pf.conf" ] && [ -f "$D/config.yaml" ]; then
  cp -p "$D/config.yaml" "$D/pf.conf" "$B/"
elif [ -e "$D/pf.conf" ] || [ -e "$D/config.yaml" ]; then
  echo "旧配置不完整，停止；没有可用的成对备份" >&2
  exit 1
fi
# 调用方必须等 ready 出现后才覆盖线上文件，避免后台备份与 install 竞争。
touch "$S/ready"
sleep 120; [ -f /tmp/<TAG>/ok ] && exit 0
if [ -f $B/pf.conf ]; then
  cp -p $B/config.yaml $B/pf.conf $D/
  pfctl -f $D/pf.conf 2>/dev/null
  launchctl kickstart -k system/net.<TAG>.mihomo
fi
echo rolled-back > /tmp/<TAG>/rolled-back
