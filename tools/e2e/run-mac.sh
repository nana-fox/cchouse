#!/bin/bash
# Mac (Apple Silicon + Tart) E2E runner.
# Called by tools/e2e/run.sh <run_dir>
set -uo pipefail

RUN_DIR="${1:?missing run_dir}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="${E2E_REPO_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
MODE="${E2E_MODE:-fast}"
KEEP_VM="${E2E_KEEP_VM:-0}"
CACHE_DIR="${E2E_CACHE_DIR:-$HOME/Library/Application Support/cchouse/e2e/cache}"
GOLDEN_VM="${MAC_GOLDEN_VM:-cchouse-debian13-arm64}"
ENV_SRC="${MAC_ENV_FILE:?MAC_ENV_FILE not set in e2e.conf}"
TVM_PATH="${E2E_TVM_PATH:-}"

COMMIT="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
RUN_ID="e2e-mac-$(date +%H%M%S)-$$"
VM_NAME="cchouse-${RUN_ID}"
IMPORTED_BASE=""
TMP_DIR="$(mktemp -d)"
chmod 700 "$TMP_DIR"
FAILED=0
FAIL_STEP=""
FAIL_REASON=""

report() { python3 "$SCRIPT_DIR/report.py" "$@"; }
# shellcheck source=scripts/vm-tart.sh
. "$REPO_ROOT/scripts/vm-tart.sh"

mkdir -p "$RUN_DIR"
report init "$RUN_DIR" --target mac --mode "$MODE" --commit "$COMMIT" --vm-name "$VM_NAME"

fail_step() {
  local sid="$1" title="$2" reason="$3" logf="${4:-}"
  FAILED=1
  FAIL_STEP="$title"
  FAIL_REASON="$reason"
  if [ -n "$logf" ] && [ -f "$logf" ]; then
    report step-end "$RUN_DIR" "$sid" --status FAIL --detail "$reason" --log-file "$logf"
  else
    report step-end "$RUN_DIR" "$sid" --status FAIL --detail "$reason"
  fi
}

cleanup() {
  report step-start "$RUN_DIR" cleanup "清理测试机与临时文件" >/dev/null 2>&1 || true
  if [ -n "$CCHOUSE_TART_PID" ]; then
    kill "$CCHOUSE_TART_PID" 2>/dev/null || true
    wait "$CCHOUSE_TART_PID" 2>/dev/null || true
  fi
  if [ "$KEEP_VM" = "0" ]; then
    tart stop "$VM_NAME" >/dev/null 2>&1 || true
    tart delete "$VM_NAME" >/dev/null 2>&1 || true
    if [ -n "$IMPORTED_BASE" ]; then
      tart delete "$IMPORTED_BASE" >/dev/null 2>&1 || true
    fi
    report step-end "$RUN_DIR" cleanup --status PASS --detail "已删除测试机 $VM_NAME 与临时明文文件" >/dev/null 2>&1 || true
  else
    report step-end "$RUN_DIR" cleanup --status SKIP --detail "--keep-vm 已启用，保留 $VM_NAME" >/dev/null 2>&1 || true
  fi
  rm -rf "$TMP_DIR"
  if [ "$FAILED" = "0" ]; then
    report finish "$RUN_DIR" --status PASS >/dev/null 2>&1 || true
  else
    report finish "$RUN_DIR" --status FAIL --failure-step "$FAIL_STEP" --failure-reason "$FAIL_REASON" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

# ---- Step 1: Prepare ephemeral key, instance.env, and Claude Code tarball ----
report step-start "$RUN_DIR" prepare "准备临时密钥、配置与 Claude Code 包"
LOG1="$TMP_DIR/step1.log"
{
  [ -f "$ENV_SRC" ] || { echo "Missing MAC_ENV_FILE: $ENV_SRC"; exit 1; }
  ssh-keygen -q -t ed25519 -N "" -f "$TMP_DIR/id_ed25519"
  chmod 600 "$TMP_DIR/id_ed25519"
  PUB_KEY="$(cat "$TMP_DIR/id_ed25519.pub")"
  touch "$TMP_DIR/known_hosts"
  chmod 600 "$TMP_DIR/known_hosts"

  # Build ephemeral instance.env (never echoed)
  cp "$ENV_SRC" "$TMP_DIR/instance.env"
  chmod 600 "$TMP_DIR/instance.env"
  {
    echo ""
    echo "# E2E ephemeral overrides"
    echo "VM_NAME=$VM_NAME"
    echo "SSH_KEY=\"$TMP_DIR/id_ed25519\""
    echo "SSH_KNOWN_HOSTS=\"$TMP_DIR/known_hosts\""
    echo "SSH_PUBKEY='$PUB_KEY'"
  } >> "$TMP_DIR/instance.env"

  CLAUDE_TGZ="$CACHE_DIR/claude-code-linux-arm64.tgz"
  if [ "${E2E_REFRESH_CLAUDE:-0}" = "1" ] || [ ! -s "$CLAUDE_TGZ" ]; then
    echo "Fetching claude-code linux-arm64 tarball..."
    python3 "$SCRIPT_DIR/fetch-claude-cached.py" \
      --platform linux-arm64 --out "$CLAUDE_TGZ"
  else
    echo "Using cached tarball: $CLAUDE_TGZ ($(wc -c < "$CLAUDE_TGZ" | tr -d ' ') bytes)"
  fi
} >"$LOG1" 2>&1
if [ $? -ne 0 ]; then
  fail_step prepare "准备临时密钥、配置与 Claude Code 包" "准备配置或拉取 Claude Code 失败" "$LOG1"
  exit 1
fi
CLAUDE_TGZ="$CACHE_DIR/claude-code-linux-arm64.tgz"
TAG_VAL="$(awk -F= '/^TAG=/{print $2}' "$TMP_DIR/instance.env" | tail -n 1 | tr -d "\"' \r")"
STRIPE_MODE_VAL="$(awk -F= '/^STRIPE_EXIT_MODE=/{print $2}' "$TMP_DIR/instance.env" | tail -n 1 | tr -d "\"' \r")"
report step-end "$RUN_DIR" prepare --status PASS --detail "TAG=$TAG_VAL, STRIPE_EXIT_MODE=${STRIPE_MODE_VAL:-front}" --log-file "$LOG1"

