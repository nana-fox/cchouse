#!/bin/bash
# GTR (Windows + VMware Workstation) E2E runner, driven from Mac over SSH/SCP.
# Called by tools/e2e/run.sh <run_dir>
set -uo pipefail

RUN_DIR="${1:?missing run_dir}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="${E2E_REPO_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
MODE="${E2E_MODE:-fast}"
KEEP_VM="${E2E_KEEP_VM:-0}"
CACHE_DIR="${E2E_CACHE_DIR:-$HOME/Library/Application Support/cchouse/e2e/cache}"
ENV_SRC="${GTR_ENV_FILE:?GTR_ENV_FILE not set in e2e.conf}"

GTR_HOST="${GTR_SSH_HOST:?GTR_SSH_HOST not set in e2e.conf}"
GTR_USER="${GTR_SSH_USER:?GTR_SSH_USER not set in e2e.conf}"
GTR_KEY="${GTR_SSH_KEY:?GTR_SSH_KEY not set in e2e.conf}"
GTR_WORK="${GTR_WORK_DIR:-C:\\Users\\${GTR_USER}\\AppData\\Local\\cchouse\\e2e}"
GTR_OVA="${E2E_OVA_PATH:-${GTR_GOLDEN_OVA:?GTR_GOLDEN_OVA not set in e2e.conf}}"
GTR_BASE_VMX="${GTR_GOLDEN_VMX:-C:\\Users\\${GTR_USER}\\cchouse-e2e-base\\cchouse-debian13-amd64\\cchouse-debian13-amd64.vmx}"

COMMIT="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
RUN_ID="e2e-gtr-$(date +%H%M%S)-$$"
VM_NAME="cchouse-${RUN_ID}"
REMOTE_RUN_DIR="${GTR_WORK}\\runs\\${RUN_ID}"
REMOTE_RUN_SCP="$(printf '%s' "$REMOTE_RUN_DIR" | tr '\\' '/')"
TMP_DIR="$(mktemp -d)"
chmod 700 "$TMP_DIR"
FAILED=0
FAIL_STEP=""
FAIL_REASON=""

GTR_SSH_OPTS=(-F none -i "$GTR_KEY" -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new -o LogLevel=ERROR)
gtr_ssh() { ssh "${GTR_SSH_OPTS[@]}" "${GTR_USER}@${GTR_HOST}" "$@"; }
gtr_scp() { scp "${GTR_SSH_OPTS[@]}" "$@"; }
gtr_ps() {
  local action="$1"; shift
  gtr_ssh "powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"${REMOTE_RUN_DIR}\\gtr-remote.ps1\" -Action ${action} -RunDir \"${REMOTE_RUN_DIR}\" -VmName \"${VM_NAME}\" -Mode \"${MODE}\" -OvaPath \"${GTR_OVA}\" -BaseVmx \"${GTR_BASE_VMX}\" $*"
}

report() { python3 "$SCRIPT_DIR/report.py" "$@"; }

mkdir -p "$RUN_DIR"
report init "$RUN_DIR" --target gtr --mode "$MODE" --commit "$COMMIT" --vm-name "$VM_NAME"

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
  report step-start "$RUN_DIR" cleanup "清理 GTR 测试机与临时文件" >/dev/null 2>&1 || true
  if [ "$KEEP_VM" = "0" ]; then
    gtr_ps cleanup >/dev/null 2>&1 || true
    report step-end "$RUN_DIR" cleanup --status PASS --detail "已删除 GTR 测试机 $VM_NAME 与临时文件" >/dev/null 2>&1 || true
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

# ---- Step 1: Check GTR SSH + prepare ephemeral key, env & Claude Code linux-x64 tarball on Mac ----
report step-start "$RUN_DIR" prepare "检查 GTR SSH、准备密钥/配置与 Mac 侧拉取 Claude Code (x64)"
LOG1="$TMP_DIR/step1.log"
{
  [ -f "$ENV_SRC" ] || { echo "Missing GTR_ENV_FILE: $ENV_SRC"; exit 1; }
  echo "Checking GTR SSH connectivity..."
  GTR_HN="$(gtr_ssh hostname | tr -d '\r\n')"
  echo "GTR hostname: $GTR_HN"

  ssh-keygen -q -t ed25519 -N "" -f "$TMP_DIR/id_ed25519"
  chmod 600 "$TMP_DIR/id_ed25519"
  PUB_KEY="$(cat "$TMP_DIR/id_ed25519.pub")"

  # Ephemeral env for Mac-side render (uses current workspace guest-linux/)
  cp "$ENV_SRC" "$TMP_DIR/instance.env"
  chmod 600 "$TMP_DIR/instance.env"
  {
    echo ""
    echo "# E2E ephemeral overrides"
    echo "VM_NAME=$VM_NAME"
    echo "SSH_PUBKEY='$PUB_KEY'"
  } >> "$TMP_DIR/instance.env"

  # Fetch & SHA-512 verify linux-x64 tarball on Mac (avoids GTR proxy SSL issues)
  CLAUDE_TGZ="$CACHE_DIR/claude-code-linux-x64.tgz"
  if [ "${E2E_REFRESH_CLAUDE:-0}" = "1" ] || [ ! -s "$CLAUDE_TGZ" ]; then
    echo "Fetching claude-code linux-x64 tarball on Mac..."
    python3 "$SCRIPT_DIR/fetch-claude-cached.py" \
      --platform linux-x64 --out "$CLAUDE_TGZ"
  else
    echo "Using cached linux-x64 tarball: $CLAUDE_TGZ ($(wc -c < "$CLAUDE_TGZ" | tr -d ' ') bytes)"
  fi
} >"$LOG1" 2>&1
if [ $? -ne 0 ]; then
  fail_step prepare "检查 GTR SSH、准备密钥/配置与 Mac 侧拉取 Claude Code (x64)" "GTR SSH 不通或准备配置/拉取 Claude Code 失败" "$LOG1"
  exit 1
fi
CLAUDE_TGZ="$CACHE_DIR/claude-code-linux-x64.tgz"
TAG_VAL="$(awk -F= '/^TAG=/{print $2}' "$TMP_DIR/instance.env" | tail -n 1 | tr -d "\"' \r")"
STRIPE_MODE_VAL="$(awk -F= '/^STRIPE_EXIT_MODE=/{print $2}' "$TMP_DIR/instance.env" | tail -n 1 | tr -d "\"' \r")"
report step-end "$RUN_DIR" prepare --status PASS --detail "TAG=$TAG_VAL, STRIPE_EXIT_MODE=${STRIPE_MODE_VAL:-front}" --log-file "$LOG1"

