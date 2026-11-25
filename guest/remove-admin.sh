#!/bin/bash
# run as claude via sudo AFTER claude key login is verified:
#   sudo bash remove-admin.sh '<claude-password>' ['<admin-password>']
# claude 由 root 用 sysadminctl 建出，没有 Secure Token；admin 是唯一持有者时删不掉，
# 所以先用 admin 的身份给 claude 授予 Secure Token，再删 admin。原版镜像 admin 密码是 admin。
set -e; [ "$(id -un)" = root ]; id claude >/dev/null
PW="$1"; APW="${2:-admin}"
sysadminctl -secureTokenOn claude -password "$PW" -adminUser admin -adminPassword "$APW" 2>&1 | tail -1
sysadminctl -secureTokenStatus claude 2>&1 | tail -1 | grep -q ENABLED
sysadminctl -deleteUser admin -secure 2>&1 | tail -1; ! id admin 2>/dev/null
rm -f /etc/sudoers.d/admin-nopasswd   # 原版镜像自带的 admin 免密 sudo
echo remove-admin-done
