"""Entry guards. They do not implement ratings, build, download, or publish.

A guard runs only as a child of the wrapper (contract env + inherited flock fd).
If the real program path is unset or missing, it fails closed. Publish --execute
also fails closed until HARMONIA_ALLOW_PUBLISH_EXECUTE=1 and
HARMONIA_WIN_LOCK_HELPER are both set. This package never sets those itself.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from unified_writer.entries import ENTRIES
from unified_writer.contract import assert_contract
from unified_writer.util import LockError

EXECUTE_ENTRIES = {"publish"}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ENTRIES or argv[0] == "library_compat":
        sys.stderr.write("usage: python3 -m unified_writer.guards <entry> [--] [args]\n")
        return 76
    entry = argv[0]
    rest = argv[1:]
    if rest and rest[0] == "--":
        rest = rest[1:]
    try:
        assert_contract(entry)
        spec = ENTRIES[entry]
        if spec["requires_cas"] and not os.environ.get("HARMONIA_RESOURCE_CAS"):
            raise LockError("cas_required", entry=entry)
        if entry in EXECUTE_ENTRIES and "--execute" in rest:
            if os.environ.get("HARMONIA_ALLOW_PUBLISH_EXECUTE") != "1":
                raise LockError("publish_execute_blocked", entry=entry)
            if not os.environ.get("HARMONIA_WIN_LOCK_HELPER"):
                raise LockError("win_publish_boundary_unconfigured", entry=entry)
        real = os.environ.get(spec["real_env"], "")
        if not real:
            raise LockError("missing_real_entrypoint", entry=entry, expected=spec["expected"])
        if not Path(real).is_file():
            raise LockError("missing_real_entrypoint", entry=entry, path=real, expected=spec["expected"])
        if os.access(real, os.X_OK):
            os.execvpe(real, [real, *rest], os.environ)
        if real.endswith(".py"):
            python = os.environ.get("PYTHON", sys.executable)
            os.execvpe(python, [python, real, *rest], os.environ)
        raise LockError("real_entrypoint_not_executable", entry=entry, path=real)
    except LockError as exc:
        sys.stderr.write(json.dumps(exc.to_dict(), ensure_ascii=False) + "\n")
        return exc.exit_code
    return 76


if __name__ == "__main__":
    raise SystemExit(main())
