#!/bin/bash
# Report whether a formal lock switch is allowed.
# Exit 0 means the switch is still blocked. It does not mean production is safe or installed.
# This script does not stop processes, publish, download, or edit MODE.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ -d "$HERE/unified_writer" ]; then PYROOT="$HERE"; else PYROOT="$(cd "$HERE/.." && pwd)"; fi
export PYTHONPATH="$PYROOT${PYTHONPATH:+:$PYTHONPATH}"

if [ "${1:-}" = "--self-test" ]; then
  shift
  exec python3 -m unittest unified_writer.test_suite "$@"
fi

LOCK=""
MODE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --library-lock) LOCK="$2"; shift 2 ;;
    --mode-file) MODE="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 76 ;;
  esac
done
exec python3 -m unified_writer.wrapper stop-write-check --library-lock "$LOCK" --mode-file "$MODE"
