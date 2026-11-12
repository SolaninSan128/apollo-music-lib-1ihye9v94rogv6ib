"""Runtime contract checked by guards and commit_cas.

Threat model: accidental legacy writers on the same host, not a hostile user who
can already rewrite library files. A descendant of the lock holder may commit.
Anyone else fails closed. This check never LOCK_UNs the inherited descriptor:
on Linux that descriptor is the parent's open-file description, and unlocking it
would drop the kernel lock.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path

from unified_writer.entries import ENTRIES
from unified_writer.util import LockError, ensure_cas, read_meta, sha256_file


def _ppid(pid: int) -> int:
    text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    rest = text.rsplit(")", 1)[1].strip().split()
    return int(rest[1])


def pid_is_self_or_ancestor(target: int, hops: int = 16) -> bool:
    pid = os.getpid()
    for _ in range(hops):
        if pid == target:
            return True
        if pid <= 1:
            return False
        try:
            pid = _ppid(pid)
        except (OSError, IndexError, ValueError):
            return False
    return False


def assert_contract(entry: str, environ: dict | None = None) -> dict:
    env = os.environ if environ is None else environ
    if entry not in ENTRIES and entry != "library_compat":
        raise LockError("unknown_entry", entry=entry)
    if env.get("HARMONIA_WRITER_CONTRACT") != "1" or env.get("HARMONIA_CONTRACT_VERSION") != "1":
        raise LockError("legacy_bypass_no_contract", entry=entry)
    if env.get("HARMONIA_WRITER_ENTRY") != entry:
        raise LockError(
            "entry_mismatch",
            expected=entry,
            got=env.get("HARMONIA_WRITER_ENTRY"),
        )
    token = env.get("HARMONIA_WRITER_TOKEN") or ""
    if not token:
        raise LockError("legacy_bypass_no_contract", entry=entry)
    if env.get("HARMONIA_LOCK_ORDER") != "library,publish":
        raise LockError("lock_order_mismatch", got=env.get("HARMONIA_LOCK_ORDER"))
    lock_path = Path(env.get("HARMONIA_LIBRARY_LOCK_PATH") or "")
    meta = read_meta(Path(str(lock_path) + ".meta.json"))
    if meta is None:
        raise LockError("fence_lost", reason="no_meta", entry=entry)
    if meta.get("owner_token") != token:
        raise LockError("fence_lost", entry=entry)
    if str(meta.get("fence")) != str(env.get("HARMONIA_WRITER_FENCE_LIBRARY")):
        raise LockError(
            "fence_mismatch",
            kind="library",
            expected=env.get("HARMONIA_WRITER_FENCE_LIBRARY"),
            got=meta.get("fence"),
        )
    try:
        holder = int(meta.get("pid") or 0)
    except (TypeError, ValueError) as exc:
        raise LockError("fence_lost", reason="bad_pid") from exc
    if not pid_is_self_or_ancestor(holder):
        raise LockError("legacy_bypass_not_owner_descendant", meta_pid=holder, self_pid=os.getpid())
    try:
        fd = int(env["HARMONIA_LIBRARY_LOCK_FD"])
        st = os.fstat(fd)
    except (OSError, KeyError, ValueError) as exc:
        raise LockError("legacy_bypass_lock_not_held", reason=str(exc)) from exc
    if (int(st.st_dev), int(st.st_ino)) != (int(meta["dev"]), int(meta["ino"])):
        raise LockError("lock_inode_mismatch", kind="library")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise LockError("legacy_bypass_lock_not_held") from exc
    need = list(ENTRIES[entry]["locks"])
    have = [part for part in (env.get("HARMONIA_WRITER_KINDS") or "").split(",") if part]
    if have != list(need):
        raise LockError("locks_do_not_match_entry", expected=",".join(need), got=",".join(have))
    if "publish" in need:
        _assert_publish(env, token)
    return meta


def _assert_publish(env: dict, token: str) -> None:
    try:
        fd = int(env["HARMONIA_PUBLISH_LOCK_FD"])
        st = os.fstat(fd)
    except (OSError, KeyError, ValueError) as exc:
        raise LockError("publish_lock_not_held", reason=str(exc)) from exc
    path = Path(env.get("HARMONIA_PUBLISH_LOCK_PATH") or "")
    meta = read_meta(Path(str(path) + ".meta.json"))
    if meta is None or meta.get("owner_token") != token:
        raise LockError("fence_lost", kind="publish")
    if str(meta.get("fence")) != str(env.get("HARMONIA_WRITER_FENCE_PUBLISH")):
        raise LockError(
            "fence_mismatch",
            kind="publish",
            expected=env.get("HARMONIA_WRITER_FENCE_PUBLISH"),
            got=meta.get("fence"),
        )
    if (int(st.st_dev), int(st.st_ino)) != (int(meta["dev"]), int(meta["ino"])):
        raise LockError("lock_inode_mismatch", kind="publish")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise LockError("publish_lock_not_held") from exc
    claim = Path(env.get("HARMONIA_WIN_PUBLISH_CLAIM") or "")
    cmeta = read_meta(claim / "meta.json")
    if cmeta is None or cmeta.get("owner_token") != token:
        raise LockError("publish_conflict", exit_code=75, reason="win_claim_token")
    if str(cmeta.get("fence")) != str(env.get("HARMONIA_WRITER_FENCE_PUBLISH")):
        raise LockError("fence_mismatch", kind="win_publish", got=cmeta.get("fence"))


def commit_cas(path: Path, data: bytes, expected: str, entry: str | None = None) -> dict:
    """Replace ``path`` only while the contract fence and hash still match."""
    entry = entry or os.environ.get("HARMONIA_WRITER_ENTRY") or ""
    meta = assert_contract(entry)
    path = Path(path)
    if not path.is_file():
        raise LockError("cas_mismatch", reason="missing_resource", path=str(path), expected=expected)
    got = sha256_file(path)
    if got != expected:
        raise LockError("cas_mismatch", expected=expected, got=got, path=str(path))
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        again = sha256_file(path)
        if again != expected:
            raise LockError("cas_mismatch", expected=expected, got=again, path=str(path))
        fresh = read_meta(Path(str(os.environ["HARMONIA_LIBRARY_LOCK_PATH"]) + ".meta.json"))
        if fresh is None or str(fresh.get("fence")) != str(meta.get("fence")) or fresh.get("owner_token") != meta.get("owner_token"):
            raise LockError("fence_mismatch", kind="library", expected=meta.get("fence"), got=None if fresh is None else fresh.get("fence"))
        if "publish" in ENTRIES[entry]["locks"]:
            pmeta = read_meta(Path(str(os.environ["HARMONIA_PUBLISH_LOCK_PATH"]) + ".meta.json"))
            if pmeta is None or str(pmeta.get("fence")) != str(os.environ.get("HARMONIA_WRITER_FENCE_PUBLISH")):
                raise LockError("fence_mismatch", kind="publish")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return {"ok": True, "sha256": sha256_file(path), "fence": meta.get("fence")}


def require_cas_pair(resource_cas: str | None, cas_file: str | None) -> None:
    ensure_cas(resource_cas, cas_file)
