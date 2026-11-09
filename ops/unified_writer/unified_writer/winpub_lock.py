"""Command line for the Windows publication host.

Copy this file together with util.py, winclaim.py, and the package's __init__.py.
Do not import locklib here: that module needs Linux flock.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import sys
import time
from pathlib import Path

from unified_writer.util import LockError
from unified_writer.winclaim import acquire_claim, claim_dir, heartbeat_claim, read_meta, release_claim


def _emit(obj: dict) -> None:
    sys.stderr.write("HARMONIA_STATUS\t" + json.dumps(obj, ensure_ascii=False) + "\n")


def _refuse_publish(path: Path) -> None:
    if not path.is_dir():
        raise LockError("claim_dir_required", path=str(path))
    text = str(path).replace("\\", "/").lower()
    if "/harmonia/pub" in text or "/var/www/musicbox" in text or "/musicbox/_ytlist" in text:
        raise LockError("refuse_publish_path", path=str(path))
    if (path / "index.html").is_file():
        raise LockError("refuse_publish_path", path=str(path))


def _probe(parent: Path) -> int:
    """Hold the mkdir claim and prove a second taker exits 75. Does not publish."""
    import signal
    import subprocess
    _refuse_publish(parent)
    stamp = parent / ".probe-token"
    pkg_root = str(Path(__file__).resolve().parent.parent)
    child_env = os.environ.copy()
    child_env["PYTHONPATH"] = pkg_root + os.pathsep + child_env.get("PYTHONPATH", "")
    holder = subprocess.Popen(
        [sys.executable, "-m", "unified_writer.winpub_lock", "hold",
         "--dir", str(parent), "--seconds", "30", "--wait", "0", "--stamp", str(stamp)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=child_env,
    )
    conflict = False
    try:
        time.sleep(0.4)
        try:
            meta = acquire_claim(
                parent, token=secrets.token_hex(8), lease_sec=30, hostname=socket.gethostname(),
                pid=os.getpid(), who="probe", cmd="win-claim-probe", resource_cas=None, cas_file=None,
                wait_sec=0.2, remote_skew_sec=30,
            )
            release_claim(parent, meta["owner_token"])
        except LockError as exc:
            conflict = exc.exit_code == 75
            if not conflict:
                raise
    finally:
        if holder.poll() is None:
            holder.send_signal(signal.SIGTERM)
            try:
                holder.wait(timeout=2)
            except subprocess.TimeoutExpired:
                holder.kill()
                holder.wait(timeout=2)
        meta_path = claim_dir(parent) / "meta.json"
        token = ""
        if meta_path.is_file():
            try:
                token = json.loads(meta_path.read_text(encoding="utf-8")).get("owner_token") or ""
            except json.JSONDecodeError:
                token = ""
        if token:
            try:
                release_claim(parent, token)
            except LockError:
                pass
        stamp.unlink(missing_ok=True)
    if not conflict:
        _emit({"ok": False, "published": False, "error": "probe_acquired"})
        return 76
    sys.stdout.write(json.dumps({
        "ok": True, "published": False, "exit_code": 75, "mutex": "publish_conflict", "dir": str(parent),
    }) + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="winpub_lock")
    parser.add_argument("action", choices=("hold", "release", "probe"))
    parser.add_argument("--dir", required=True)
    parser.add_argument("--lease", type=float, default=600)
    parser.add_argument("--wait", type=float, default=0.2)
    parser.add_argument("--skew", type=float, default=30)
    parser.add_argument("--seconds", type=float, default=1)
    parser.add_argument("--token", default="")
    parser.add_argument("--cas-file", default="")
    parser.add_argument("--cas-hash", default="")
    parser.add_argument("--stamp", default="")
    parser.add_argument("--who", default="winpub")
    args = parser.parse_args(argv)
    token = args.token or secrets.token_hex(16)
    try:
        if args.action == "probe":
            return _probe(Path(args.dir))
        if args.action == "release":
            if not args.token:
                raise LockError("fence_lost", reason="token_required")
            release_claim(Path(args.dir), args.token)
            _emit({"ok": True, "released": True})
            return 0
        meta = acquire_claim(
            Path(args.dir),
            token=token,
            lease_sec=args.lease,
            hostname=socket.gethostname(),
            pid=os.getpid(),
            who=args.who,
            cmd="winpub_lock hold",
            resource_cas=args.cas_hash or None,
            cas_file=args.cas_file or None,
            wait_sec=args.wait,
            remote_skew_sec=args.skew,
        )
        if args.stamp:
            Path(args.stamp).write_text(token + "\n", encoding="utf-8")
        deadline = time.time() + max(0.0, args.seconds)
        while time.time() < deadline:
            heartbeat_claim(Path(args.dir), token, args.lease)
            time.sleep(0.05)
        release_claim(Path(args.dir), token)
        _emit({"ok": True, "fence": meta["fence"], "claim": str(claim_dir(Path(args.dir)))})
        return 0
    except LockError as exc:
        _emit(exc.to_dict())
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())


def claim_meta(parent: Path) -> dict | None:
    path = claim_dir(parent) / "meta.json"
    if not path.exists():
        return None
    return read_meta(path)
