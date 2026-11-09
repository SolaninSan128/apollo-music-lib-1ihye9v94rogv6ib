"""Stable command line for the four Lomax routines and the publish boundary.

``run`` is the only supported way to start a guarded writer. It refuses known
legacy program names so a bare apply/build/publish/increment command cannot
borrow this lock by accident. ``with_library_lock.sh`` remains flock-compatible
for commands that already take the legacy lock; it is not a bypass of publish
or CAS rules.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

from unified_writer.entries import ENTRIES, MISSING_FILES, normalize_kinds
from unified_writer.locklib import Layout, WriterSession, drop_inherited_meta
from unified_writer.util import LockError, dump_env_file, ensure_cas


def emit(obj: dict) -> None:
    sys.stderr.write("HARMONIA_STATUS\t" + json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stderr.flush()


def _session(args, *, holder_pid: int | None = None) -> WriterSession:
    if not getattr(args, "root", ""):
        raise LockError("root_required")
    layout = Layout(
        Path(args.root),
        library_lock=Path(args.library_lock) if getattr(args, "library_lock", "") else None,
        win_parent=Path(args.win_parent) if getattr(args, "win_parent", "") else None,
    )
    return WriterSession(
        layout,
        lease_sec=args.lease,
        remote_skew_sec=args.skew,
        who=args.who,
        holder_pid=holder_pid,
    )


def _cas(args) -> tuple[str | None, str | None]:
    cas_hash = getattr(args, "cas_hash", "") or ""
    cas_file = getattr(args, "cas_file", "") or ""
    if not cas_hash and not cas_file:
        return None, None
    return cas_hash or None, cas_file or None


def cmd_plan(args) -> int:
    spec = ENTRIES[args.entry]
    real = os.environ.get(spec["real_env"], "") if spec["real_env"] else ""
    payload = {
        "ok": True,
        "entry": args.entry,
        "locks_in_order": list(normalize_kinds(spec["locks"])),
        "requires_cas": spec["requires_cas"],
        "lomax_routine": spec["lomax_routine"],
        "real_env": spec["real_env"],
        "real_present": bool(real) and Path(real).is_file(),
        "will_execute": False,
        "formal_switch_blocked": True,
        "ok_to_claim_global": False,
        "production_installed": False,
        "note": spec["note"],
        "contract_version": 1,
    }
    emit(payload)
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return 0


def cmd_entries(_args) -> int:
    payload = {
        name: {
            "locks_in_order": list(normalize_kinds(spec["locks"])),
            "requires_cas": spec["requires_cas"],
            "lomax_routine": spec["lomax_routine"],
            "real_env": spec["real_env"],
            "note": spec["note"],
        }
        for name, spec in ENTRIES.items()
    }
    emit({"ok": True, "entries": list(payload)})
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return 0


def _prepare_run(args) -> None:
    spec = ENTRIES[args.entry]
    if args.entry == "library_compat":
        raise LockError("use_with_library_lock_sh", entry=args.entry)
    if spec["requires_cas"] and not args.cas_hash:
        raise LockError("cas_required", entry=args.entry)
    cas_hash, cas_file = _cas(args)
    if cas_hash or cas_file:
        ensure_cas(cas_hash, cas_file)
    # Date-specific ingest_YYYY_MM_DD.py / daily_artist_YYYYMMDD.py are generated
    # at runtime and import queue functions. A filename is not the lock.


def _park_contract_fds(env: dict) -> list[int]:
    """Move lock fds above the range bash uses for redirects."""
    pairs = [("HARMONIA_LIBRARY_LOCK_FD", 19)]
    if env.get("HARMONIA_PUBLISH_LOCK_FD"):
        pairs.append(("HARMONIA_PUBLISH_LOCK_FD", 20))
    parked = []
    for key, dest in pairs:
        src = int(env[key])
        if src != dest:
            os.dup2(src, dest)
        os.set_inheritable(dest, True)
        env[key] = str(dest)
        parked.append(dest)
    return parked


def _close_parked(session, parked: list[int]) -> None:
    originals = {held.fd for held in session.held}
    for fd in parked:
        if fd not in originals:
            try:
                os.close(fd)
            except OSError:
                pass


def cmd_run(args) -> int:
    _prepare_run(args)
    if not args.plan_only and not args.cmd:
        raise LockError("missing_cmd", entry=args.entry)
    session = _session(args)
    cas_hash, cas_file = _cas(args)
    acquired = session.acquire(
        ENTRIES[args.entry]["locks"],
        wait_sec=args.wait,
        resource_cas=cas_hash,
        cas_file=cas_file,
        cmd=" ".join(args.cmd),
    )
    try:
        env = os.environ.copy()
        env.update(session.contract_env(args.entry))
        payload = {"ok": True, "mode": "plan_only" if args.plan_only else "run", "acq": acquired}
        if args.status_file:
            Path(args.status_file).write_text(json.dumps(acquired), encoding="utf-8")
        if args.plan_only:
            emit(payload)
            sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
            return 0
        parked = _park_contract_fds(env)
        try:
            proc = subprocess.run(list(args.cmd), env=env, pass_fds=tuple(parked), check=False)
        finally:
            _close_parked(session, parked)
        payload["returncode"] = proc.returncode
        emit(payload)
        return proc.returncode
    finally:
        session.release()


def cmd_hold(args) -> int:
    if args.entry == "library_compat":
        raise LockError("use_with_library_lock_sh")
    spec = ENTRIES[args.entry]
    if spec["requires_cas"] and not args.cas_hash:
        raise LockError("cas_required", entry=args.entry)
    cas_hash, cas_file = _cas(args)
    if cas_hash or cas_file:
        ensure_cas(cas_hash, cas_file)
    session = _session(args)
    acquired = session.acquire(
        spec["locks"],
        wait_sec=args.wait,
        resource_cas=cas_hash,
        cas_file=cas_file,
        cmd=f"hold {args.entry}",
    )
    try:
        if args.stamp:
            Path(args.stamp).write_text(session.owner_token + "\n", encoding="utf-8")
        if args.status_file:
            Path(args.status_file).write_text(json.dumps(acquired), encoding="utf-8")
        deadline = time.time() + max(0.0, args.seconds)
        while time.time() < deadline:
            session.heartbeat()
            time.sleep(0.05)
        emit({"ok": True, "mode": "hold", "acq": acquired})
        return 0
    finally:
        session.release()


def cmd_crash_orphan(args) -> int:
    """Acquire, record a dirty epoch, then die without releasing it."""
    spec = ENTRIES[args.entry]
    cas_hash, cas_file = _cas(args)
    if spec["requires_cas"] and not cas_hash:
        raise LockError("cas_required", entry=args.entry)
    if cas_hash or cas_file:
        ensure_cas(cas_hash, cas_file)
    session = _session(args)
    try:
        session.acquire(
            spec["locks"],
            wait_sec=args.wait,
            resource_cas=cas_hash,
            cas_file=cas_file,
            cmd=f"crash-orphan {args.entry}",
        )
        if args.stamp:
            Path(args.stamp).write_text("orphan\n", encoding="utf-8")
    except Exception:
        session.release()
        raise
    os._exit(99)


def cmd_stop_write_check(args) -> int:
    import fcntl

    library = Path(args.library_lock) if args.library_lock else None
    probe = {"free": True, "absent": True, "created": False}
    if library is not None:
        if not library.exists():
            probe = {"free": True, "absent": True, "created": False}
        else:
            fd = os.open(library, os.O_RDWR)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    probe = {"free": True, "absent": False, "created": False}
                except BlockingIOError:
                    probe = {"free": False, "absent": False, "created": False}
            finally:
                os.close(fd)
    risks = []
    if library is not None:
        owner = Path(str(library) + ".owner")
        if owner.exists():
            risks.append({
                "id": "stale_owner_file",
                "block_switch": False,
                "note": "owner text is not the lock; kernel flock is",
                "detail": owner.read_text(encoding="utf-8", errors="replace")[:200],
            })
    for item in MISSING_FILES:
        risks.append({
            "id": "missing:" + item["id"],
            "block_switch": True,
            "expected_path": item["expected_path"],
            "why": item["why"],
        })
    risks.append({
        "id": "win_helper_not_on_windows_host",
        "block_switch": True,
        "why": "mkdir claim was not installed on the Windows publish host in this run",
    })
    risks.append({
        "id": "bare_legacy_writers_still_possible",
        "block_switch": True,
        "why": "unwrapped apply_ratings/build/publish/download can still write if invoked outside this guard",
    })
    mode = None
    if args.mode_file:
        mode_path = Path(args.mode_file)
        if mode_path.is_file():
            mode = json.loads(mode_path.read_text(encoding="utf-8"))
        else:
            mode = {"absent": True, "path": str(mode_path)}
    payload = {
        "ok": True,
        "meaning": "exit_0_means_switch_still_blocked_not_production_safe",
        "formal_switch_blocked": True,
        "ok_to_claim_global": False,
        "production_installed": False,
        "deployed_this_run": False,
        "library_lock": probe,
        "mode": mode,
        "mode_mutated": False,
        "missing": list(MISSING_FILES),
        "risks": risks,
        "contract_version": 2,
        "chain_interface": 2,
        "ready_for_install": True,
        "ready_for_live": False,
        "lock_order": ["library", "publish"],
        "schedules_preserved": {
            "daily_06": "CRON_TZ=Asia/Shanghai 0 6 * * *",
            "ratings": "CRON_TZ=Asia/Shanghai 0 0 * * * and 0 12 * * *",
            "gapfill_03": "CRON_TZ=Asia/Shanghai 0 3 * * *",
            "resume_07": "CRON_TZ=Asia/Shanghai 0 7 * * *",
        },
    }
    emit(payload)
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    if payload["formal_switch_blocked"] and not payload["ok_to_claim_global"]:
        return 0
    return 76


def cmd_commit_cas(args) -> int:
    from unified_writer.contract import commit_cas
    data = Path(args.data_file).read_bytes()
    result = commit_cas(Path(args.path), data, args.expect)
    emit(result)
    sys.stdout.write(json.dumps(result) + "\n")
    return 0


def cmd_compat_begin(args) -> int:
    session = _session(args, holder_pid=os.getppid())
    cas_hash, cas_file = _cas(args)
    if cas_hash or cas_file:
        ensure_cas(cas_hash, cas_file)
    try:
        session.adopt_library_fd(
            args.fd,
            resource_cas=cas_hash,
            cas_file=cas_file,
            cmd=args.cmd_text,
        )
        dump_env_file(Path(args.env_out), session.contract_env(args.entry))
    except Exception:
        session.release()
        raise
    # The shell still holds the flock inode. Drop our bookkeeping only.
    session.held.clear()
    session.win_held = False
    emit({"ok": True, "mode": "compat-begin", "fence": session.fences.get("library")})
    return 0


def cmd_compat_end(args) -> int:
    drop_inherited_meta(args.fd, Path(args.lock), args.token)
    emit({"ok": True, "mode": "compat-end"})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="unified_writer")
    sub = parser.add_subparsers(dest="action", required=True)

    def add_common(target, *, entry=False, cas=False):
        target.add_argument("--root", default="")
        target.add_argument("--library-lock", default="")
        target.add_argument("--win-parent", default="")
        target.add_argument("--lease", type=float, default=600)
        target.add_argument("--skew", type=float, default=30)
        target.add_argument("--wait", type=float, default=0.3)
        target.add_argument("--who", default="unified")
        if entry:
            target.add_argument("--entry", required=True, choices=list(ENTRIES))
        if cas:
            target.add_argument("--cas-file", default="")
            target.add_argument("--cas-hash", default="")
        target.add_argument("--stamp", default="")
        target.add_argument("--status-file", default="")

    plan = sub.add_parser("plan")
    plan.add_argument("--entry", required=True, choices=list(ENTRIES))
    plan.set_defaults(func=cmd_plan)

    entries = sub.add_parser("entries")
    entries.set_defaults(func=cmd_entries)

    run = sub.add_parser("run")
    add_common(run, entry=True, cas=True)
    run.add_argument("--plan-only", action="store_true")
    run.add_argument("cmd", nargs="*")
    run.set_defaults(func=cmd_run)

    hold = sub.add_parser("hold")
    add_common(hold, entry=True, cas=True)
    hold.add_argument("--seconds", type=float, default=1)
    hold.set_defaults(func=cmd_hold)

    crash = sub.add_parser("crash-orphan")
    add_common(crash, entry=True, cas=True)
    crash.set_defaults(func=cmd_crash_orphan)

    stop = sub.add_parser("stop-write-check")
    stop.add_argument("--library-lock", default="")
    stop.add_argument("--mode-file", default="")
    stop.set_defaults(func=cmd_stop_write_check)

    commit = sub.add_parser("commit-cas")
    commit.add_argument("--path", required=True)
    commit.add_argument("--expect", required=True)
    commit.add_argument("--data-file", required=True)
    commit.set_defaults(func=cmd_commit_cas)

    begin = sub.add_parser("compat-begin")
    add_common(begin, entry=True, cas=True)
    begin.add_argument("--fd", type=int, required=True)
    begin.add_argument("--lock", required=True)
    begin.add_argument("--env-out", required=True)
    begin.add_argument("--cmd", dest="cmd_text", default="")
    begin.set_defaults(func=cmd_compat_begin)

    end = sub.add_parser("compat-end")
    end.add_argument("--fd", type=int, required=True)
    end.add_argument("--lock", required=True)
    end.add_argument("--token", required=True)
    end.set_defaults(func=cmd_compat_end)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "library_lock", None) == "" and getattr(args, "lock", None):
        args.library_lock = args.lock
    try:
        return int(args.func(args))
    except LockError as exc:
        emit(exc.to_dict())
        sys.stdout.write(json.dumps(exc.to_dict(), ensure_ascii=False) + "\n")
        return exc.exit_code
    except Exception:
        traceback.print_exc()
        emit({"ok": False, "error": "internal", "exit_code": 76})
        return 76


if __name__ == "__main__":
    raise SystemExit(main())
