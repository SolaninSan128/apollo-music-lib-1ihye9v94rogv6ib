#!/bin/bash
# Restore the newest install backup under DEST. Only paths under DEST/work/ops.
# Does not restart processes or edit library/MODE/homepage.
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
META="$DEST/work/.unified_writer_install.json"
[ -f "$META" ] || { echo "no install manifest at $META" >&2; exit 76; }
BACK="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1],encoding="utf-8"))["latest_backup"])' "$META")"
[ -f "$BACK/actions.tsv" ] || { echo "backup actions missing: $BACK" >&2; exit 76; }
OPS="$DEST/work/ops"
while IFS="$(printf '\t')" read -r action rel; do
  [ -n "${action:-}" ] || continue
  dst="$OPS/$rel"
  case "$dst" in
    "$OPS"/*) ;;
    *) echo "refuse path $dst" >&2; exit 76 ;;
  esac
  if [ "$action" = "created" ]; then
    rm -rf "$dst"
  elif [ "$action" = "replaced" ]; then
    rm -rf "$dst"
    mkdir -p "$(dirname "$dst")"
    cp -a "$BACK/$rel" "$dst"
  else
    echo "unknown action $action" >&2
    exit 76
  fi
done < "$BACK/actions.tsv"
echo "rolled back $OPS from $BACK"
