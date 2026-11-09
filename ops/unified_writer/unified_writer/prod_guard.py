"""Production mutation guard.

Dynamic date-specific scripts and the fixed entrypoints call these functions.
A filename is not a credential. The gate is the kernel flock on
work/.library.lock, plus the publish mkdir claim for the main-site index.
"""
from __future__ import annotations

import atexit
import json
import os
import sys
from pathlib import Path

from unified_writer.locklib import Layout, WriterSession
from unified_writer.util import LockError, sha256_file

GUARD = "HARMONIA_UNIFIED_GUARD_v1"
CHAIN_INTERFACE = 2
LIBRARY_ENTRIES = {
    "ratings",
    "build_push",
    "download_writeback",
    "increment",
    "library_compat",
    "daily_ingest",
}
CONTRACT_ENTRIES = {
    "ratings",
    "build_push",
    "download_writeback",
    "increment",
    "library_compat",
    "publish",
}
_HELD: dict | None = None
_ATEXIT = False

WHO_ENTRY = {
    "ratings": "ratings",
    "gapfill": "download_writeback",
    "dailycheck": "build_push",
}


def _die(exc: LockError) -> None:
    sys.stderr.write(GUARD + " " + json.dumps(exc.to_dict(), ensure_ascii=False) + "\n")
    raise SystemExit(exc.exit_code)


def _ensure_import_path() -> None:
    env_path = os.environ.get("HARMONIA_UNIFIED_WRITER") or ""
    if env_path and env_path not in sys.path:
        sys.path.insert(0, env_path)


def find_library_lock(resource_path: str | None = None) -> str:
    explicit = os.environ.get("HARMONIA_LIBRARY_LOCK") or ""
    if explicit:
        return explicit
    if resource_path and os.path.basename(resource_path) == "library.json":
        return os.path.join(os.path.dirname(os.path.abspath(resource_path)), "work", ".library.lock")
    cwd = os.getcwd()
    for candidate in (os.path.join(cwd, "work", ".library.lock"), os.path.join(cwd, ".library.lock")):
        if os.path.isdir(os.path.dirname(candidate)):
            return candidate
    raise LockError("library_lock_unconfigured")


def _lock_root(lock_path: str) -> str:
    explicit = os.environ.get("HARMONIA_LOCK_ROOT") or ""
    if explicit:
        return explicit
    return os.path.join(os.path.dirname(os.path.abspath(lock_path)), ".unified_lock")


def _arm_exit() -> None:
    global _HELD
    held = _HELD
    _HELD = None
    if not held or not held.get("owned"):
        return
    try:
        held["session"].release()
    except Exception:
        pass


def _remember(session: WriterSession, owned: bool) -> None:
    global _HELD, _ATEXIT
    _HELD = {"session": session, "owned": owned}
    if owned and not _ATEXIT:
        atexit.register(_arm_exit)
        _ATEXIT = True


def _contract_entry(requested: str) -> str:
    if requested in CONTRACT_ENTRIES:
        return requested
    return "library_compat"


def release_if_owned() -> None:
    """Drop a lock this process acquired. An inherited parent lock stays."""
    global _HELD
    held = _HELD
    if not held or not held.get("owned"):
        return
    _HELD = None
    try:
        held["session"].release()
    except Exception:
        pass
    end_held_flock()


def _assert_held_library(requested: str) -> None:
    from unified_writer.contract import assert_contract

    env_entry = os.environ.get("HARMONIA_WRITER_ENTRY") or ""
    if requested == "publish":
        assert_contract("publish")
        return
    if env_entry not in LIBRARY_ENTRIES or requested not in LIBRARY_ENTRIES:
        raise LockError("entry_mismatch", expected=requested, got=env_entry)
    assert_contract(env_entry)


def _check_cas(requested: str, resource_path: str | None) -> None:
    if requested not in ("download_writeback", "increment"):
        return
    expected = os.environ.get("HARMONIA_RESOURCE_CAS") or ""
    if not expected or not resource_path:
        raise LockError("cas_required", entry=requested)
    path = Path(resource_path)
    if not path.is_file():
        raise LockError("cas_mismatch", reason="missing_resource", path=str(path), expected=expected)
    got = sha256_file(path)
    if got != expected:
        raise LockError("cas_mismatch", expected=expected, got=got, path=str(path))


def snapshot_sha(path: str) -> str:
    """Hash a file before the unlocked read. Compared again after the lock."""
    return sha256_file(Path(path))


def require_unchanged(path: str, expected: str) -> None:
    """Fail closed if the file moved between the read and the locked write."""
    got = sha256_file(Path(path))
    if got != expected:
        _die(LockError("cas_mismatch", expected=expected, got=got, path=path))


