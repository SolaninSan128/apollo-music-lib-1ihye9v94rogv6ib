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

# Sources now in the handoff are patched by hash. These are the live gaps.
MISSING_FILES = (
    {
        "id": "make_deploy_variants.py",
        "expected_path": "work/make_deploy_variants.py",
        "why": "not inspected; pass check --root. A GUARD_v3 comment without the guard calls is not an install",
    },
    {
        "id": "poll_ratings.py",
        "expected_path": "work/poll_ratings.py",
        "why": "not inspected; pass check --root. A GUARD_v3 comment without the guard calls is not an install",
    },
    {
        "id": "ne_avail.py",
        "expected_path": "work/ne_avail.py",
        "why": "not inspected; pass check --root. stdout-only is the verified hash, not a missing-file guess",
    },
    {
        "id": "lomax_routines_not_updated",
        "expected_path": "",
        "why": "Lomax reports the four safety sections saved at work/ops/LOMAX_CONTRACT.md; signature verification is underway and is not acceptance",
    },
    {
        "id": "windows_claim_not_proven_on_hora",
        "expected_path": "C:/harmonia/cand/unified_writer",
        "why": "mutex was previously verified on the cand path; claim lifecycle is not accepted and check does not publish",
    },
)

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
    """Filename lists are not the lock. Date-specific scripts stay callable."""
    del argv
    return None
