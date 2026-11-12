"""Receipt gate for the hora local play path.

Two resolver copies were supplied and their hashes differ. The file that
implements the path in use is evidence/resolve_server.py
(sha256 8a064132c39e13e1009978a39990fca4a27996ba43ff0aef1c0ca2348b130380):
a logical /115/音乐库 path is played from D:\\Harmonia\\音乐库 only when the
ledger says copied or same_skip and the file size matches, and the URL is
/local-audio/... . Otherwise that server falls through to /115proxy.

evidence/resolve_server.src_opt.py
(sha256 d4eee4d8cce3078e23d8779fb0084e9140fc731a7337a1426ecc82be6c93ad7f)
has no ledger and no /local-audio. It is not the local ingest path.

A receipt is the signal that lets the Bili pipeline delete the temp video.
This gate returns ok only when the served _ytlist file contains the row and
the ledger plus the local file would make that resolver return source=local.
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
from pathlib import Path

ACTIVE_RESOLVE_SHA256 = "8a064132c39e13e1009978a39990fca4a27996ba43ff0aef1c0ca2348b130380"
SRC_OPT_RESOLVE_SHA256 = "d4eee4d8cce3078e23d8779fb0084e9140fc731a7337a1426ecc82be6c93ad7f"
LOGIC_ROOT = "/115/音乐库"
LEDGER_OK = ("copied", "same_skip")


def _sha1(path: Path) -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def latest_ledger(ledger_path: Path, logical: str) -> dict | None:
    if not ledger_path.is_file():
        return None
    hit = None
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("src") == logical:
            hit = row
    return hit


def ledger_row(logical: str, dest: Path, size: int, sha1_hex: str, status: str) -> dict:
    return {
        "src": logical,
        "dst": str(dest),
        "size": int(size),
        "sha1_dst": sha1_hex,
        "status": status,
        "verify": "sha1_dst_only",
        "via": "bili_local",
    }


def append_ledger(ledger_path: Path, logical: str, dest: Path, size: int) -> dict:
    """Append the place_local record shape if the latest row is not already this file."""
    sha1_hex = _sha1(dest)
    current = latest_ledger(ledger_path, logical)
    if current and current.get("status") in LEDGER_OK and current.get("dst") == str(dest) and int(current.get("size") or 0) == int(size) and str(current.get("sha1_dst") or "").upper() == sha1_hex:
        return current
    status = "same_skip" if current and current.get("status") in LEDGER_OK else "copied"
    row = ledger_row(logical, dest, size, sha1_hex, status)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return row


def _under_root(dest: Path, local_root: Path) -> bool:
    root = local_root.resolve()
    full = dest.resolve()
    return full == root or root in full.parents


def _served_row(served: Path, bvid: str, logical: str) -> bool:
    if not served.is_file():
        return False
    data = json.loads(served.read_text(encoding="utf-8"))
    for song in data.get("songs") or []:
        if song.get("id") == bvid and song.get("file") == logical and (song.get("file") or song.get("file_m4a")):
            return True
    return False


def local_play_url(logical: str) -> str:
    """Same shape as the active resolver: {origin}/local-audio/{quoted rel}."""
    rel = logical[len(LOGIC_ROOT):].lstrip("/")
    parts = rel.split("/")
    quoted = "/".join(urllib.parse.quote(part, safe="") for part in parts)
    return "/local-audio/" + quoted


def assess(
    *,
    logical: str,
    bvid: str,
    local_root: Path,
    ledger_path: Path | None,
    served_playlist: Path | None,
    size: int,
) -> dict:
    """Fail closed. ok is true only for a served row plus a ledger-backed local file."""
    reasons = []
    if served_playlist is None or not _served_row(Path(served_playlist), bvid, logical):
        reasons.append("served_playlist")
    entry = latest_ledger(Path(ledger_path), logical) if ledger_path else None
    dest = None
    if entry is None or entry.get("status") not in LEDGER_OK:
        reasons.append("ledger")
    else:
        raw = str(entry.get("dst") or "")
        dest = Path(raw) if raw else None
        try:
            led_size = int(entry.get("size"))
        except (TypeError, ValueError):
            led_size = -1
        sha_ok = bool(dest) and dest.is_file() and not dest.is_symlink() and _under_root(dest, local_root)
        if not sha_ok or led_size != int(size) or dest.stat().st_size != int(size):
            reasons.append("local_file")
        elif str(entry.get("sha1_dst") or "").upper() != _sha1(dest):
            reasons.append("ledger_sha1")
        elif entry.get("verify") not in ("sha1_dst_only", "sha1_src+dst"):
            reasons.append("ledger_verify")
    if reasons:
        return {
            "ok": False,
            "box_playable": False,
            "commit_scope": "local_playlist",
            "reasons": reasons,
            "source": None,
            "resolver_sha256": ACTIVE_RESOLVE_SHA256,
        }
    return {
        "ok": True,
        "box_playable": True,
        "commit_scope": "served_playlist_verified",
        "reasons": [],
        "source": "local",
        "play_path": local_play_url(logical),
        "local_path": str(dest),
        "resolver_sha256": ACTIVE_RESOLVE_SHA256,
        "rejected_resolver_sha256": SRC_OPT_RESOLVE_SHA256,
    }
