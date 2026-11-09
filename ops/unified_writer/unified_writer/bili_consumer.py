"""Commit one bili_hora_audio_queue_v1 item under the library writer lock.

The Bili pipeline does not write the library and does not delete video.
This consumer re-hashes and re-probes the staged file, then CAS-writes the
evidenced youtube.json playlist and a local copy. A receipt is written only
after that commit is durable. If the queue says publish or build is required
and that step fails or has no runner, there is no receipt. Video files are
never unlinked. Nothing is sent to 115.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

from unified_writer.prod_guard import guard_library_mutation, release_if_owned
from unified_writer.util import sha256_file

QUEUE_SCHEMA = "bili_hora_audio_queue_v1"
RECEIPT_SCHEMA = "bili_hora_audio_receipt_v1"
WRITER_NAME = "harmonia-music-writer"
LOGICAL_PREFIX = "/115/音乐库/"
DURATION_TOLERANCE = 0.05
_REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_WEAK_REVISIONS = {"ok", "true", "false", "pending", "success", "done", "1", "yes"}
_IDENTITY_RE = re.compile(r"^bili:2221540698:BV[0-9A-Za-z]{10}$")
_SONG_KEYS = (
    "id", "title", "artist", "channel", "duration", "acodec", "abr", "ext",
    "format_id", "file", "file_m4a", "abr_m4a", "format_id_m4a", "size",
    "src", "source", "thumb", "yt_url", "done_at", "storage", "verify",
    "local_path", "sha256",
)


class ConsumerError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def receipt_name(identity: str) -> str:
    return identity.replace(":", "_") + ".json"


def validate_queue(item: dict) -> None:
    if not isinstance(item, dict) or item.get("schema") != QUEUE_SCHEMA:
        raise ConsumerError("queue_schema")
    if item.get("state") != "pending_commit":
        raise ConsumerError("queue_state")
    if not _IDENTITY_RE.fullmatch(str(item.get("identity") or "")):
        raise ConsumerError("identity")
    if item.get("media_id") != 2221540698:
        raise ConsumerError("media_id")
    if item.get("bvid") != item["identity"].rsplit(":", 1)[-1]:
        raise ConsumerError("bvid")
    audio = item.get("audio") or {}
    for key in ("staged_path", "sha256", "size", "duration_sec", "extract_mode", "logical_dest"):
        if key not in audio:
            raise ConsumerError("audio_field")
    if audio.get("extract_mode") != "copy":
        raise ConsumerError("extract_mode")
    if not re.fullmatch(r"[0-9a-f]{64}", str(audio.get("sha256") or "")):
        raise ConsumerError("sha256")
    if int(audio.get("size") or 0) < 1:
        raise ConsumerError("size")
    if float(audio.get("duration_sec") or 0) <= 0:
        raise ConsumerError("duration")
    logical = str(audio.get("logical_dest") or "")
    if not logical.startswith(LOGICAL_PREFIX) or ".." in logical.split("/"):
        raise ConsumerError("logical_dest")
    writer = item.get("writer") or {}
    if writer.get("required") != WRITER_NAME:
        raise ConsumerError("writer")
    if not str(item.get("queue_id") or "").startswith(item["identity"] + ":"):
        raise ConsumerError("queue_id")


def fresh_stat(path: Path, probe) -> dict:
    if not path.is_file() or path.is_symlink():
        raise ConsumerError("staged_missing")
    duration = float(probe(path))
    return {"size": int(path.stat().st_size), "sha256": sha256_file(path), "duration_sec": duration}


def _matches(queue_audio: dict, stat: dict) -> None:
    if stat["sha256"] != queue_audio["sha256"]:
        raise ConsumerError("sha256_mismatch")
    if int(stat["size"]) != int(queue_audio["size"]):
        raise ConsumerError("size_mismatch")
    if abs(float(stat["duration_sec"]) - float(queue_audio["duration_sec"])) > DURATION_TOLERANCE:
        raise ConsumerError("duration_mismatch")


def _local_dest(local_root: Path, logical_dest: str) -> Path:
    rel = logical_dest[len(LOGICAL_PREFIX):]
    dest = (local_root / rel).resolve()
    root = local_root.resolve()
    if root != dest and root not in dest.parents:
        raise ConsumerError("dest_escape")
    return dest


def _atomic_json(path: Path, obj: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return sha256_file(path)


def _copy_audio(src: Path, dest: Path, expected_sha: str, size: int) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() or dest.is_symlink():
        if dest.is_symlink() or not dest.is_file() or dest.stat().st_size != size or sha256_file(dest) != expected_sha:
            raise ConsumerError("dest_conflict")
        return
    tmp = dest.with_name(dest.name + f".tmp.{os.getpid()}")
    shutil.copyfile(src, tmp)
    if tmp.stat().st_size != size or sha256_file(tmp) != expected_sha:
        tmp.unlink(missing_ok=True)
        raise ConsumerError("copy_mismatch")
    os.replace(tmp, dest)


def _song(item: dict, stat: dict, local_path: Path) -> dict:
    audio = item["audio"]
    ext = str(audio.get("container") or Path(str(audio["logical_dest"])).suffix.lstrip(".") or "m4a")
    record = {
        "id": item["bvid"],
        "title": item["title"] if isinstance(item.get("title"), str) and item.get("title") else item["bvid"],
        "artist": item["artist"] if isinstance(item.get("artist"), str) else "",
        "channel": item["channel"] if isinstance(item.get("channel"), str) else "",
        "duration": stat["duration_sec"],
        "acodec": audio.get("acodec"),
        "abr": audio.get("abr"),
        "ext": ext,
        "format_id": "video-extract",
        "file": audio["logical_dest"],
        "file_m4a": audio["logical_dest"] if ext == "m4a" else None,
        "abr_m4a": audio.get("abr") if ext == "m4a" else None,
        "format_id_m4a": None,
        "size": stat["size"],
        "src": "bilibili",
        "source": "bili-queue:" + item["queue_id"],
        "thumb": item["thumb"] if isinstance(item.get("thumb"), str) else "",
        "yt_url": f"https://www.bilibili.com/video/{item['bvid']}",
        "done_at": item["staged_at"] if isinstance(item.get("staged_at"), str) else "",
        "storage": "local",
        "verify": "sha256+duration",
        "local_path": str(local_path),
        "sha256": stat["sha256"],
    }
    return {key: record.get(key) for key in _SONG_KEYS}


def _playlist(path: Path) -> tuple[dict, str | None]:
    if not path.exists():
        return {
            "id": "youtube",
            "name": "YouTube",
            "description": "从银色幕布 YouTube 收藏里点「收录八音盒」的歌，YouTube 能给的最高音质原样保存",
            "generated": "",
            "songs": [],
        }, None
    raw = path.read_bytes()
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("songs"), list):
        raise ConsumerError("playlist_schema")
    return data, hashlib.sha256(raw).hexdigest()


def _upsert(playlist: dict, song: dict) -> None:
    for index, existing in enumerate(playlist["songs"]):
        if existing.get("id") != song["id"]:
            continue
        if existing.get("sha256") not in (None, song["sha256"]):
            raise ConsumerError("playlist_conflict")
        playlist["songs"][index] = song
        return
    playlist["songs"].append(song)


def _already(playlist: dict, song: dict, dest: Path, stat: dict) -> bool:
    for existing in playlist.get("songs") or []:
        if existing.get("id") != song["id"] or existing.get("sha256") != stat["sha256"] or existing.get("file") != song["file"]:
            continue
        return dest.is_file() and not dest.is_symlink() and sha256_file(dest) == stat["sha256"] and dest.stat().st_size == stat["size"]
    return False


_PAGE_ID = re.compile(r"^(?:[A-Za-z0-9_-]{11}|BV[0-9A-Za-z]{10})$")


def _playback(item: dict, song: dict, dest: Path, stat: dict) -> dict:
    """Prove the page's path, not that the public box can already play.

    index.html ytApply keeps a row only when the id matches and file or
    file_m4a is set, then resolvePlayUrl sends that path to PLAY_RESOLVE.
    The list is /_ytlist/youtube.json, not library.json. The resolve service
    source is not in this repository, so this does not claim a live play.
    """
    alist = str(song.get("file") or "")
    local_ok = (
        dest.is_file()
        and not dest.is_symlink()
        and dest.stat().st_size == stat["size"]
        and sha256_file(dest) == stat["sha256"]
        and alist == item["audio"]["logical_dest"]
        and _PAGE_ID.fullmatch(str(song.get("id") or "")) is not None
    )
    if not local_ok or not (song.get("file") or song.get("file_m4a")):
        raise ConsumerError("playback_path")
    writer = item.get("writer") or {}
    callbacks = bool(writer.get("direct_publish")) or bool(writer.get("direct_build"))
    return {
        "library_json_written": False,
        "playlist_role": "_ytlist",
        "commit_scope": "local_playlist_plus_callback" if callbacks else "local_playlist",
        "box_playable": False,
        "playable_claim": "refused",
        "playback": {
            "page_keeps_row": True,
            "alist_path": alist,
            "play_resolve_query": "path=" + alist,
            "local_path": str(dest),
            "local_matches": True,
            "served_ytlist_url": "/_ytlist/youtube.json",
            "resolve_checked": False,
            "resolve_server_source": "not in this repository",
        },
        "video_delete": {
            "performed": False,
            "local_audio_deleted": False,
            "pipeline_may_delete_temp_video": True,
            "condition": (
                "the bili pipeline may delete only this job's temp video after it "
                "rechecks the staged audio against this receipt's sha256, size, and duration. "
                "A local playlist commit is not a claim that the public site can play the song."
            ),
        },
    }


def _receipt(item: dict, stat: dict, revision: str, song: dict, dest: Path) -> dict:
    if not _REVISION_RE.fullmatch(revision) or revision.lower() in _WEAK_REVISIONS:
        raise ConsumerError("revision")
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "queue_id": item["queue_id"],
        "identity": item["identity"],
        "bvid": item["bvid"],
        "revision": revision,
        "verified": True,
        "durable": True,
        "storage": "local",
        "writer": WRITER_NAME,
        "audio": {
            "sha256": stat["sha256"],
            "size": stat["size"],
            "duration_sec": stat["duration_sec"],
            "logical_dest": item["audio"]["logical_dest"],
        },
    }
    receipt.update(_playback(item, song, dest, stat))
    return receipt


def _write_receipt(receipt_root: Path, receipt: dict) -> Path:
    path = receipt_root / receipt_name(receipt["identity"])
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        current = json.loads(path.read_text(encoding="utf-8"))
        if current == receipt:
            return path
        raise ConsumerError("receipt_conflict")
    _atomic_json(path, receipt)
    return path


def commit_queue_item(
    item: dict,
    *,
    playlist_path: Path,
    local_root: Path,
    receipt_root: Path,
    probe,
    publisher=None,
    build_runner=None,
    expected_playlist_sha: str | None = None,
) -> dict:
    validate_queue(item)
    staged = Path(item["audio"]["staged_path"])
    stat = fresh_stat(staged, probe)
    _matches(item["audio"], stat)
    writer = item.get("writer") or {}
    if bool(writer.get("direct_publish")) and publisher is None:
        raise ConsumerError("publisher_missing")
    if bool(writer.get("direct_build")) and build_runner is None:
        raise ConsumerError("builder_missing")
    dest = _local_dest(local_root, item["audio"]["logical_dest"])
    song = _song(item, stat, dest)
    guard_library_mutation("library_compat", None)
    try:
        playlist, preimage = _playlist(playlist_path)
        if expected_playlist_sha is not None and preimage != expected_playlist_sha:
            raise ConsumerError("cas_mismatch")
        if preimage is not None and sha256_file(playlist_path) != preimage:
            raise ConsumerError("cas_mismatch")
        if not _already(playlist, song, dest, stat):
            _copy_audio(staged, dest, stat["sha256"], stat["size"])
            _upsert(playlist, song)
            revision = _atomic_json(playlist_path, playlist)
            if os.environ.get("BILI_CONSUMER_CRASH_AFTER") == "commit":
                raise SystemExit(91)
        else:
            revision = sha256_file(playlist_path)
        if bool(writer.get("direct_build")) and build_runner(item) is not True:
            raise ConsumerError("build_failed")
        if bool(writer.get("direct_publish")) and publisher(item) is not True:
            raise ConsumerError("publish_failed")
        receipt = _receipt(item, stat, revision, song, dest)
        path = _write_receipt(receipt_root, receipt)
        return {"ok": True, "receipt_path": str(path), "receipt": receipt, "video_deleted": False}
    finally:
        release_if_owned()


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="bili-audio-commit")
    parser.add_argument("--queue", required=True)
    parser.add_argument("--playlist", required=True)
    parser.add_argument("--local-root", required=True)
    parser.add_argument("--receipt-root", required=True)
    args = parser.parse_args(argv)
    item = json.loads(Path(args.queue).read_text(encoding="utf-8"))

    def probe(path: Path) -> float:
        import subprocess
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, check=False,
        )
        if proc.returncode != 0:
            raise ConsumerError("probe_failed")
        return float((json.loads(proc.stdout or "{}").get("format") or {}).get("duration") or 0)

    try:
        result = commit_queue_item(
            item,
            playlist_path=Path(args.playlist),
            local_root=Path(args.local_root),
            receipt_root=Path(args.receipt_root),
            probe=probe,
        )
    except ConsumerError as exc:
        sys.stderr.write(exc.code + "\n")
        return 76
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 76
    sys.stdout.write(json.dumps({
        "ok": True,
        "video_deleted": False,
        "box_playable": False,
        "commit_scope": result["receipt"].get("commit_scope"),
        "receipt": result["receipt_path"],
    }) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