# ---- Step 2: Render on Mac (current workspace guest-linux) + build seed.iso + upload to GTR ----
report step-start "$RUN_DIR" render "渲染当前工作区 guest-linux、打包 seed.iso 并上传到 GTR"
LOG2="$TMP_DIR/step2.log"
{
  python3 -I "$REPO_ROOT/scripts/render.py" "$TMP_DIR/instance.env" "$TMP_DIR/rendered"
  if [ "${STRIPE_MODE_VAL:-front}" != "off" ]; then
    grep -q "DOMAIN-SUFFIX,stripe.com," "$TMP_DIR/rendered/config.yaml" \
      || { echo "Rendered config.yaml missing stripe.com split rule"; exit 1; }
    echo "Verified rendered config.yaml contains Stripe split routing rule."
  fi
  python3 -I "$REPO_ROOT/scripts/make-seed-iso.py" "$TMP_DIR/rendered" "$TMP_DIR/seed.iso" \
    --claude-tarball "$CLAUDE_TGZ"

  # Build GTR-side instance.env for verify-linux.sh (Windows forward-slash paths work for both Git Bash & Windows OpenSSH)
  REMOTE_RUN_WIN_FWD="$(printf '%s' "$REMOTE_RUN_DIR" | tr '\\' '/')"
  cp "$TMP_DIR/instance.env" "$TMP_DIR/gtr-verify.env"
  chmod 600 "$TMP_DIR/gtr-verify.env"
  {
    echo "SSH_KEY=\"${REMOTE_RUN_WIN_FWD}/id_ed25519\""
    echo "SSH_KNOWN_HOSTS=\"${REMOTE_RUN_WIN_FWD}/known_hosts\""
  } >> "$TMP_DIR/gtr-verify.env"

  gtr_ssh "powershell.exe -NoProfile -Command \"New-Item -ItemType Directory -Force -Path '${REMOTE_RUN_DIR}' | Out-Null\""
  gtr_scp \
    "$TMP_DIR/seed.iso" \
    "$TMP_DIR/id_ed25519" \
    "$TMP_DIR/gtr-verify.env" \
    "$REPO_ROOT/scripts/verify-linux.sh" \
    "$SCRIPT_DIR/gtr-remote.ps1" \
    "$SCRIPT_DIR/guest-checks.sh" \
    "${GTR_USER}@${GTR_HOST}:${REMOTE_RUN_SCP}/"

  # Delete local plaintext seed.iso and rendered/ immediately
  rm -rf "$TMP_DIR/seed.iso" "$TMP_DIR/rendered"
  echo "Uploaded seed.iso and runner scripts to GTR, cleaned local plaintext."
} >"$LOG2" 2>&1
if [ $? -ne 0 ]; then
  fail_step render "渲染当前工作区 guest-linux、打包 seed.iso 并上传到 GTR" "render / make-seed-iso / scp 上传失败" "$LOG2"
  exit 1
fi
report step-end "$RUN_DIR" render --status PASS --detail "已渲染当前工作区并上传 seed.iso 到 GTR" --log-file "$LOG2"

