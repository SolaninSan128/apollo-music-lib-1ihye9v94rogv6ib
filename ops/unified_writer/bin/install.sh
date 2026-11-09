#!/bin/bash
# Copy this artifact into DEST/work/ops. Does not touch library data, MODE, homepage,
# schedules, or a live lock. Refuses the Pages repo and the production handoff path.
set -euo pipefail
DEST=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dest) DEST="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 76 ;;
  esac
done
[ -n "$DEST" ] || { echo "need --dest DIR" >&2; exit 76; }
DEST="$(realpath -m "$DEST")"
case "$DEST" in
  /|/workspace|/workspace/) echo "refuse dest $DEST" >&2; exit 76 ;;
esac
if [[ "$DEST" == *harmonia-handoff* ]]; then
  echo "refuse production handoff path: $DEST" >&2
  exit 76
fi
if [ -f "$DEST/index.html" ] && [ -d "$DEST/img" ]; then
  echo "refuse dest that looks like the Pages repo: $DEST" >&2
  exit 76
fi

SRC="$(cd "$(dirname "$0")/.." && pwd)"
OPS="$DEST/work/ops"
mkdir -p "$OPS"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACK="$DEST/work/.unified_writer_backup/$STAMP"
mkdir -p "$BACK"
: > "$BACK/actions.tsv"

install_path() {
  rel="$1"
  src="$2"
  dst="$OPS/$rel"
  case "$dst" in
    "$OPS"/*) ;;
    *) echo "refuse path $dst" >&2; exit 76 ;;
  esac
  mkdir -p "$(dirname "$dst")"
  if [ -e "$dst" ]; then
    mkdir -p "$(dirname "$BACK/$rel")"
    cp -a "$dst" "$BACK/$rel"
    printf 'replaced\t%s\n' "$rel" >> "$BACK/actions.tsv"
  else
    printf 'created\t%s\n' "$rel" >> "$BACK/actions.tsv"
  fi
  rm -rf "$dst"
  cp -a "$src" "$dst"
}

for script in with_library_lock.sh with_unified_lock guard-ratings guard-build-push \
  guard-download-writeback guard-increment guard-publish check.sh rollback.sh install.sh; do
  install_path "$script" "$SRC/bin/$script"
done
install_path "unified_writer" "$SRC/unified_writer"
install_path "reference" "$SRC/reference"
install_path "README.md" "$SRC/README.md"
install_path "LOMAX_CONTRACT.md" "$SRC/LOMAX_CONTRACT.md"
install_path "MISSING_PRODUCTION_FILES.md" "$SRC/MISSING_PRODUCTION_FILES.md"

python3 - "$DEST" "$BACK" << 'PY'
import json, sys
from pathlib import Path
dest, backup = sys.argv[1:]
path = Path(dest) / "work" / ".unified_writer_install.json"
path.write_text(json.dumps({
    "latest_backup": backup,
    "production_install": False,
    "deployed_this_run": False,
    "contract_version": 1,
    "note": "files were copied into this dest only; nothing was restarted or published",
}, indent=2) + "\n", encoding="utf-8")
PY
echo "installed preview under $OPS"
echo "backup $BACK"