# ---- Step 2: preflight + render + make-seed-iso ----
report step-start "$RUN_DIR" render "Preflight、渲染 guest-linux 与生成 seed.iso"
LOG2="$TMP_DIR/step2.log"
{
  python3 -I "$REPO_ROOT/scripts/preflight.py" --route tart-linux --env "$TMP_DIR/instance.env"
  python3 -I "$REPO_ROOT/scripts/render.py" "$TMP_DIR/instance.env" "$TMP_DIR/rendered"
  if [ "${STRIPE_MODE_VAL:-front}" != "off" ]; then
    grep -q "DOMAIN-SUFFIX,stripe.com," "$TMP_DIR/rendered/config.yaml" \
      || { echo "Rendered config.yaml missing stripe.com split rule"; exit 1; }
    echo "Verified rendered config.yaml contains Stripe split routing rule."
  fi
  python3 -I "$REPO_ROOT/scripts/make-seed-iso.py" "$TMP_DIR/rendered" "$TMP_DIR/seed.iso" \
    --claude-tarball "$CLAUDE_TGZ"
} >"$LOG2" 2>&1
if [ $? -ne 0 ]; then
  fail_step render "Preflight、渲染 guest-linux 与生成 seed.iso" "preflight / render / make-seed-iso 失败" "$LOG2"
  exit 1
fi
report step-end "$RUN_DIR" render --status PASS --detail "已渲染当前工作区 guest-linux 并生成 seed.iso" --log-file "$LOG2"

# ---- Step 3: Clone golden VM (fast) or import .tvm (release) ----
report step-start "$RUN_DIR" clone "准备测试虚拟机 ($MODE)"
LOG3="$TMP_DIR/step3.log"
{
  if [ "$MODE" = "release" ]; then
    [ -n "$TVM_PATH" ] && [ -f "$TVM_PATH" ] || { echo "Release mode requires --tvm <file> or MAC_GOLDEN_TVM"; exit 1; }
    IMPORTED_BASE="cchouse-relbase-$$"
    cchouse_tart_import "$IMPORTED_BASE" "$TVM_PATH" && cchouse_tart_clone "$IMPORTED_BASE" "$VM_NAME"
  else
    cchouse_tart_clone "$GOLDEN_VM" "$VM_NAME"
  fi
} >"$LOG3" 2>&1
if [ $? -ne 0 ]; then
  fail_step clone "准备测试虚拟机 ($MODE)" "Tart 克隆或导入镜像失败" "$LOG3"
  exit 1
fi
report step-end "$RUN_DIR" clone --status PASS --detail "VM=$VM_NAME" --log-file "$LOG3"

# ---- Step 4: Offline firstboot injection ----
report step-start "$RUN_DIR" firstboot "首次开机离线注入（挂载 seed.iso）"
LOG4="$TMP_DIR/step4.log"
{
  cchouse_tart_firstboot "$VM_NAME" "$TMP_DIR/seed.iso" "$TMP_DIR/id_ed25519" "$TMP_DIR/tart-firstboot.log"
} >"$LOG4" 2>&1
if [ $? -ne 0 ]; then
  fail_step firstboot "首次开机离线注入（挂载 seed.iso）" "首启注入阶段异常退出" "$LOG4"
  exit 1
