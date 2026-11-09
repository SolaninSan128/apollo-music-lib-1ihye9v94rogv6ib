#!/bin/bash
# work/ops/with_library_lock.sh <command...>
# Same call shape and same flock inode as the legacy script.
# Exit 75 if the kernel lock is not acquired within LOCK_WAIT seconds (default 1800).
# A dirty meta left by a crash fails closed until its lease ends and a CAS hash matches.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ -d "$HERE/unified_writer" ]; then
  PYROOT="$HERE"
else
  PYROOT="$(cd "$HERE/.." && pwd)"
fi
export PYTHONPATH="$PYROOT${PYTHONPATH:+:$PYTHONPATH}"

if [ -n "${HARMONIA_LIBRARY_LOCK:-}" ]; then
  LOCK="$HARMONIA_LIBRARY_LOCK"
elif [ "$(basename "$HERE")" = "ops" ]; then
  LOCK="$(cd "$HERE/.." && pwd)/.library.lock"
else
  echo "set HARMONIA_LIBRARY_LOCK, or install this script as work/ops/with_library_lock.sh" >&2
  exit 76
fi
[ $# -ge 1 ] || { echo "用法：$0 <命令...>" >&2; exit 2; }

WAIT="${LOCK_WAIT:-1800}"
ROOT="${HARMONIA_LOCK_ROOT:-$(dirname "$LOCK")/.unified_lock}"
mkdir -p "$(dirname "$LOCK")"
exec 9>>"$LOCK"
if ! flock -w "$WAIT" 9; then
  echo "等 work/.library.lock 超时（${WAIT} 秒），没执行：$*" >&2
  exit 75
fi

ENVF="$(mktemp)"
TOKEN=""
cleanup() {
  status=$?
  trap - EXIT
  if [ -n "${TOKEN:-}" ]; then
    python3 -m unified_writer.wrapper compat-end --fd 9 --lock "$LOCK" --token "$TOKEN" || true
  fi
  rm -f "$ENVF"
  exit "$status"
}
trap cleanup EXIT

python3 -m unified_writer.wrapper compat-begin \
  --fd 9 \
  --lock "$LOCK" \
  --root "$ROOT" \
  --entry "${HARMONIA_WRITER_ENTRY:-library_compat}" \
  --env-out "$ENVF" \
  --lease "${HARMONIA_LEASE_SEC:-600}" \
  --skew "${HARMONIA_REMOTE_SKEW_SEC:-30}" \
  --cas-file "${HARMONIA_CAS_FILE:-}" \
  --cas-hash "${HARMONIA_RESOURCE_CAS:-}" \
  --cmd "$*"
set -a
# shellcheck disable=SC1090
. "$ENVF"
set +a
TOKEN="${HARMONIA_WRITER_TOKEN}"
"$@"
