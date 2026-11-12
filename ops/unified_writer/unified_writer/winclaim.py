"""Windows-safe publish claim.

The publication host is Windows and does not honor Linux flock. The claim is a
directory created with mkdir, which is atomic on NTFS and on Linux. This module
does not import fcntl so it can be copied next to the Windows publish helper.

A live claim is a conflict. An expired claim is recovered only when the caller
presents a matching resource hash; otherwise recovery fails closed. A remote
host's pid is not treated as proof of death.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from unified_writer.util import (
    LockError,
    bump_fence,
    ensure_cas,
    lease_expired,
    read_meta,
    write_json_atomic,
)

CLAIM_DIRNAME = "publish.claim.d"


def claim_dir(parent: Path) -> Path:
    return Path(parent) / CLAIM_DIRNAME


def fence_path(parent: Path) -> Path:
    return Path(parent) / "publish.fence"


def _grave(parent: Path, fence) -> Path:
    return parent / f"{CLAIM_DIRNAME}.dead.{fence}.{time.time_ns()}"


def _drop_dir(path: Path) -> None:
    meta = path / "meta.json"
    if meta.exists():
        meta.unlink()
    try:
        path.rmdir()
    except OSError:
        pass


def acquire_claim(
    parent: Path,
    *,
    token: str,
    lease_sec: float,
    hostname: str,
    pid: int,
    who: str,
    cmd: str,
    resource_cas: str | None,
    cas_file: str | None,
    wait_sec: float,
    remote_skew_sec: float,
) -> dict:
    parent = Path(parent)
    parent.mkdir(parents=True, exist_ok=True)
    claim = claim_dir(parent)
    deadline = time.monotonic() + max(0.0, wait_sec)
    while True:
        if claim.exists():
            meta_path = claim / "meta.json"
            if meta_path.exists():
                meta = read_meta(meta_path)
            else:
                meta = None
            if meta is None:
                age = time.time() - claim.stat().st_mtime
                if age < 2.0 and time.monotonic() < deadline:
                    time.sleep(0.02)
                    continue
                if age < max(lease_sec, 0):
                    raise LockError("claim_corrupt_fail_closed", path=str(claim))
                ensure_cas(resource_cas, cas_file)
                grave = _grave(parent, "corrupt")
                try:
                    os.rename(claim, grave)
                except FileNotFoundError:
                    continue
            else:
                if not lease_expired(meta, hostname, remote_skew_sec):
                    raise LockError(
                        "publish_conflict",
                        exit_code=75,
                        holder_host=meta.get("host"),
                        holder_pid=meta.get("pid"),
                        holder_fence=meta.get("fence"),
                        holder_token_prefix=str(meta.get("owner_token", ""))[:8],
                    )
                ensure_cas(resource_cas, cas_file)
                grave = _grave(parent, meta.get("fence"))
                try:
                    os.rename(claim, grave)
                except FileNotFoundError:
                    continue
        try:
            os.mkdir(claim)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise LockError("publish_conflict", exit_code=75, reason="mkdir_lost")
            time.sleep(0.02)
    try:
        fence = bump_fence(fence_path(parent))
        now = time.time()
        meta = {
            "kind": "publish",
            "pid": pid,
            "host": hostname,
            "owner_token": token,
            "fence": fence,
            "since_epoch": now,
            "lease_until": now + float(lease_sec),
            "lease_sec": float(lease_sec),
            "who": who,
            "cmd": cmd[:500],
            "resource_cas": resource_cas,
            "contract_version": 1,
            "backend": "win_mkdir",
        }
        write_json_atomic(claim / "meta.json", meta)
        return meta
    except Exception:
        _drop_dir(claim)
        raise


def release_claim(parent: Path, token: str) -> None:
    claim = claim_dir(parent)
    meta_path = claim / "meta.json"
    meta = read_meta(meta_path) if meta_path.exists() else None
    if not meta or meta.get("owner_token") != token:
        raise LockError("fence_lost", path=str(claim))
    meta_path.unlink()
    claim.rmdir()


def heartbeat_claim(parent: Path, token: str, lease_sec: float) -> None:
    meta_path = claim_dir(parent) / "meta.json"
    meta = read_meta(meta_path)
    if not meta or meta.get("owner_token") != token:
        raise LockError("fence_lost", path=str(meta_path))
    now = time.time()
    meta["since_epoch"] = now
    meta["lease_until"] = now + float(lease_sec)
    write_json_atomic(meta_path, meta)
