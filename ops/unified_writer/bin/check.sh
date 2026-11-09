#!/bin/bash
# Report whether a formal lock switch is allowed.
# Exit 0 means the switch is still blocked. It does not mean production is safe or installed.
# This script does not stop processes, publish, download, or edit MODE.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ -d "$HERE/unified_writer" ]; then PYROOT="$HERE"; else PYROOT="$(cd "$HERE/.." && pwd)"; fi
# This tree wins over a stale work/ops/unified_writer on PYTHONPATH or in cwd.
export HARMONIA_PATCH_ROOT="$PYROOT"

if [ "${1:-}" = "--self-test" ]; then
  shift
  export PYTHONPATH="$PYROOT${PYTHONPATH:+:$PYTHONPATH}"
  exec python3 -m unittest unified_writer.test_suite "$@"
fi

exec python3 -c '
import os, sys
from pathlib import Path
root = Path(os.environ["HARMONIA_PATCH_ROOT"]).resolve()
sys.path.insert(0, str(root))
import unified_writer
loaded = Path(unified_writer.__file__).resolve()
if loaded.parent.parent != root:
    sys.stderr.write("stale_package_shadow: %s\n" % loaded)
    raise SystemExit(76)
from unified_writer.wrapper import main
raise SystemExit(main(["check", *sys.argv[1:]]))
' "$@"
