"""Stable interface for date-specific scripts.

06:00 and 07:00 generate ingest_YYYY_MM_DD.py and daily_artist_YYYYMMDD.py.
Those names are not special. The script imports this module, or imports
run_all_queues and calls the functions that module re-exports.

Download queues stay outside the library lock. library.json, build.py,
make_deploy_variants.py and the ordinary Pages push run inside it.
Main-site publish is arm_site_publish / harmonia_publish.py, not this chain.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

from unified_writer.prod_guard import (
    CHAIN_INTERFACE,
    collision_blocks,
    guard_library_mutation,
    ordinary_git_push,
    subprocess_lock_kwargs,
)
from unified_writer.util import sha256_file

__all__ = [
    "CHAIN_INTERFACE",
    "LibraryChain",
    "collision_blocks",
    "commit_library_json",
    "run_guarded",
]


def _refuse_force(argv: list) -> None:
    bad = [arg for arg in argv if arg in ("--force", "-f", "--force-with-lease") or (isinstance(arg, str) and arg.startswith("+"))]
    if bad:
        raise SystemExit("ordinary_push_only")


def commit_library_json(path, obj, expected_sha: str | None = None) -> str:
    """Atomic library.json replace. expected_sha is the preimage CAS."""
    path = os.fspath(path)
    if os.path.basename(path) != "library.json":
        raise SystemExit("library_json_only")
    guard_library_mutation("library_compat", path)
    if expected_sha is not None:
        if not os.path.isfile(path) or sha256_file(path) != expected_sha:
            raise SystemExit("cas_mismatch")
    blob = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    tmp = path + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(blob)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return sha256_file(path)


def run_guarded(argv: list, cwd: str | None = None) -> int:
    """Run build.py, make_deploy_variants.py, or an ordinary git push under the lock."""
    argv = [str(part) for part in argv]
    _refuse_force(argv)
    guard_library_mutation("build_push", None)
    kwargs = subprocess_lock_kwargs()
    proc = subprocess.run(argv, cwd=cwd, check=False, **kwargs)
    return proc.returncode


class LibraryChain:
    def __init__(self, entry: str = "daily_ingest", library_path: str | None = None):
        self.entry = entry
        self.library_path = library_path
        self._sha: str | None = None

    def __enter__(self):
        guard_library_mutation(self.entry if self.entry else "library_compat", None)
        if self.library_path and os.path.isfile(self.library_path):
            self._sha = sha256_file(self.library_path)
        return self

    def __exit__(self, exc_type, exc, tb):
        from unified_writer.prod_guard import release_if_owned
        release_if_owned()
        return False

    def commit_json(self, path, obj) -> str:
        path = os.fspath(path)
        expected = self._sha if os.path.basename(path) == "library.json" else None
        if expected is not None and sha256_file(path) != expected:
            raise SystemExit("cas_mismatch")
        digest = commit_library_json(path, obj, expected_sha=expected)
        if os.path.basename(path) == "library.json":
            self._sha = digest
        return digest

    def run(self, argv: list, cwd: str | None = None) -> int:
        return run_guarded(argv, cwd=cwd)

    def pages_push(self, deploy_dir: str) -> None:
        """Ordinary `git push origin main` after the caller has committed. No --force."""
        guard_library_mutation("build_push", None)
        ordinary_git_push(deploy_dir)
