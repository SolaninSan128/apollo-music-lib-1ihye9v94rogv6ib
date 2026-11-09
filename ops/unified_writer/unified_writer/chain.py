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
from pathlib import Path

from unified_writer.prod_guard import (
    CHAIN_INTERFACE,
    _die,
    _require_pages_git_tree,
    collision_blocks,
    guard_library_mutation,
    ordinary_git_push,
    subprocess_lock_kwargs,
)
from unified_writer.util import LockError, sha256_file

__all__ = [
    "CHAIN_INTERFACE",
    "LibraryChain",
    "collision_blocks",
    "commit_library_json",
    "guarded_pages_commit",
    "guarded_pages_publish",
    "guarded_pages_push",
    "run_guarded",
]


def _refuse_force(argv: list) -> None:
    bad = [arg for arg in argv if arg in ("--force", "-f", "--force-with-lease") or (isinstance(arg, str) and arg.startswith("+"))]
    if bad:
        raise SystemExit("ordinary_push_only")


def commit_library_json(path, obj, expected_sha: str | None = None) -> str:
    """Atomic library.json replace. expected_sha is the preimage CAS.

    Refusals raise SystemExit(76). The status line names library_json_only or cas_mismatch.
    """
    path = os.fspath(path)
    if os.path.basename(path) != "library.json":
        _die(LockError("library_json_only", exit_code=76))
    guard_library_mutation("library_compat", path)
    if expected_sha is not None:
        if not os.path.isfile(path) or sha256_file(path) != expected_sha:
            _die(LockError("cas_mismatch", exit_code=76))
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
            _die(LockError("cas_mismatch", exit_code=76))
        digest = commit_library_json(path, obj, expected_sha=expected)
        if os.path.basename(path) == "library.json":
            self._sha = digest
        return digest

    def run(self, argv: list, cwd: str | None = None) -> int:
        return run_guarded(argv, cwd=cwd)

    def pages_push(self, deploy_dir: str) -> None:
        """Push origin main of the anchored deploy git tree. Does not add or commit."""
        guarded_pages_push(deploy_dir)

    def pages_publish(self, deploy_dir: str, paths: list[str], message: str) -> None:
        """git add, git commit, then git push origin main, in that order."""
        guarded_pages_publish(deploy_dir, paths, message)


def _git(cwd: str, args: list[str]) -> None:
    proc = subprocess.run(["git", *args], cwd=cwd, check=False)
    if proc.returncode != 0:
        _die(LockError("git_failed", exit_code=76))


def _safe_paths(paths: list[str]) -> list[str]:
    if not paths:
        _die(LockError("pages_paths_required", exit_code=76))
    clean = []
    for rel in paths:
        text = os.fspath(rel)
        parts = Path(text).parts
        if not text or text.startswith("/") or ".." in parts or Path(text).is_absolute():
            _die(LockError("pages_path_escape", exit_code=76))
        clean.append(text)
    return clean


def guarded_pages_commit(deploy_dir: str, paths: list[str], message: str) -> str:
    """git add then git commit in the real Pages deploy git tree. Not a dated package directory."""
    if not str(message or "").strip():
        _die(LockError("pages_message_required", exit_code=76))
    root = _require_pages_git_tree(deploy_dir)
    clean = _safe_paths(list(paths))
    guard_library_mutation("build_push", None)
    _git(root, ["add", "--", *clean])
    _git(root, ["commit", "-m", message])
    return root


def guarded_pages_push(deploy_dir: str) -> str:
    """git push origin main only. The directory must be the anchored deploy git tree."""
    root = _require_pages_git_tree(deploy_dir)
    guard_library_mutation("build_push", None)
    ordinary_git_push(root)
    return root


def guarded_pages_publish(deploy_dir: str, paths: list[str], message: str) -> str:
    """Add, commit, then push. Push is not used as a substitute for the commit."""
    root = guarded_pages_commit(deploy_dir, paths, message)
    guarded_pages_push(root)
    return root
