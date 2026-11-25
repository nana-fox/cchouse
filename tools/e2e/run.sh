#!/bin/bash
# cchouse one-command real-machine E2E verification runner.
#
# Usage:
#   tools/e2e/run.sh --target mac|gtr|both --mode fast|release \
#     [--config <e2e.conf>] [--ova <path>] [--tvm <path>] [--keep-vm] [--refresh-claude]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

TARGET="both"
MODE="fast"
CONFIG_FILE="${CCHOUSE_E2E_CONFIG:-$HOME/Library/Application Support/cchouse/e2e/e2e.conf}"
OVA_PATH=""
TVM_PATH=""
KEEP_VM=0
REFRESH_CLAUDE=0

usage() {
  cat <<'EOF'
Usage: tools/e2e/run.sh [options]
  --target mac|gtr|both   Target machine(s) to verify (default: both)
  --mode fast|release     fast: clone golden VM; release: import .ova/.tvm (default: fast)
  --config <path>         Private host config file (default: ~/Library/Application Support/cchouse/e2e/e2e.conf)
  --ova <path>            Path to .ova on GTR for release mode (overrides GTR_GOLDEN_OVA)
  --tvm <path>            Path to .tvm on Mac for release mode (overrides MAC_GOLDEN_TVM)
  --keep-vm               Do not delete the test VM after completion (for debugging)
  --refresh-claude        Force re-download of cached Claude Code tarballs
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --target) TARGET="${2:?missing value for --target}"; shift 2 ;;
    --mode) MODE="${2:?missing value for --mode}"; shift 2 ;;
    --config) CONFIG_FILE="${2:?missing value for --config}"; shift 2 ;;
    --ova) OVA_PATH="${2:?missing value for --ova}"; shift 2 ;;
    --tvm) TVM_PATH="${2:?missing value for --tvm}"; shift 2 ;;
    --keep-vm) KEEP_VM=1; shift ;;
    --refresh-claude) REFRESH_CLAUDE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$TARGET" in
  mac|gtr|both) ;;
  *) echo "Invalid --target: $TARGET (expected mac|gtr|both)" >&2; exit 2 ;;
esac
case "$MODE" in
  fast|release) ;;
  *) echo "Invalid --mode: $MODE (expected fast|release)" >&2; exit 2 ;;
esac

if [ ! -f "$CONFIG_FILE" ]; then
  echo "Private config not found: $CONFIG_FILE" >&2
  echo "See tools/e2e/README.md for e2e.conf format." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
. "$CONFIG_FILE"
set +a

export E2E_REPO_ROOT="$REPO_ROOT"
export E2E_MODE="$MODE"
export E2E_KEEP_VM="$KEEP_VM"
export E2E_REFRESH_CLAUDE="$REFRESH_CLAUDE"
export E2E_CACHE_DIR="${E2E_CACHE_DIR:-$HOME/Library/Application Support/cchouse/e2e/cache}"
export E2E_RUNS_DIR="${E2E_RUNS_DIR:-$HOME/Library/Application Support/cchouse/e2e-runs}"
export E2E_OVA_PATH="${OVA_PATH:-${GTR_GOLDEN_OVA:-}}"
export E2E_TVM_PATH="${TVM_PATH:-${MAC_GOLDEN_TVM:-}}"

mkdir -p "$E2E_CACHE_DIR" "$E2E_RUNS_DIR"
chmod 700 "$E2E_CACHE_DIR" "$E2E_RUNS_DIR"

OVERALL_RC=0
MAC_REPORT=""
GTR_REPORT=""

if [ "$TARGET" = "mac" ] || [ "$TARGET" = "both" ]; then
  RUN_TS="$(date +%Y%m%d-%H%M%S)"
  MAC_RUN_DIR="$E2E_RUNS_DIR/${RUN_TS}-mac"
  echo "==> Running Mac E2E ($MODE) -> $MAC_RUN_DIR/report.html"
  if bash "$SCRIPT_DIR/run-mac.sh" "$MAC_RUN_DIR"; then
    echo "PASS Mac E2E: $MAC_RUN_DIR/report.html"
  else
    echo "FAIL Mac E2E: $MAC_RUN_DIR/report.html" >&2
    OVERALL_RC=1
  fi
  MAC_REPORT="$MAC_RUN_DIR/report.html"
fi

if [ "$TARGET" = "gtr" ] || [ "$TARGET" = "both" ]; then
  RUN_TS="$(date +%Y%m%d-%H%M%S)"
  GTR_RUN_DIR="$E2E_RUNS_DIR/${RUN_TS}-gtr"
  echo "==> Running GTR E2E ($MODE) -> $GTR_RUN_DIR/report.html"
  if bash "$SCRIPT_DIR/run-gtr.sh" "$GTR_RUN_DIR"; then
    echo "PASS GTR E2E: $GTR_RUN_DIR/report.html"
  else
    echo "FAIL GTR E2E: $GTR_RUN_DIR/report.html" >&2
    OVERALL_RC=1
  fi
  GTR_REPORT="$GTR_RUN_DIR/report.html"
fi

echo ""
echo "=== E2E Summary ==="
[ -n "$MAC_REPORT" ] && echo "  Mac report: $MAC_REPORT"
[ -n "$GTR_REPORT" ] && echo "  GTR report: $GTR_REPORT"
exit "$OVERALL_RC"
