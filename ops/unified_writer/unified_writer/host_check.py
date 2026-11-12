"""Read the on-host scripts. A comment that only contains the guard token is not an install.

poll_ratings and make_deploy_variants count as installed when the patched call
lines are present. ne_avail counts only when its bytes are the verified
stdout-only file. Package revision must be 4. None of that clears the live switch.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

PACKAGE_REVISION = "4"
NE_STDOUT_SHA256 = "41ca830fe55815001898d3cad884c430333aa62882a35adf3a7b338477b1510b"

POLL_CALLS = (
    '_cas.guard_library_mutation("ratings", SNAP if _cas_os.path.isfile(SNAP) else None)',
    "_cas.require_unchanged(SNAP, _pg_sha)",
)
MAKE_CALLS = (
    '_cas.guard_library_mutation("build_push", os.path.join(ROOT, "index.html"))',
    '_cas.require_unchanged(os.path.join(ROOT, "index.html"), _pg_sha)',
)

EVIDENCE_KIND = "harmonia_install_evidence_v1"
EVIDENCE_CHECKS = {
    "poll_held_lock": {"exit_code": 75, "snapshot_unchanged": True},
    "make_held_lock": {"exit_code": 75, "output_dir_created": False},
    "fd_inherited": {"exit_code": 0, "inherited": True},
    "cas_mismatch": {"exit_code": 76, "cas_mismatch": True},
}

LIVE_BLOCKERS = (
    {
        "id": "saved_routine_acceptance",
        "block_switch": True,
        "why": "Lomax reports the four safety sections saved; independent signature verification is underway, so this is not verified proof and does not unlock",
    },
    {
        "id": "mode_acceptance",
        "block_switch": True,
        "why": "MODE remains shadow; this check does not edit MODE and does not treat shadow as live",
    },
    {
        "id": "claim_lifecycle_acceptance",
        "block_switch": True,
        "why": "the Windows mutex was previously verified; claim lifecycle acceptance is still open and this check does not publish",
    },
    {
        "id": "raw_writers",
        "block_switch": True,
        "why": "a generated script can still open library.json without commit_library_json",
    },
)


def routine_report(root: Path | None) -> dict:
    """Lomax's save report is not signature proof. The contract file is work/ops/LOMAX_CONTRACT.md."""
    present = False
    if root is not None:
        present = (Path(root) / "work" / "ops" / "LOMAX_CONTRACT.md").is_file()
    return {
        "contract_path": "work/ops/LOMAX_CONTRACT.md",
        "contract_file_present": present,
        "reported_saved": True,
        "reported_by": "Lomax",
        "reported_detail": (
            "all 4 get/update safety sections saved; schedules, windows, and MODE unchanged; "
            "07 uses collision_blocks instead of mtime; 03/06/07 use expected_sha commit plus "
            "guarded build, variants, and pages; exit 75 keeps waiting and exit 76 stops"
        ),
        "verified": False,
        "signature_verification": "underway",
        "acceptance_evidence": None,
    }


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _calls_present(text: str, calls: tuple[str, ...]) -> bool:
    return all(call in text for call in calls)


def classify_tree(root: Path) -> dict:
    """Inspect one music-library tree. Does not write."""
    root = Path(root)
    poll = root / "work" / "poll_ratings.py"
    make = root / "work" / "make_deploy_variants.py"
    ne = root / "work" / "ne_avail.py"
    revision = root / "work" / "ops" / "unified_writer" / "REVISION"
    poll_text = poll.read_text(encoding="utf-8") if poll.is_file() else ""
    make_text = make.read_text(encoding="utf-8") if make.is_file() else ""
    ne_text = ne.read_text(encoding="utf-8") if ne.is_file() else ""
    rev = revision.read_text(encoding="utf-8").strip() if revision.is_file() else ""
    poll_ok = _calls_present(poll_text, POLL_CALLS)
    make_ok = _calls_present(make_text, MAKE_CALLS)
    ne_ok = ne.is_file() and "open(" not in ne_text and _sha(ne) == NE_STDOUT_SHA256
    rev_ok = rev == PACKAGE_REVISION
    installed = []
    missing = []
    if poll_ok:
        installed.append({"id": "poll_ratings.py", "status": "guard_calls", "path": str(poll)})
    else:
        missing.append({
            "id": "poll_ratings.py",
            "expected_path": "work/poll_ratings.py",
            "why": "guard call lines are absent; a GUARD_v3 comment alone is not an install",
        })
    if make_ok:
        installed.append({"id": "make_deploy_variants.py", "status": "guard_calls", "path": str(make)})
    else:
        missing.append({
            "id": "make_deploy_variants.py",
            "expected_path": "work/make_deploy_variants.py",
            "why": "guard call lines are absent; a GUARD_v3 comment alone is not an install",
        })
    if ne_ok:
        installed.append({"id": "ne_avail.py", "status": "stdout_only", "path": str(ne), "sha256": NE_STDOUT_SHA256})
    else:
        missing.append({
            "id": "ne_avail.py",
            "expected_path": "work/ne_avail.py",
            "why": "stdout-only bytes were not verified",
        })
    return {
        "package_revision": rev,
        "package_revision_ok": rev_ok,
        "installed": installed,
        "entrypoint_gaps": missing,
        "anchors_installed": poll_ok and make_ok and ne_ok and rev_ok,
    }


def accept_evidence(document: dict) -> dict:
    """Accept only the four generated observations. Strings such as \"true\" or \"75\" fail."""
    if not isinstance(document, dict) or document.get("kind") != EVIDENCE_KIND:
        return {"accepted": False, "reason": "kind"}
    checks = document.get("checks")
    if not isinstance(checks, list):
        return {"accepted": False, "reason": "checks"}
    found = {}
    for item in checks:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            return {"accepted": False, "reason": "item"}
        found[item["id"]] = item
    for check_id, expect in EVIDENCE_CHECKS.items():
        got = found.get(check_id)
        if got is None:
            return {"accepted": False, "reason": check_id}
        for key, value in expect.items():
            if got.get(key) is not value:
                return {"accepted": False, "reason": check_id + "." + key}
    return {"accepted": True, "reason": ""}


def load_evidence(path: str) -> dict:
    if not path:
        return {"accepted": False, "reason": "not_provided"}
    file = Path(path)
    if not file.is_file():
        return {"accepted": False, "reason": "absent"}
    try:
        document = json.loads(file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"accepted": False, "reason": "json"}
    return accept_evidence(document)
