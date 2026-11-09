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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="winpub_lock")
    parser.add_argument("action", choices=("hold", "release"))
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
