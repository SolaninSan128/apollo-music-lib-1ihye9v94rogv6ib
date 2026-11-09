"""Shared helpers with no fcntl dependency (safe to import on Windows)."""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import time
from pathlib import Path


class LockError(Exception):
    def __init__(self, code: str, exit_code: int = 76, **detail):
        super().__init__(code)
        self.code = code
        self.exit_code = int(exit_code)
        self.detail = detail

    def to_dict(self) -> dict:
        out = {"ok": False, "error": self.code, "exit_code": self.exit_code}
        out.update(self.detail)
        return out


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_meta(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LockError("meta_corrupt_fail_closed", path=str(path), reason=str(exc)) from exc
    if not isinstance(data, dict) or "owner_token" not in data or "lease_until" not in data:
        raise LockError("meta_corrupt_fail_closed", path=str(path), reason="missing_fields")
    return data


def write_json_atomic(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def bump_fence(path: Path) -> int:
    current = 0
    if path.exists():
        raw = path.read_text(encoding="utf-8").strip()
        if raw:
            try:
                current = int(raw)
            except ValueError as exc:
                raise LockError("fence_corrupt_fail_closed", path=str(path)) from exc
    nxt = current + 1
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(f"{nxt}\n", encoding="utf-8")
    os.replace(tmp, path)
    return nxt


def dump_env_file(path: Path, mapping: dict) -> None:
    lines = [f"{key}={shlex.quote(str(value))}\n" for key, value in mapping.items()]
    path.write_text("".join(lines), encoding="utf-8")
    os.chmod(path, 0o600)


def lease_expired(meta: dict, local_host: str, remote_skew_sec: float) -> bool:
    until = float(meta["lease_until"])
    host = meta.get("host")
    skew = float(remote_skew_sec) if host not in (None, "", local_host) else 0.0
    return time.time() >= until + skew


def ensure_cas(resource_cas: str | None, cas_file: str | None) -> str:
    if not resource_cas or not cas_file:
        raise LockError("expired_dirty_needs_cas")
    path = Path(cas_file)
    if not path.is_file():
        raise LockError("cas_mismatch", reason="missing_resource", path=str(path), expected=resource_cas)
    got = sha256_file(path)
    if got != resource_cas:
        raise LockError("cas_mismatch", expected=resource_cas, got=got, path=str(path))
    return got