UNPATCHED_TOOLS = {
    "poll_ratings.py": {"mutates": False, "proof": "source not in the script pack; treated as a read"},
    "ne_avail.py": {"mutates": False, "proof": "source not in the script pack; treated as a read"},
    "make_deploy_variants.py": {"mutates": True, "proof": "source not in the script pack; subprocess only under the library lock"},
}


def run_unpatched(name: str, argv: list, cwd: str | None = None) -> dict:
    """Run a tool whose source we do not have. Does not claim the tool itself checks the lock."""
    import subprocess
    spec = UNPATCHED_TOOLS.get(name)
    if spec is None:
        _die(LockError("unpatched_tool_unknown", tool=name))
    mode = "unchecked_read"
    if spec["mutates"]:
        mode = guard_library_mutation("build_push", None)
    proc = subprocess.run([str(part) for part in argv], cwd=cwd, check=False)
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "internally_guarded": False,
        "lock": mode,
        "proof": spec["proof"],
    }


def guard_library_mutation(entry: str, resource_path: str | None = None) -> str:
    """Hold or prove the library flock before a library/build/Pages mutation."""
    _ensure_import_path()
    try:
        if os.environ.get("HARMONIA_WRITER_CONTRACT") == "1":
            _assert_held_library(entry)
            _check_cas(entry, resource_path)
            return "inherited"
        lock = find_library_lock(resource_path)
        root = _lock_root(lock)
        cas = cas_file = None
        if entry in ("download_writeback", "increment") and resource_path and os.path.isfile(resource_path):
            cas = sha256_file(Path(resource_path))
            cas_file = resource_path
        session = WriterSession(
            Layout(root, library_lock=Path(lock)),
            lease_sec=float(os.environ.get("HARMONIA_LEASE_SEC", "600")),
            remote_skew_sec=float(os.environ.get("HARMONIA_REMOTE_SKEW_SEC", "30")),
            who=entry,
        )
        session.acquire(
            ("library",),
            wait_sec=float(os.environ.get("LOCK_WAIT", "30")),
            resource_cas=cas,
            cas_file=cas_file,
            cmd=entry,
        )
        os.environ.update(session.contract_env(_contract_entry(entry)))
        if cas:
            os.environ["HARMONIA_RESOURCE_CAS"] = cas
        _remember(session, True)
        return "acquired"
    except LockError as exc:
        _die(exc)
    raise AssertionError("unreachable")


def begin_held_flock(fd: int, lock_path: str, who: str, library_path: str | None = None) -> None:
    """Ops already holds flock on fd. Record the contract without opening the file again."""
    _ensure_import_path()
    try:
        os.set_inheritable(fd, True)
        entry = WHO_ENTRY.get(who, "library_compat")
        root = _lock_root(lock_path)
        cas = cas_file = None
        if entry == "download_writeback" and library_path and os.path.isfile(library_path):
            cas = sha256_file(Path(library_path))
            cas_file = library_path
        session = WriterSession(
            Layout(root, library_lock=Path(lock_path)),
            lease_sec=float(os.environ.get("HARMONIA_LEASE_SEC", "600")),
            remote_skew_sec=float(os.environ.get("HARMONIA_REMOTE_SKEW_SEC", "30")),
            who=who,
        )
        session.adopt_library_fd(fd, resource_cas=cas, cas_file=cas_file, cmd=who)
        os.environ.update(session.contract_env(entry))
        os.environ["HARMONIA_LIBRARY_LOCK_FD"] = str(fd)
        if cas:
            os.environ["HARMONIA_RESOURCE_CAS"] = cas
        _remember(session, False)
    except LockError as exc:
        _die(exc)


def end_held_flock() -> None:
    global _HELD
    held = _HELD
    _HELD = None
    if held:
        try:
            held["session"].release_meta_keep_flock()
        except Exception:
            pass
    for key in (
        "HARMONIA_WRITER_CONTRACT",
        "HARMONIA_CONTRACT_VERSION",
        "HARMONIA_WRITER_ENTRY",
        "HARMONIA_WRITER_TOKEN",
        "HARMONIA_WRITER_FENCE_LIBRARY",
        "HARMONIA_WRITER_FENCE_PUBLISH",
        "HARMONIA_WRITER_KINDS",
        "HARMONIA_LIBRARY_LOCK_FD",
        "HARMONIA_PUBLISH_LOCK_FD",
        "HARMONIA_LIBRARY_LOCK_PATH",
        "HARMONIA_PUBLISH_LOCK_PATH",
        "HARMONIA_WIN_PUBLISH_CLAIM",
        "HARMONIA_RESOURCE_CAS",
        "HARMONIA_LOCK_ORDER",
    ):
        os.environ.pop(key, None)


