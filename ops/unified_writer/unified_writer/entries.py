"""Write boundaries the guard knows about.

The real ratings/build/download/publish programs are not in this deploy repo.
Names here are the stable contract, not proof those programs are wrapped.
"""
from __future__ import annotations

# Fixed acquisition order. Publish always includes library so a publisher
# cannot hold the publish claim while a library writer waits, which deadlocks.
LOCK_ORDER = ("library", "publish")

ENTRIES = {
    "ratings": {
        "locks": ("library",),
        "requires_cas": False,
        "lomax_routine": True,
        "real_env": "HARMONIA_REAL_RATINGS",
        "expected": "work/apply_ratings.py",
        "note": "Ratings writeback. Download is not this entry. Bare apply_ratings.py --publish is rejected by `run`.",
    },
    "build_push": {
        "locks": ("library",),
        "requires_cas": False,
        "lomax_routine": True,
        "real_env": "HARMONIA_REAL_BUILD_PUSH",
        "expected": "music-library/build.py plus deploy git push",
        "note": "Build and Pages push. Bare build.py / git push of the deploy repo is rejected by `run`.",
    },
    "download_writeback": {
        "locks": ("library",),
        "requires_cas": True,
        "lomax_routine": True,
        "real_env": "HARMONIA_REAL_DOWNLOAD_WRITEBACK",
        "expected": "work/run_all_queues.py writeback step only",
        "note": "Download may stay outside the lock. Writing library.json requires this entry and a CAS hash.",
    },
    "increment": {
        "locks": ("library",),
        "requires_cas": True,
        "lomax_routine": True,
        "real_env": "HARMONIA_REAL_INCREMENT",
        "expected": "Spotify/HongKong apply script (not shipped)",
        "note": "Serial increment. CAS of the current library file is mandatory. This package does not apply Spotify or HongKong.",
    },
    "publish": {
        "locks": ("library", "publish"),
        "requires_cas": True,
        "lomax_routine": False,
        "real_env": "HARMONIA_REAL_PUBLISH",
        "expected": "cand/publish/harmonia_publish.py",
        "note": "Library lock then publish claim. --execute stays fail-closed until the Windows helper is configured.",
    },
    "library_compat": {
        "locks": ("library",),
        "requires_cas": False,
        "lomax_routine": False,
        "real_env": "",
        "expected": "",
        "note": "with_library_lock.sh compatibility. Same flock inode as the legacy script. Not a license to skip entry guards.",
    },
}

# Present in neither the Pages repo nor the sanitized handoff. Do not invent them.
MISSING_FILES = (
    {
        "id": "apply_ratings.py",
        "expected_path": "work/apply_ratings.py",
        "why": "ratings --publish writer; not in the deploy repo or the handoff",
    },
    {
        "id": "build.py",
        "expected_path": "music-library/build.py",
        "why": "Pages build; not shipped",
    },
    {
        "id": "run_all_queues.py",
        "expected_path": "work/run_all_queues.py",
        "why": "download writeback module; not shipped",
    },
    {
        "id": "harmonia_target.py",
        "expected_path": "cand/publish/harmonia_target.py",
        "why": "handoff stub has no is_hora, label, rpath, _ssh, _scp, HOPT, HHOST, or PY",
    },
    {
        "id": "pub_remote.py",
        "expected_path": "cand/publish/pub_remote.py",
        "why": "Windows publish executor referenced by harmonia_publish.py; not shipped",
    },
    {
        "id": "harmonia_ops.py",
        "expected_path": "work/ops/harmonia_ops.py",
        "why": "only the library_lock excerpt was shipped",
    },
    {
        "id": "lomax_routine_prompts",
        "expected_path": "",
        "why": "the four routine prompts were not in the handoff, so wrap coverage is unverified",
    },
)

LEGACY_BASENAMES = {
    "apply_ratings.py",
    "build.py",
    "run_all_queues.py",
    "harmonia_publish.py",
    "harmonia_ops.py",
    "pub_remote.py",
}


def normalize_kinds(kinds: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    unknown = [k for k in kinds if k not in LOCK_ORDER]
    if unknown:
        from unified_writer.util import LockError
        raise LockError("unknown_kinds", kinds=",".join(unknown))
    wanted = set(kinds)
    if "publish" in wanted:
        wanted.add("library")
    return tuple(k for k in LOCK_ORDER if k in wanted)


def find_legacy(argv: list[str]) -> str | None:
    from pathlib import Path
    for index, arg in enumerate(argv):
        name = Path(arg).name
        if name in LEGACY_BASENAMES:
            return name
        if name.startswith("ingest_") and name.endswith(".py"):
            return name
        lower = name.lower()
        if "spotify" in lower or "hongkong" in lower or "hong_kong" in lower:
            return name
        if arg == "-m" and index + 1 < len(argv) and argv[index + 1] in {"run_all_queues"}:
            return argv[index + 1]
    return None
