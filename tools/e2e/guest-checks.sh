#!/bin/bash
# Guest-side extended E2E checks + desktop screenshot capture.
# Executed on the Debian 13 guest over SSH as user `claude`.
# Usage: bash guest-checks.sh <TAG> <STRIPE_EXIT_MODE>
set -uo pipefail

TAG="${1:?missing TAG}"
STRIPE_MODE="${2:-front}"
FAIL=0

pass() { echo "PASS $1"; }
fail() { echo "FAIL $1"; FAIL=$((FAIL + 1)); }

# 1. Image source commit (/etc/cchouse-image-release)
if [ -f /etc/cchouse-image-release ]; then
  REL="$(tr '\n' ' ' < /etc/cchouse-image-release | sed 's/  */ /g; s/ *$//')"
  echo "META image_release=${REL}"
  pass "image_release (${REL})"
else
  echo "META image_release=not-present"
  pass "image_release (not present in image)"
fi

# 2. Claude Code version
CLAUDE_VER="$(claude --version 2>&1 | head -n 1 || true)"
if [ -n "$CLAUDE_VER" ]; then
  echo "META claude_version=${CLAUDE_VER}"
  pass "claude_version (${CLAUDE_VER})"
else
  fail "claude_version (empty)"
fi

# 3. Real exit IP / country / ASN (for report metadata; retry trace and fall back to ipinfo.io)
EGRESS_IP=""
EGRESS_LOC=""
for _ in 1 2 3; do
  TRACE="$(curl -s --max-time 15 https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null || true)"
  EGRESS_IP="$(printf '%s\n' "$TRACE" | awk -F= '$1=="ip"{print $2}' | tr -d '\r')"
  EGRESS_LOC="$(printf '%s\n' "$TRACE" | awk -F= '$1=="loc"{print $2}' | tr -d '\r')"
  [ -n "$EGRESS_IP" ] && break
  sleep 1
done
IPINFO="$(curl -s --max-time 15 https://ipinfo.io/json 2>/dev/null || true)"
if [ -z "$EGRESS_IP" ]; then
  EGRESS_IP="$(printf '%s' "$IPINFO" | sed -n 's/.*"ip"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)"
fi
if [ -z "$EGRESS_LOC" ]; then
  EGRESS_LOC="$(printf '%s' "$IPINFO" | sed -n 's/.*"country"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)"
fi
EGRESS_ASN="$(printf '%s' "$IPINFO" | sed -n 's/.*"org"[[:space:]]*:[[:space:]]*"\([^" ]*\).*/\1/p' | head -n 1)"
echo "META egress_ip=${EGRESS_IP:-unknown}"
echo "META egress_loc=${EGRESS_LOC:-unknown}"
echo "META egress_asn=${EGRESS_ASN:-unknown}"

# 4. Stripe split routing reachability (when STRIPE_EXIT_MODE != off)
if [ "$STRIPE_MODE" != "off" ]; then
  STRIPE_CODE="$(curl -sSI --max-time 15 -o /dev/null -w '%{http_code}' https://js.stripe.com/v3/ 2>/dev/null || true)"
  STRIPE_CODE="${STRIPE_CODE:-000}"
  if [ "$STRIPE_CODE" = "200" ]; then
    pass "stripe_split_routing (https://js.stripe.com/v3/ HTTP 200)"
  else
    fail "stripe_split_routing (https://js.stripe.com/v3/ HTTP ${STRIPE_CODE})"
  fi
else
  pass "stripe_split_routing (mode=off, skipped)"
fi

# 5. Chinese desktop processes + CJK fonts
CJK_CNT="$(fc-list :lang=zh 2>/dev/null | wc -l | tr -d ' ')"
if [ "${CJK_CNT:-0}" -gt 0 ]; then
  pass "cjk_fonts (${CJK_CNT} fonts)"
else
  fail "cjk_fonts (0 Chinese fonts)"
fi

# Wait briefly for desktop session to settle after boot
for _ in 1 2 3 4 5; do
  if pgrep -x openbox >/dev/null 2>&1 && pgrep -x tint2 >/dev/null 2>&1 && pgrep -x fcitx5 >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

for proc in lightdm openbox tint2 fcitx5; do
  if pgrep -x "$proc" >/dev/null 2>&1 || pgrep -f "$proc" >/dev/null 2>&1; then
    pass "desktop_proc_${proc}"
  else
    fail "desktop_proc_${proc} (not running)"
  fi
done

# Discover X11 authority for DISPLAY=:0
XAUTH_FILE="/home/claude/.Xauthority"
if [ ! -r "$XAUTH_FILE" ]; then
  XAUTH_FILE="$(find /run/user /home/claude /tmp -maxdepth 3 -name '*Xauthority*' -readable 2>/dev/null | head -n 1 || true)"
fi

# Launch Chromium with check.html on DISPLAY=:0 if not already running
if ! pgrep -f chromium >/dev/null 2>&1; then
  CHECK_URL="file:///usr/local/${TAG}/check.html"
  DISPLAY=:0 XAUTHORITY="${XAUTH_FILE}" setsid -f chromium --no-first-run --disable-default-apps --window-size=1200,820 "$CHECK_URL" </dev/null >/tmp/cchouse-chromium.log 2>&1
  for _ in 1 2 3 4 5 6 7 8; do
    sleep 1
    if pgrep -f chromium >/dev/null 2>&1; then
      break
    fi
  done
  # Let check.html finish async IP/DNS checks before screenshot
  sleep 5
fi

if pgrep -f chromium >/dev/null 2>&1; then
  pass "desktop_proc_chromium"
else
  fail "desktop_proc_chromium (failed to start)"
fi

# 6. Capture full desktop screenshot via xwd on DISPLAY=:0
rm -f /tmp/cchouse-desktop.xwd.gz
if command -v xwd >/dev/null 2>&1; then
  if DISPLAY=:0 XAUTHORITY="${XAUTH_FILE}" xwd -root -silent -display :0 2>/tmp/cchouse-xwd.err | gzip -c > /tmp/cchouse-desktop.xwd.gz; then
    SZ="$(stat -c%s /tmp/cchouse-desktop.xwd.gz 2>/dev/null || echo 0)"
    if [ "$SZ" -gt 1000 ]; then
      pass "desktop_screenshot_xwd (${SZ} bytes gz)"
    else
      fail "desktop_screenshot_xwd (too small: ${SZ} bytes; $(cat /tmp/cchouse-xwd.err 2>/dev/null))"
    fi
  else
    fail "desktop_screenshot_xwd (xwd error: $(cat /tmp/cchouse-xwd.err 2>/dev/null))"
  fi
else
  fail "desktop_screenshot_xwd (xwd command not found)"
fi

exit "$FAIL"