def subprocess_lock_kwargs() -> dict:
    """Pass the inherited lock fd into build.py / apply_ratings.py children."""
    if os.environ.get("HARMONIA_WRITER_CONTRACT") != "1":
        _die(LockError("legacy_bypass_no_contract"))
    fds = []
    env = os.environ.copy()
    for key in ("HARMONIA_LIBRARY_LOCK_FD", "HARMONIA_PUBLISH_LOCK_FD"):
        raw = env.get(key) or ""
        if not raw:
            continue
        fd = int(raw)
        os.set_inheritable(fd, True)
        fds.append(fd)
    if not fds:
        _die(LockError("legacy_bypass_lock_not_held"))
    return {"env": env, "pass_fds": tuple(fds)}


def ordinary_git_push(cwd: str, remote: str = "origin", branch: str = "main") -> None:
    """Pages push. Refuses --force and plus refspecs."""
    import subprocess
    argv = ["git", "push", remote, branch]
    _refuse_force(argv)
    subprocess.check_call(argv, cwd=cwd)


def _refuse_force(argv: list) -> None:
    bad = [arg for arg in argv if arg in ("--force", "-f", "--force-with-lease") or (isinstance(arg, str) and arg.startswith("+"))]
    if bad:
        _die(LockError("ordinary_push_only", args=",".join(bad)))


def collision_blocks(lock_path: str | None = None) -> dict:
    """Real lock state for the 07:00 resume. This does not look at mtime."""
    import fcntl
    path = lock_path or os.environ.get("HARMONIA_LIBRARY_LOCK") or ""
    result = {"blocked": False, "reason": "free", "heuristic_mtime": False, "guard": GUARD}
    if not path or not os.path.exists(path):
        result["reason"] = "lock_absent"
        return result
    fd = os.open(path, os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            result["blocked"] = True
            result["reason"] = "lock_held"
            return result
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
    meta_path = path + ".meta.json"
    if os.path.isfile(meta_path):
        try:
            meta = json.loads(Path(meta_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            result["blocked"] = True
            result["reason"] = "meta_corrupt_fail_closed"
            return result
        if time_unexpired(meta):
            result["blocked"] = True
            result["reason"] = "crash_lease_fail_closed"
            result["holder_pid"] = meta.get("pid")
    return result


def time_unexpired(meta: dict) -> bool:
    import time
    try:
        return time.time() < float(meta.get("lease_until") or 0)
    except (TypeError, ValueError):
        return True


def arm_site_publish(deploy_dir: str, index_sha: str, index_path: str | None = None) -> dict:
    """Library flock, then the publish claim, then PUBLISH_CLAIM.json.

    Ratings and the 03:00 gapfill must not call this. Main-site publish is separate.
    """
    _ensure_import_path()
    try:
        lock = find_library_lock(None)
        root = _lock_root(lock)
        win_parent = os.environ.get("HARMONIA_WIN_PARENT") or os.path.join(root, "winpub")
        index_path = index_path or os.path.join(deploy_dir, "index.html")
        session = WriterSession(
            Layout(root, library_lock=Path(lock), win_parent=Path(win_parent)),
            lease_sec=float(os.environ.get("HARMONIA_LEASE_SEC", "600")),
            remote_skew_sec=float(os.environ.get("HARMONIA_REMOTE_SKEW_SEC", "30")),
            who="publish",
        )
        session.acquire(
            ("library", "publish"),
            wait_sec=float(os.environ.get("LOCK_WAIT", "30")),
            resource_cas=index_sha,
            cas_file=index_path,
            cmd="publish",
        )
        claim_dir = str(Path(win_parent) / "publish.claim.d")
        live = json.loads((Path(claim_dir) / "meta.json").read_text(encoding="utf-8"))
        document = {
            "guard": GUARD,
            "contract_version": 1,
            "owner_token": live["owner_token"],
            "fence": live["fence"],
            "resource_cas": index_sha,
            "claim_dir": claim_dir,
            "lease_until": live["lease_until"],
        }
        Path(deploy_dir, "PUBLISH_CLAIM.json").write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.environ.update(session.contract_env("publish"))
        _remember(session, True)
        if os.environ.get("HARMONIA_PUBLISH_TRANSPORT", "ssh") != "local":
            import harmonia_target as target
            target.ensure_win_claim(os.path.join(deploy_dir, "PUBLISH_CLAIM.json"))
        return document
    except LockError as exc:
        _die(exc)
    raise AssertionError("unreachable")
