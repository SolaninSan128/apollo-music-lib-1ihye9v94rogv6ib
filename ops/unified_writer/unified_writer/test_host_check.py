"""On-host check: real guard calls versus a comment, and a non-publishing claim probe."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
FIX = PKG / "fixtures" / "prod"
CHECK = PKG / "bin" / "check.sh"
PROBE = PKG / "bin" / "win-claim-probe"


def env():
    out = os.environ.copy()
    out["PYTHONPATH"] = str(PKG) + os.pathsep + out.get("PYTHONPATH", "")
    return out


def patched_tree() -> Path:
    tree = Path(tempfile.mkdtemp(prefix="host-check-"))
    shutil.copytree(FIX, tree / "src")
    proc = subprocess.run(
        [sys.executable, "-m", "unified_writer.patch_install", "--root", str(tree / "src"),
         "--layout", "fixture", "--fixture"],
        cwd=str(PKG), capture_output=True, text=True, env=env(), check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr + proc.stdout)
    rev = tree / "src" / "work" / "ops" / "unified_writer"
    rev.mkdir(parents=True, exist_ok=True)
    (rev / "REVISION").write_text("4\n", encoding="utf-8")
    return tree


class HostCheckTests(unittest.TestCase):
    def test_real_calls_are_installed_and_live_stays_blocked(self):
        tree = patched_tree()
        mode = tree / "MODE.json"
        mode.write_text('{"ratings":"shadow","gapfill":"shadow","dailycheck":"shadow"}\n', encoding="utf-8")
        decoy = tree / "src" / "work" / "ops" / "unified_writer" / "LOMAX_CONTRACT.md"
        decoy.write_text("not the contract path\n", encoding="utf-8")
        proc = subprocess.run(
            ["bash", str(CHECK), "--root", str(tree / "src"), "--mode-file", str(mode)],
            capture_output=True, text=True, env=env(), check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        body = json.loads(proc.stdout)
        ids = {item["id"] for item in body["missing"]}
        self.assertNotIn("poll_ratings.py", ids)
        self.assertNotIn("make_deploy_variants.py", ids)
        self.assertNotIn("ne_avail.py", ids)
        installed = {item["id"]: item["status"] for item in body["anchors"]["installed"]}
        self.assertEqual(installed["poll_ratings.py"], "guard_calls")
        self.assertEqual(installed["make_deploy_variants.py"], "guard_calls")
        self.assertEqual(installed["ne_avail.py"], "stdout_only")
        self.assertTrue(body["install_completed"])
        self.assertFalse(body["runtime_evidence_accepted"])
        self.assertFalse(body["ready_for_live"])
        self.assertTrue(body["formal_switch_blocked"])
        blocker_ids = {item["id"] for item in body["blockers"]}
        self.assertEqual(blocker_ids, {
            "saved_routine_acceptance", "mode_acceptance", "claim_lifecycle_acceptance", "raw_writers",
        })
        self.assertTrue(body["routines"]["reported_saved"])
        self.assertFalse(body["routines"]["verified"])
        self.assertIsNone(body["routines"]["acceptance_evidence"])
        self.assertEqual(body["routines"]["contract_path"], "work/ops/LOMAX_CONTRACT.md")
        self.assertFalse(body["routines"]["contract_file_present"])
        self.assertFalse(body["bili"]["end_to_end_ran"])
        self.assertFalse(body["bili"]["deletion_gate_deployed"])
        self.assertFalse(body["bili"]["blocks_music_lock_install"])
        self.assertEqual(mode.read_text(encoding="utf-8"), '{"ratings":"shadow","gapfill":"shadow","dailycheck":"shadow"}\n')

    def test_comment_marker_does_not_complete_install(self):
        tree = patched_tree()
        poll = tree / "src" / "work" / "poll_ratings.py"
        poll.write_text(poll.read_text(encoding="utf-8").split("_cas.guard_library_mutation", 1)[0] + "# HARMONIA_UNIFIED_GUARD_v3\n", encoding="utf-8")
        proc = subprocess.run(
            ["bash", str(CHECK), "--root", str(tree / "src")],
            capture_output=True, text=True, env=env(), check=False,
        )
        body = json.loads(proc.stdout)
        self.assertFalse(body["install_completed"])
        self.assertFalse(body["ready_for_live"])
        self.assertIn("poll_ratings.py", {item["id"] for item in body["missing"]})

    def test_evidence_requires_generated_bools_and_codes(self):
        from unified_writer.host_check import accept_evidence
        fake = {"kind": "harmonia_install_evidence_v1", "ready_for_live": True, "checks": [
            {"id": "poll_held_lock", "exit_code": "75", "snapshot_unchanged": "true"},
        ]}
        self.assertFalse(accept_evidence(fake)["accepted"])
        good = {"kind": "harmonia_install_evidence_v1", "checks": [
            {"id": "poll_held_lock", "exit_code": 75, "snapshot_unchanged": True},
            {"id": "make_held_lock", "exit_code": 75, "output_dir_created": False},
            {"id": "fd_inherited", "exit_code": 0, "inherited": True},
            {"id": "cas_mismatch", "exit_code": 76, "cas_mismatch": True},
        ]}
        self.assertTrue(accept_evidence(good)["accepted"])
        tree = patched_tree()
        evidence = tree / "evidence.json"
        evidence.write_text(json.dumps(good), encoding="utf-8")
        proc = subprocess.run(
            ["bash", str(CHECK), "--root", str(tree / "src"), "--evidence", str(evidence)],
            capture_output=True, text=True, env=env(), check=False,
        )
        body = json.loads(proc.stdout)
        self.assertTrue(body["runtime_evidence_accepted"])
        self.assertTrue(body["install_completed"])
        self.assertFalse(body["ready_for_live"])
        self.assertTrue(body["formal_switch_blocked"])

    def test_claim_probe_conflicts_and_refuses_publish_path(self):
        cand = Path(tempfile.mkdtemp(prefix="cand-claim-"))
        proc = subprocess.run(
            ["bash", str(PROBE), "--dir", str(cand)],
            capture_output=True, text=True, env=env(), check=False, timeout=15,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        body = json.loads(proc.stdout)
        self.assertTrue(body["ok"])
        self.assertFalse(body["published"])
        self.assertEqual(body["exit_code"], 75)
        publish = Path(tempfile.mkdtemp(prefix="harmonia")) / "pub"
        publish.mkdir()
        (publish / "index.html").write_text("site", encoding="utf-8")
        refused = subprocess.run(
            ["bash", str(PROBE), "--dir", str(publish)],
            capture_output=True, text=True, env=env(), check=False,
        )
        self.assertEqual(refused.returncode, 76, refused.stderr)
        self.assertEqual((publish / "index.html").read_text(encoding="utf-8"), "site")


if __name__ == "__main__":
    raise SystemExit(0 if unittest.main(verbosity=2, exit=False).result.wasSuccessful() else 1)
