"""Publish-claim check with no flock and no host secrets.

Copied next to pub_remote.py on the Windows publish host. The Linux
coordinator writes PUBLISH_CLAIM.json into the staging directory after it
holds the library lock and the mkdir claim. Replacing index.html without
that claim fails closed.
"""
from __future__ import annotations

import hashlib
import json
import os
import time


class PublishGuardError(SystemExit):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise PublishGuardError("publish_claim_corrupt")
    return data


def require_claim_document(document: dict, expected_sha: str) -> dict:
    if document.get("guard") != "HARMONIA_UNIFIED_GUARD_v1" or document.get("contract_version") != 1:
        raise PublishGuardError("publish_contract")
    if document.get("resource_cas") != expected_sha:
        raise PublishGuardError("publish_cas_mismatch")
    claim_dir = document.get("claim_dir") or ""
    if not claim_dir:
        raise PublishGuardError("publish_claim_dir_missing")
    live_path = os.path.join(claim_dir, "meta.json")
    if not os.path.isfile(live_path):
        raise PublishGuardError("publish_claim_dir_missing")
    live = _read_json(live_path)
    if live.get("owner_token") != document.get("owner_token"):
        raise PublishGuardError("publish_claim_fence")
    if str(live.get("fence")) != str(document.get("fence")):
        raise PublishGuardError("publish_claim_fence")
    if time.time() > float(live.get("lease_until") or 0):
        raise PublishGuardError("publish_lease_expired")
    return live


def require_index_claim(staging: str, index_sha: str) -> dict:
    path = os.path.join(staging, "PUBLISH_CLAIM.json")
    if not os.path.isfile(path):
        raise PublishGuardError("publish_claim_missing")
    return require_claim_document(_read_json(path), index_sha)


def require_backup_claim(backup_path: str) -> dict:
    explicit = os.environ.get("HARMONIA_PUBLISH_CLAIM_FILE") or ""
    path = explicit or os.path.join(os.path.dirname(os.path.abspath(backup_path)), "PUBLISH_CLAIM.json")
    if not os.path.isfile(path):
        raise PublishGuardError("publish_claim_missing")
    return require_claim_document(_read_json(path), _sha256(backup_path))