fi
report step-end "$RUN_DIR" firstboot --status PASS --detail "首启注入结束并已关机" --log-file "$LOG4"

# ---- Step 5: Destroy seed.iso & reboot networked ----
report step-start "$RUN_DIR" reboot "销毁 seed.iso 并联网重启，等待 SSH"
LOG5="$TMP_DIR/step5.log"
GUEST_IP=""
{
  rm -rf "$TMP_DIR/seed.iso" "$TMP_DIR/rendered"
  echo "Deleted plaintext seed.iso and rendered/."
  cchouse_tart_boot "$VM_NAME" "$TMP_DIR/id_ed25519" "$TMP_DIR/known_hosts" "$TMP_DIR/guest_ip.txt" "$TMP_DIR/tart-run.log" \
    && echo "GUEST_IP=$(cat "$TMP_DIR/guest_ip.txt")" >> "$TMP_DIR/instance.env"
} >"$LOG5" 2>&1
if [ $? -ne 0 ]; then
  fail_step reboot "销毁 seed.iso 并联网重启，等待 SSH" "客机重启后无法获取 IP 或 SSH 超时" "$LOG5"
  exit 1
fi
GUEST_IP="$(cat "$TMP_DIR/guest_ip.txt")"
report set-meta "$RUN_DIR" guest_ip "$GUEST_IP"
report step-end "$RUN_DIR" reboot --status PASS --detail "SSH 已就绪: claude@$GUEST_IP" --log-file "$LOG5"

# ---- Step 6: Run verify-linux.sh --fail-closed ----
report step-start "$RUN_DIR" verify "执行 scripts/verify-linux.sh --fail-closed"
LOG6="$TMP_DIR/step6.log"
bash "$REPO_ROOT/scripts/verify-linux.sh" "$TMP_DIR/instance.env" --fail-closed >"$LOG6" 2>&1
VRC=$?
SUMMARY6="$(tail -n 1 "$LOG6" 2>/dev/null || true)"
if [ "$VRC" -ne 0 ]; then
  fail_step verify "执行 scripts/verify-linux.sh --fail-closed" "verify-linux.sh 存在失败项: $SUMMARY6" "$LOG6"
  exit 1
fi
report step-end "$RUN_DIR" verify --status PASS --detail "$SUMMARY6" --log-file "$LOG6"

# ---- Step 7: Extended checks + desktop screenshot ----
report step-start "$RUN_DIR" desktop "扩展检查（Stripe/Claude/桌面进程/镜像来源）与桌面截图"
LOG7="$TMP_DIR/step7.log"
SSH_OPTS=(-F none -i "$TMP_DIR/id_ed25519" -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$TMP_DIR/known_hosts")
ssh "${SSH_OPTS[@]}" "claude@$GUEST_IP" "bash -s -- '$TAG_VAL' '${STRIPE_MODE_VAL:-front}'" < "$SCRIPT_DIR/guest-checks.sh" >"$LOG7" 2>&1
DRC=$?

# Extract META lines for report
while IFS= read -r line; do
  case "$line" in
    META\ *=*)
      kv="${line#META }"
      k="${kv%%=*}"
      v="${kv#*=}"
      report set-meta "$RUN_DIR" "$k" "$v"
      ;;
  esac
done < "$LOG7"

# Pull screenshot and convert to PNG if generated
if scp "${SSH_OPTS[@]}" "claude@$GUEST_IP:/tmp/cchouse-desktop.xwd.gz" "$TMP_DIR/desktop.xwd.gz" >>"$LOG7" 2>&1; then
  if python3 "$SCRIPT_DIR/xwd2png.py" "$TMP_DIR/desktop.xwd.gz" "$RUN_DIR/desktop.png" >>"$LOG7" 2>&1; then
    report set-meta "$RUN_DIR" screenshot "desktop.png"
  else
    echo "FAIL xwd2png conversion failed" >>"$LOG7"
    DRC=1
  fi
else
  echo "FAIL scp desktop.xwd.gz failed" >>"$LOG7"
  DRC=1
fi

if [ "$DRC" -ne 0 ]; then
  FIRST_FAIL="$(grep -m1 '^FAIL ' "$LOG7" || echo '扩展检查或截图失败')"
  fail_step desktop "扩展检查（Stripe/Claude/桌面进程/镜像来源）与桌面截图" "$FIRST_FAIL" "$LOG7"
  exit 1
fi
report step-end "$RUN_DIR" desktop --status PASS --detail "桌面进程、Stripe 分流、Claude CLI 与截图全部通过" --log-file "$LOG7"

exit 0
