"""Linux library/publish coordinator.

Library exclusion uses flock(2) on one inode, the same inode
``with_library_lock.sh`` and util-linux ``flock`` already use. A second open of
that inode blocks even inside the holder, so callers must keep the inherited
descriptor and must not reopen the lock file.

Publish exclusion adds a mkdir claim (see winclaim.py) after the library flock.
Order is always library, then publish. Release is the reverse.

Kernel flock is the mutex. A leftover ``.owner`` file is not. A dirty meta left
by a crash does not grant the next writer the epoch until the lease is over and
the resource hash matches; otherwise the attempt fails closed and the meta stays.
"""
from __future__ import annotations

import fcntl
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path

from unified_writer.entries import normalize_kinds
from unified_writer.util import (
    LockError,
    bump_fence,
    ensure_cas,
    lease_expired,
    read_meta,
    sha256_file,
    write_json_atomic,
)
from unified_writer.winclaim import acquire_claim, heartbeat_claim, release_claim


@dataclass
class Held:
    kind: str
    fd: int
    path: Path
    close_fd: bool


class Layout:
    def __init__(self, root: Path, library_lock: Path | None = None, win_parent: Path | None = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.library_lock = Path(library_lock) if library_lock else self.root / "library.lock"
        self.library_lock.parent.mkdir(parents=True, exist_ok=True)
        self.publish_lock = self.root / "publish.lock"
        self.win_parent = Path(win_parent) if win_parent else self.root / "winpub"

    @staticmethod
    def meta_for(lock_path: Path) -> Path:
        return Path(str(lock_path) + ".meta.json")

    @staticmethod
    def owner_for(lock_path: Path) -> Path:
        return Path(str(lock_path) + ".owner")

    def library_fence(self) -> Path:
        return Path(str(self.library_lock) + ".fence")


def drop_inherited_meta(fd: int, lock_path: Path, token: str) -> None:
    """Remove our sidecar after a compat command. Leave flock to the shell."""
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise LockError("legacy_bypass_lock_not_held") from exc
    meta_path = Layout.meta_for(lock_path)
    meta = read_meta(meta_path)
    if not meta or meta.get("owner_token") != token:
        raise LockError("fence_lost", path=str(meta_path))
    st = os.fstat(fd)
    if (int(st.st_dev), int(st.st_ino)) != (int(meta["dev"]), int(meta["ino"])):
        raise LockError("lock_inode_mismatch")
    meta_path.unlink()


class WriterSession:
    def __init__(
        self,
        layout: Layout,
        *,
        lease_sec: float = 600,
        remote_skew_sec: float = 30,
        who: str = "unified",
        hostname: str | None = None,
        owner_token: str | None = None,
        holder_pid: int | None = None,
    ):
        self.layout = layout
        self.lease_sec = float(lease_sec)
        self.remote_skew_sec = float(remote_skew_sec)
        self.who = who
        self.hostname = hostname or socket.gethostname()
        self.owner_token = owner_token or f"{os.getpid():x}-{time.time_ns():x}"
        self.holder_pid = os.getpid() if holder_pid is None else int(holder_pid)
        self.held: list[Held] = []
        self.win_held = False
        self.fences: dict[str, int] = {}
        self.order: tuple[str, ...] = ()
        self.resource_cas: str | None = None
        self._entered = False

    def acquire(
        self,
        kinds: tuple[str, ...] | list[str],
        *,
        wait_sec: float,
        resource_cas: str | None = None,
        cas_file: str | None = None,
        cmd: str = "",
    ) -> dict:
        if self.held or self.win_held:
            raise LockError("already_held")
        self.resource_cas = resource_cas or None
        wanted = normalize_kinds(tuple(kinds))
        self._entered = True
        try:
            self._acquire_library(wait_sec, resource_cas, cas_file, cmd)
            if "publish" in wanted:
                self._acquire_publish(wait_sec, resource_cas, cas_file, cmd)
            self.order = wanted
            return self.describe()
        except Exception:
            self.release()
            raise

    def adopt_library_fd(
        self,
        fd: int,
        *,
        resource_cas: str | None,
        cas_file: str | None,
        cmd: str,
    ) -> dict:
        """Take over an flock already held on ``fd`` (shell compat). Do not reopen."""
        if self.held:
            raise LockError("already_held")
        self.resource_cas = resource_cas or None
        self._entered = True
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise LockError("busy:library", exit_code=75) from exc
            st = os.fstat(fd)
            try:
                other = os.stat(self.layout.library_lock)
            except FileNotFoundError as exc:
                raise LockError("lock_inode_mismatch") from exc
            if (st.st_dev, st.st_ino) != (other.st_dev, other.st_ino):
                raise LockError("lock_inode_mismatch")
            self._claim_flock_meta(
                "library",
                fd,
                self.layout.library_lock,
                self.layout.library_fence(),
                resource_cas,
                cas_file,
                cmd,
                close_fd=False,
            )
            self.order = ("library",)
            return self.describe()
        except Exception:
            self.release()
            raise

    def describe(self) -> dict:
        return {
            "ok": True,
            "order": list(self.order),
            "owner_token": self.owner_token,
            "fences": dict(self.fences),
            "contract_version": 1,
            "hostname": self.hostname,
            "holder_pid": self.holder_pid,
            "win_claim": str(self.layout.win_parent / "publish.claim.d") if self.win_held else "",
        }

    def contract_env(self, entry: str) -> dict:
        library = next(h for h in self.held if h.kind == "library")
        publish = next((h for h in self.held if h.kind == "publish"), None)
        return {
            "HARMONIA_WRITER_CONTRACT": "1",
            "HARMONIA_CONTRACT_VERSION": "1",
            "HARMONIA_WRITER_ENTRY": entry,
            "HARMONIA_WRITER_TOKEN": self.owner_token,
            "HARMONIA_WRITER_FENCE_LIBRARY": str(self.fences["library"]),
            "HARMONIA_WRITER_FENCE_PUBLISH": "" if "publish" not in self.fences else str(self.fences["publish"]),
            "HARMONIA_WRITER_KINDS": ",".join(self.order),
            "HARMONIA_LIBRARY_LOCK_FD": str(library.fd),
            "HARMONIA_PUBLISH_LOCK_FD": "" if publish is None else str(publish.fd),
            "HARMONIA_LIBRARY_LOCK_PATH": str(self.layout.library_lock),
            "HARMONIA_PUBLISH_LOCK_PATH": "" if publish is None else str(publish.path),
            "HARMONIA_WIN_PUBLISH_CLAIM": str(self.layout.win_parent / "publish.claim.d") if self.win_held else "",
            "HARMONIA_RESOURCE_CAS": self.resource_cas or "",
            "HARMONIA_LOCK_ORDER": "library,publish",
            "HARMONIA_LOCK_ROOT": str(self.layout.root),
            "HARMONIA_WIN_LOCK_HELPER": os.environ.get("HARMONIA_WIN_LOCK_HELPER", ""),
        }

    def heartbeat(self) -> None:
        now = time.time()
        for held in self.held:
            path = Layout.meta_for(held.path)
            meta = read_meta(path)
            if not meta or meta.get("owner_token") != self.owner_token:
                raise LockError("fence_lost", kind=held.kind)
            meta["since_epoch"] = now
            meta["lease_until"] = now + self.lease_sec
            write_json_atomic(path, meta)
        if self.win_held:
            heartbeat_claim(self.layout.win_parent, self.owner_token, self.lease_sec)

    def release(self) -> None:
        if self.win_held:
            try:
                release_claim(self.layout.win_parent, self.owner_token)
            except LockError:
                pass
            self.win_held = False
        while self.held:
            held = self.held.pop()
            meta_path = Layout.meta_for(held.path)
            try:
                meta = read_meta(meta_path) if meta_path.exists() else None
                if meta and meta.get("owner_token") == self.owner_token:
                    meta_path.unlink()
            except (LockError, OSError):
                pass
            try:
                fcntl.flock(held.fd, fcntl.LOCK_UN)
            except OSError:
                pass
            if held.close_fd:
                try:
                    os.close(held.fd)
                except OSError:
                    pass
        self.order = ()

    def release_meta_keep_flock(self) -> None:
        """Drop our meta but leave the inherited flock to the shell process."""
        if self.win_held:
            raise LockError("publish_held_on_compat_end")
        for held in list(self.held):
            meta_path = Layout.meta_for(held.path)
            meta = read_meta(meta_path) if meta_path.exists() else None
            if not meta or meta.get("owner_token") != self.owner_token:
                raise LockError("fence_lost", kind=held.kind)
            meta_path.unlink()
            held.close_fd = False
        self.held.clear()
        self.order = ()

    def _acquire_library(self, wait_sec, resource_cas, cas_file, cmd) -> None:
        fd = self._flock_wait("library", self.layout.library_lock, wait_sec)
        try:
            self._claim_flock_meta(
                "library",
                fd,
                self.layout.library_lock,
                self.layout.library_fence(),
                resource_cas,
                cas_file,
                cmd,
                close_fd=True,
            )
        except Exception:
            self._unlock_quiet(fd, close_fd=True)
            raise

    def _acquire_publish(self, wait_sec, resource_cas, cas_file, cmd) -> None:
        fd = self._flock_wait("publish", self.layout.publish_lock, wait_sec)
        self.held.append(Held("publish", fd, self.layout.publish_lock, True))
        try:
            meta = acquire_claim(
                self.layout.win_parent,
                token=self.owner_token,
                lease_sec=self.lease_sec,
                hostname=self.hostname,
                pid=self.holder_pid,
                who=self.who,
                cmd=cmd,
                resource_cas=resource_cas,
                cas_file=cas_file,
                wait_sec=wait_sec,
                remote_skew_sec=self.remote_skew_sec,
            )
            self.win_held = True
            self.fences["publish"] = int(meta["fence"])
            self._recheck_cas(resource_cas, cas_file)
            st = os.fstat(fd)
            self._write_side_meta("publish", self.layout.publish_lock, st, cmd, meta["fence"], resource_cas)
        except Exception:
            raise

    def _flock_wait(self, kind: str, path: Path, wait_sec: float) -> int:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
        os.set_inheritable(fd, True)
        deadline = time.monotonic() + max(0.0, wait_sec)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise LockError(f"busy:{kind}", exit_code=75, path=str(path))
                time.sleep(0.02)

    def _claim_flock_meta(
        self,
        kind: str,
        fd: int,
        path: Path,
        fence_file: Path,
        resource_cas,
        cas_file,
        cmd: str,
        close_fd: bool,
    ) -> None:
        meta_path = Layout.meta_for(path)
        existing = read_meta(meta_path) if meta_path.exists() else None
        if existing is not None:
            if not lease_expired(existing, self.hostname, self.remote_skew_sec):
                raise LockError(
                    "crash_lease_fail_closed",
                    kind=kind,
                    holder_pid=existing.get("pid"),
                    holder_host=existing.get("host"),
                    lease_until=existing.get("lease_until"),
                )
            ensure_cas(resource_cas, cas_file)
        self._recheck_cas(resource_cas, cas_file)
        fence = bump_fence(fence_file)
        st = os.fstat(fd)
        self.fences[kind] = fence
        self._write_side_meta(kind, path, st, cmd, fence, resource_cas)
        self.held.append(Held(kind, fd, path, close_fd))

    def _recheck_cas(self, resource_cas, cas_file) -> None:
        if resource_cas or cas_file:
            if bool(resource_cas) != bool(cas_file):
                raise LockError("cas_incomplete")
            path = Path(cas_file)
            if not path.is_file():
                raise LockError("cas_mismatch", reason="missing_resource", path=str(path), expected=resource_cas)
            got = sha256_file(path)
            if got != resource_cas:
                raise LockError("cas_mismatch", expected=resource_cas, got=got, path=str(path))

    def _write_side_meta(self, kind, path: Path, st, cmd: str, fence: int, resource_cas) -> None:
        now = time.time()
        meta = {
            "kind": kind,
            "pid": self.holder_pid,
            "host": self.hostname,
            "owner_token": self.owner_token,
            "fence": int(fence),
            "since_epoch": now,
            "lease_until": now + self.lease_sec,
            "lease_sec": self.lease_sec,
            "who": self.who,
            "cmd": (cmd or "")[:500],
            "resource_cas": resource_cas,
            "contract_version": 1,
            "dev": int(st.st_dev),
            "ino": int(st.st_ino),
            "backend": "linux_flock",
        }
        write_json_atomic(Layout.meta_for(path), meta)
        if kind == "library":
            Layout.owner_for(path).write_text(
                f"unified_writer pid={self.holder_pid} token={self.owner_token} fence={fence} "
                f"since={time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(now))} cmd={cmd[:200]}\n",
                encoding="utf-8",
            )

    @staticmethod
    def _unlock_quiet(fd: int, close_fd: bool) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        if close_fd:
            try:
                os.close(fd)
            except OSError:
                pass