# ---- Step 3: Clone linked VM (fast) or import .ova (release) on GTR ----
report step-start "$RUN_DIR" clone "GTR 准备测试虚拟机 ($MODE)"
LOG3="$TMP_DIR/step3.log"
gtr_ps clone >"$LOG3" 2>&1
if [ $? -ne 0 ]; then
  fail_step clone "GTR 准备测试虚拟机 ($MODE)" "VMware 克隆或导入 OVA 失败" "$LOG3"
  exit 1
fi
report step-end "$RUN_DIR" clone --status PASS --detail "VM=$VM_NAME ($MODE)" --log-file "$LOG3"

# ---- Step 4: Offline firstboot injection on GTR ----
report step-start "$RUN_DIR" firstboot "GTR 首次开机离线注入（断网 + seed.iso）"
LOG4="$TMP_DIR/step4.log"
gtr_ps firstboot >"$LOG4" 2>&1
if [ $? -ne 0 ]; then
  fail_step firstboot "GTR 首次开机离线注入（断网 + seed.iso）" "VMware 首启注入失败" "$LOG4"
  exit 1
fi
report step-end "$RUN_DIR" firstboot --status PASS --detail "首启注入完成并已关机" --log-file "$LOG4"

# ---- Step 5: Detach & delete seed.iso, enable NIC, reboot & wait SSH ----
report step-start "$RUN_DIR" reboot "卸载销毁 seed.iso、接通网卡并等待 SSH"
LOG5="$TMP_DIR/step5.log"
gtr_ps reboot >"$LOG5" 2>&1
if [ $? -ne 0 ]; then
  fail_step reboot "卸载销毁 seed.iso、接通网卡并等待 SSH" "重启后无法获取客机 IP 或 SSH 超时" "$LOG5"
  exit 1
fi
GUEST_IP="$(awk -F= '/^GUEST_IP=/{print $2}' "$LOG5" | tail -n 1 | tr -d '\r ')"
report set-meta "$RUN_DIR" guest_ip "${GUEST_IP:-unknown}"
report step-end "$RUN_DIR" reboot --status PASS --detail "SSH 已就绪: claude@$GUEST_IP" --log-file "$LOG5"

# ---- Step 6: Run verify-linux.sh --fail-closed on GTR ----
report step-start "$RUN_DIR" verify "在 GTR 上执行 scripts/verify-linux.sh --fail-closed"
LOG6="$TMP_DIR/step6.log"
gtr_ps verify >"$LOG6" 2>&1
VRC=$?
SUMMARY6="$(grep -E '^== 结果：' "$LOG6" | tail -n 1 || tail -n 1 "$LOG6" || true)"
if [ "$VRC" -ne 0 ]; then
  fail_step verify "在 GTR 上执行 scripts/verify-linux.sh --fail-closed" "verify-linux.sh 存在失败项: $SUMMARY6" "$LOG6"
  exit 1
fi
report step-end "$RUN_DIR" verify --status PASS --detail "$SUMMARY6" --log-file "$LOG6"

# ---- Step 7: Extended checks + desktop screenshot ----
report step-start "$RUN_DIR" desktop "扩展检查（Stripe/Claude/桌面进程/镜像来源）与桌面截图"
LOG7="$TMP_DIR/step7.log"
gtr_ps desktop -Tag "$TAG_VAL" -StripeMode "${STRIPE_MODE_VAL:-front}" >"$LOG7" 2>&1
DRC=$?

while IFS= read -r line; do
  line="$(printf '%s' "$line" | tr -d '\r')"
  case "$line" in
    META\ *=*)
      kv="${line#META }"
      k="${kv%%=*}"
      v="${kv#*=}"
      report set-meta "$RUN_DIR" "$k" "$v"
      ;;
  esac
done < "$LOG7"

if gtr_scp "${GTR_USER}@${GTR_HOST}:${REMOTE_RUN_SCP}/desktop.xwd.gz" "$TMP_DIR/desktop.xwd.gz" >>"$LOG7" 2>&1; then
  if python3 "$SCRIPT_DIR/xwd2png.py" "$TMP_DIR/desktop.xwd.gz" "$RUN_DIR/desktop.png" >>"$LOG7" 2>&1; then
    report set-meta "$RUN_DIR" screenshot "desktop.png"
  else
    echo "FAIL xwd2png conversion failed" >>"$LOG7"
    DRC=1
  fi
else
  echo "FAIL scp desktop.xwd.gz from GTR failed" >>"$LOG7"
  DRC=1
fi

if [ "$DRC" -ne 0 ]; then
  FIRST_FAIL="$(grep -m1 '^FAIL ' "$LOG7" | tr -d '\r' || echo '扩展检查或截图失败')"
  fail_step desktop "扩展检查（Stripe/Claude/桌面进程/镜像来源）与桌面截图" "$FIRST_FAIL" "$LOG7"
  exit 1
fi
report step-end "$RUN_DIR" desktop --status PASS --detail "桌面进程、Stripe 分流、Claude CLI 与截图全部通过" --log-file "$LOG7"

exit 0
