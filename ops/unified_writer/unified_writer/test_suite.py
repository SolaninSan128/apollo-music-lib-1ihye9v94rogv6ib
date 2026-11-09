"""Executable tests for the unified writer guard.

Exclusion tests use separate processes and util-linux flock. They do not mock
the kernel lock and they do not touch the Pages homepage or a production path.
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
BIN = PKG / "bin" if (PKG / "bin" / "with_unified_lock").exists() else PKG
REF = PKG / "reference"


def base_env(**extra):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PKG) + os.pathsep + env.get("PYTHONPATH", "")
    env.update(extra)
    return env


def run_mod(args, env=None, timeout=30):
    return subprocess.run(
        [sys.executable, "-m", "unified_writer.wrapper", *args],
        capture_output=True,
        text=True,
        env=env or base_env(),
        timeout=timeout,
        check=False,
    )


def status_of(proc) -> dict:
    lines = [line for line in (proc.stderr or "").splitlines() if line.startswith("HARMONIA_STATUS\t")]
    if not lines:
        raise AssertionError(f"no status\nstdout={proc.stdout}\nstderr={proc.stderr}")
    return json.loads(lines[-1].split("\t", 1)[1])


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def wait_file(path: Path, timeout=5):
    start = time.time()
    while time.time() - start < timeout:
        if path.exists() and path.stat().st_size > 0:
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path}")


def spawn(args, env=None):
    return subprocess.Popen(
        args,
        start_new_session=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env or base_env(),
    )


def stop(proc):
    if proc.poll() is None:
        os.killpg(proc.pid, signal.SIGKILL)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
    for pipe in (proc.stdout, proc.stderr):
        if pipe is not None:
            pipe.close()


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="uwriter-"))
        self.root = self.tmp / "root"
        self.root.mkdir()
        self.lock = self.root / "library.lock"
        self.cas = self.tmp / "library.json"
        self.cas.write_text('{"v":1}\n', encoding="utf-8")
        self.cas_hash = sha256(self.cas)

    def common(self, *extra):
        return [
            "--root", str(self.root),
            "--library-lock", str(self.lock),
            "--wait", "0.4",
            "--skew", "0",
            *extra,
        ]

    def test_subprocess_exclusion_against_util_linux_flock(self):
        self.lock.write_bytes(b"")
        stamp = self.tmp / "held"
        sentinel = self.tmp / "sentinel"
        holder = spawn(["flock", str(self.lock), "bash", "-c", f"echo held > {stamp}; sleep 30"])
        try:
            wait_file(stamp)
            proc = run_mod([
                "run", *self.common("--entry", "ratings"),
                "--", sys.executable, "-c", f"open({str(sentinel)!r},'w').write('x')",
            ])
            self.assertEqual(proc.returncode, 75, proc.stderr)
            self.assertEqual(status_of(proc)["error"], "busy:library")
            self.assertFalse(sentinel.exists())
        finally:
            stop(holder)
        again = run_mod(["run", "--plan-only", *self.common("--entry", "ratings")])
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(status_of(again)["acq"]["order"], ["library"])

    def test_holder_blocks_second_wrapper_and_shell_flock(self):
        stamp = self.tmp / "stamp"
        holder = spawn([
            sys.executable, "-m", "unified_writer.wrapper", "hold",
            *self.common("--entry", "ratings", "--seconds", "30", "--stamp", str(stamp)),
        ])
        try:
            wait_file(stamp)
            blocked = run_mod(["run", *self.common("--entry", "build_push"), "--", sys.executable, "-c", "print('no')"])
            self.assertEqual(blocked.returncode, 75, blocked.stderr)
            self.assertFalse((self.root / "winpub" / "publish.claim.d").exists())
            flock = subprocess.run(
                ["flock", "-n", str(self.lock), "-c", "echo got"],
                capture_output=True, text=True, check=False,
            )
            self.assertNotEqual(flock.returncode, 0)
            self.assertNotIn("got", flock.stdout)
        finally:
            stop(holder)

    def test_writes_are_not_interleaved(self):
        ready = self.tmp / "ready"
        out = self.tmp / "out"
        code = (
            "import time,sys\n"
            "ready, dest, letter, pause = sys.argv[1:]\n"
            "open(ready,'w').write(letter)\n"
            "open(dest,'a').write(letter+'\\n')\n"
            "time.sleep(float(pause))\n"
            "open(dest,'a').write(letter+'\\n')\n"
        )
        script = self.tmp / "write_twice.py"
        script.write_text(code, encoding="utf-8")
        first = spawn([
            sys.executable, "-m", "unified_writer.wrapper", "run",
            *self.common("--entry", "ratings", "--wait", "2"),
            "--", sys.executable, str(script), str(ready), str(out), "A", "0.6",
        ])
        try:
            wait_file(ready)
            second = run_mod([
                "run", *self.common("--entry", "ratings", "--wait", "2"),
                "--", sys.executable, str(script), str(self.tmp / "ready-b"), str(out), "B", "0",
            ], timeout=10)
            self.assertEqual(second.returncode, 0, second.stderr)
        finally:
            try:
                first.wait(timeout=10)
            finally:
                stop(first)
        text = out.read_text(encoding="utf-8")
        self.assertNotIn("A\nB\nA\n", text)
        self.assertEqual(text, "A\nA\nB\nB\n")

    def test_legacy_script_and_new_lock_share_inode(self):
        work = self.tmp / "work"
        ops = work / "ops"
        ops.mkdir(parents=True)
        legacy = ops / "with_library_lock.sh"
        legacy.write_text((REF / "with_library_lock.OLD.sh").read_text(encoding="utf-8"), encoding="utf-8")
        legacy.chmod(legacy.stat().st_mode | stat.S_IEXEC)
        lock = work / ".library.lock"
        stamp = self.tmp / "legacy-held"
        holder = spawn(["bash", str(legacy), "bash", "-c", f"echo held > {stamp}; sleep 30"])
        try:
            wait_file(stamp)
            proc = run_mod([
                "run", "--root", str(self.root), "--library-lock", str(lock),
                "--wait", "0.3", "--skew", "0", "--entry", "ratings",
                "--", sys.executable, "-c", "print('ran')",
            ])
            self.assertEqual(proc.returncode, 75, proc.stderr)
            self.assertNotIn("ran", proc.stdout)
        finally:
            stop(holder)

    def test_stale_owner_file_does_not_block(self):
        self.lock.write_bytes(b"")
        Path(str(self.lock) + ".owner").write_text(
            "with_library_lock pid=1 since=2026-11-07 cmd=old\n", encoding="utf-8"
        )
        proc = run_mod(["run", "--plan-only", *self.common("--entry", "ratings")])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        body = status_of(proc)
        self.assertTrue(body["acq"]["ok"])
        self.assertFalse((self.root / "winpub" / "publish.claim.d").exists())

    def test_dynamic_script_names_are_not_a_deny_list(self):
        script = self.tmp / "ingest_2026_11_09.py"
        script.write_text("print('dynamic-ok')\n", encoding="utf-8")
        proc = run_mod([
            "run", *self.common("--entry", "ratings"),
            "--", sys.executable, str(script),
        ])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("dynamic-ok", proc.stdout)
        guard_env = base_env()
        for key in list(guard_env):
            if key.startswith("HARMONIA_WRITER_") or key in ("HARMONIA_LIBRARY_LOCK_FD", "HARMONIA_RESOURCE_CAS"):
                guard_env.pop(key, None)
        guard = subprocess.run(
            ["bash", str(BIN / "guard-ratings")],
            capture_output=True, text=True, env=guard_env, check=False,
        )
        self.assertEqual(guard.returncode, 76, guard.stderr)
        self.assertIn("legacy_bypass_no_contract", guard.stderr)

    def test_revision_mismatch_does_not_run(self):
        sentinel = self.tmp / "sentinel"
        proc = run_mod([
            "run",
            *self.common(
                "--entry", "increment",
                "--cas-file", str(self.cas),
                "--cas-hash", "0" * 64,
            ),
            "--", sys.executable, "-c", f"open({str(sentinel)!r},'w').write('x')",
        ])
        self.assertEqual(proc.returncode, 76, proc.stderr)
        self.assertEqual(status_of(proc)["error"], "cas_mismatch")
        self.assertEqual(self.cas.read_text(encoding="utf-8"), '{"v":1}\n')
        self.assertFalse(sentinel.exists())
        self.assertFalse(Path(str(self.lock) + ".meta.json").exists())

    def test_fence_mismatch_refuses_commit(self):
        script = self.tmp / "bad_fence.py"
        script.write_text(textwrap.dedent("""
            import json, os, sys
            from pathlib import Path
            from unified_writer.contract import commit_cas
            from unified_writer.util import LockError
            meta_p = Path(os.environ["HARMONIA_LIBRARY_LOCK_PATH"] + ".meta.json")
            meta = json.loads(meta_p.read_text())
            meta["fence"] = int(meta["fence"]) + 9
            meta_p.write_text(json.dumps(meta))
            try:
                commit_cas(Path(sys.argv[1]), b"v2\\n", sys.argv[2])
                print("committed")
            except LockError as exc:
                print(exc.code)
        """), encoding="utf-8")
        proc = run_mod([
            "run", *self.common("--entry", "ratings"),
            "--", sys.executable, str(script), str(self.cas), self.cas_hash,
        ])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("fence_mismatch", proc.stdout)
        self.assertEqual(self.cas.read_text(encoding="utf-8"), '{"v":1}\n')

    def test_commit_cas_under_contract(self):
        script = self.tmp / "good_commit.py"
        script.write_text(textwrap.dedent("""
            import sys
            from pathlib import Path
            from unified_writer.contract import commit_cas
            print(commit_cas(Path(sys.argv[1]), b'{"v":2}\\n', sys.argv[2])["ok"])
        """), encoding="utf-8")
        proc = run_mod([
            "run", *self.common("--entry", "ratings"),
            "--", sys.executable, str(script), str(self.cas), self.cas_hash,
        ])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("True", proc.stdout)
        self.assertEqual(self.cas.read_text(encoding="utf-8"), '{"v":2}\n')

    def test_crash_unexpired_lease_fail_closed(self):
        stamp = self.tmp / "orphan"
        sentinel = self.tmp / "sentinel"
        proc = run_mod([
            "crash-orphan", *self.common("--entry", "ratings", "--lease", "3600", "--stamp", str(stamp)),
        ])
        self.assertEqual(proc.returncode, 99, proc.stderr)
        self.assertTrue(stamp.exists())
        nxt = run_mod([
            "run", *self.common("--entry", "ratings", "--lease", "3600"),
            "--", sys.executable, "-c", f"open({str(sentinel)!r},'w').write('x')",
        ])
        self.assertEqual(nxt.returncode, 76, nxt.stderr)
        self.assertEqual(status_of(nxt)["error"], "crash_lease_fail_closed")
        self.assertFalse(sentinel.exists())
        self.assertTrue(Path(str(self.lock) + ".meta.json").exists())

    def test_expired_dirty_without_cas_and_with_mismatch(self):
        stamp = self.tmp / "orphan"
        proc = run_mod([
            "crash-orphan",
            *self.common("--entry", "ratings", "--lease", "-5", "--stamp", str(stamp),
                         "--cas-file", str(self.cas), "--cas-hash", self.cas_hash),
        ])
        self.assertEqual(proc.returncode, 99, proc.stderr)
        no_cas = run_mod([
            "run", *self.common("--entry", "ratings", "--lease", "30"),
            "--", sys.executable, "-c", "print('ran')",
        ])
        self.assertEqual(no_cas.returncode, 76, no_cas.stderr)
        self.assertEqual(status_of(no_cas)["error"], "expired_dirty_needs_cas")
        self.assertNotIn("ran", no_cas.stdout)
        self.cas.write_text('{"v":2}\n', encoding="utf-8")
        mismatch = run_mod([
            "run",
            *self.common("--entry", "increment", "--lease", "30",
                         "--cas-file", str(self.cas), "--cas-hash", self.cas_hash),
            "--", sys.executable, "-c", "print('ran')",
        ])
        self.assertEqual(mismatch.returncode, 76, mismatch.stderr)
        self.assertEqual(status_of(mismatch)["error"], "cas_mismatch")
        self.assertEqual(self.cas.read_text(encoding="utf-8"), '{"v":2}\n')
        self.assertTrue(Path(str(self.lock) + ".meta.json").exists())

    def test_expired_dirty_cas_match_recovers_and_old_token_fails(self):
        stamp = self.tmp / "orphan"
        proc = run_mod([
            "crash-orphan",
            *self.common("--entry", "ratings", "--lease", "-5", "--stamp", str(stamp),
                         "--cas-file", str(self.cas), "--cas-hash", self.cas_hash),
        ])
        self.assertEqual(proc.returncode, 99, proc.stderr)
        old = json.loads(Path(str(self.lock) + ".meta.json").read_text(encoding="utf-8"))
        hold_stamp = self.tmp / "recovered"
        holder = spawn([
            sys.executable, "-m", "unified_writer.wrapper", "hold",
            *self.common("--entry", "ratings", "--lease", "30", "--seconds", "30",
                         "--stamp", str(hold_stamp), "--cas-file", str(self.cas),
                         "--cas-hash", self.cas_hash),
        ])
        try:
            wait_file(hold_stamp)
            new_token = hold_stamp.read_text(encoding="utf-8").strip()
            self.assertNotEqual(new_token, old["owner_token"])
            meta = json.loads(Path(str(self.lock) + ".meta.json").read_text(encoding="utf-8"))
            self.assertGreater(int(meta["fence"]), int(old["fence"]))
            outsider = self.tmp / "outsider.py"
            outsider.write_text(textwrap.dedent(f"""
                import os
                from pathlib import Path
                from unified_writer.contract import commit_cas
                from unified_writer.util import LockError
                os.environ["HARMONIA_WRITER_CONTRACT"] = "1"
                os.environ["HARMONIA_CONTRACT_VERSION"] = "1"
                os.environ["HARMONIA_WRITER_ENTRY"] = "ratings"
                os.environ["HARMONIA_WRITER_TOKEN"] = {old["owner_token"]!r}
                os.environ["HARMONIA_WRITER_FENCE_LIBRARY"] = {str(old["fence"])!r}
                os.environ["HARMONIA_WRITER_KINDS"] = "library"
                os.environ["HARMONIA_LOCK_ORDER"] = "library,publish"
                os.environ["HARMONIA_LIBRARY_LOCK_PATH"] = {str(self.lock)!r}
                os.environ["HARMONIA_LIBRARY_LOCK_FD"] = "19"
                try:
                    commit_cas(Path({str(self.cas)!r}), b"stolen\\n", {self.cas_hash!r})
                    print("committed")
                except LockError as exc:
                    print(exc.code)
            """), encoding="utf-8")
            stolen = subprocess.run(
                [sys.executable, str(outsider)],
                capture_output=True, text=True, env=base_env(), check=False,
            )
            self.assertEqual(stolen.returncode, 0, stolen.stderr)
            self.assertNotIn("committed", stolen.stdout)
            self.assertNotEqual(stolen.stdout.strip(), "")
            self.assertEqual(self.cas.read_text(encoding="utf-8"), '{"v":1}\n')
        finally:
            stop(holder)

    def test_publish_conflict_releases_library_and_keeps_foreign_claim(self):
        from unified_writer.winclaim import acquire_claim
        parent = self.root / "winpub"
        acquire_claim(
            parent,
            token="foreign-token",
            lease_sec=3600,
            hostname="hora-win",
            pid=999999,
            who="foreign",
            cmd="publish",
            resource_cas=None,
            cas_file=None,
            wait_sec=0,
            remote_skew_sec=0,
        )
        proc = run_mod([
            "run", "--plan-only",
            *self.common("--entry", "publish", "--cas-file", str(self.cas), "--cas-hash", self.cas_hash),
        ])
        self.assertEqual(proc.returncode, 75, proc.stderr)
        self.assertEqual(status_of(proc)["error"], "publish_conflict")
        self.assertFalse(Path(str(self.lock) + ".meta.json").exists())
        flock = subprocess.run(
            ["flock", "-n", str(self.lock), "-c", "echo free"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(flock.returncode, 0, flock.stderr)
        self.assertEqual(flock.stdout.strip(), "free")
        meta = json.loads((parent / "publish.claim.d" / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["owner_token"], "foreign-token")
        self.assertEqual(meta["host"], "hora-win")

    def test_publish_order_and_library_holder_blocks_before_claim(self):
        proc = run_mod([
            "run", "--plan-only",
            *self.common("--entry", "publish", "--cas-file", str(self.cas), "--cas-hash", self.cas_hash),
        ])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(status_of(proc)["acq"]["order"], ["library", "publish"])
        lib_fence = Path(str(self.lock) + ".fence")
        pub_fence = self.root / "winpub" / "publish.fence"
        self.assertTrue(lib_fence.exists())
        self.assertTrue(pub_fence.exists())
        self.assertLessEqual(lib_fence.stat().st_mtime, pub_fence.stat().st_mtime + 1)
        self.assertFalse((self.root / "winpub" / "publish.claim.d").exists())
        stamp = self.tmp / "lib-held"
        holder = spawn([
            sys.executable, "-m", "unified_writer.wrapper", "hold",
            *self.common("--entry", "ratings", "--seconds", "30", "--stamp", str(stamp)),
        ])
        try:
            wait_file(stamp)
            before = list((self.root / "winpub").glob("publish.claim.d*"))
            blocked = run_mod([
                "run", "--plan-only",
                *self.common("--entry", "publish", "--cas-file", str(self.cas), "--cas-hash", self.cas_hash),
            ])
            self.assertEqual(blocked.returncode, 75, blocked.stderr)
            self.assertEqual(status_of(blocked)["error"], "busy:library")
            after = list((self.root / "winpub").glob("publish.claim.d*"))
            self.assertEqual(before, after)
        finally:
            stop(holder)

    def test_remote_expired_claim_fail_closed_without_cas_then_recovers(self):
        parent = self.tmp / "win"
        parent.mkdir()
        claim = parent / "publish.claim.d"
        claim.mkdir()
        (claim / "meta.json").write_text(json.dumps({
            "kind": "publish",
            "pid": 999999,
            "host": "hora-win",
            "owner_token": "old-remote",
            "fence": 3,
            "since_epoch": time.time() - 500,
            "lease_until": time.time() - 120,
            "lease_sec": 30,
            "who": "foreign",
            "cmd": "publish",
            "resource_cas": None,
            "contract_version": 1,
        }), encoding="utf-8")
        refused = subprocess.run(
            [sys.executable, "-m", "unified_writer.winpub_lock", "hold",
             "--dir", str(parent), "--wait", "0.2", "--skew", "30", "--seconds", "0.1"],
            capture_output=True, text=True, env=base_env(), check=False,
        )
        self.assertEqual(refused.returncode, 76, refused.stderr)
        self.assertEqual(status_of(refused)["error"], "expired_dirty_needs_cas")
        self.assertTrue((claim / "meta.json").exists())
        recovered = subprocess.run(
            [sys.executable, "-m", "unified_writer.winpub_lock", "hold",
             "--dir", str(parent), "--wait", "0.5", "--skew", "30", "--seconds", "0.1",
             "--cas-file", str(self.cas), "--cas-hash", self.cas_hash, "--stamp", str(self.tmp / "win-stamp")],
            capture_output=True, text=True, env=base_env(), check=False,
        )
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertFalse(claim.exists())
        graves = list(parent.glob("publish.claim.d.dead.*"))
        self.assertTrue(graves)

    def test_win_helper_subprocess_conflict_and_no_fcntl(self):
        probe = subprocess.run(
            [sys.executable, "-c", "import sys, unified_writer.winpub_lock; assert 'fcntl' not in sys.modules"],
            capture_output=True, text=True, env=base_env(), check=False,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr)
        parent = self.tmp / "win2"
        stamp = self.tmp / "win-held"
        holder = spawn([
            sys.executable, "-m", "unified_writer.winpub_lock", "hold",
            "--dir", str(parent), "--seconds", "30", "--stamp", str(stamp), "--skew", "0",
        ])
        try:
            wait_file(stamp)
            second = subprocess.run(
                [sys.executable, "-m", "unified_writer.winpub_lock", "hold",
                 "--dir", str(parent), "--wait", "0.2", "--seconds", "0.1", "--skew", "0"],
                capture_output=True, text=True, env=base_env(), check=False,
            )
            self.assertEqual(second.returncode, 75, second.stderr)
            self.assertEqual(status_of(second)["error"], "publish_conflict")
        finally:
            stop(holder)

    def test_shell_contract_timeout_and_crash(self):
        script = BIN / "with_library_lock.sh"
        env = base_env(HARMONIA_LIBRARY_LOCK=str(self.lock), HARMONIA_LOCK_ROOT=str(self.root), HARMONIA_LEASE_SEC="3600")
        ok = subprocess.run(
            ["bash", str(script), sys.executable, "-c",
             "from unified_writer.contract import assert_contract; assert_contract('library_compat'); print('contract-ok')"],
            capture_output=True, text=True, env=env, check=False,
        )
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertIn("contract-ok", ok.stdout)
        self.assertFalse(Path(str(self.lock) + ".meta.json").exists())
        again = subprocess.run(
            ["bash", str(script), sys.executable, "-c", "print('second')"],
            capture_output=True, text=True, env=env, check=False,
        )
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("second", again.stdout)

        self.lock.write_bytes(b"")
        stamp = self.tmp / "shell-held"
        holder = spawn(["flock", str(self.lock), "bash", "-c", f"echo held > {stamp}; sleep 30"])
        try:
            wait_file(stamp)
            timed = subprocess.run(
                ["bash", str(script), "true"],
                capture_output=True, text=True,
                env=base_env(HARMONIA_LIBRARY_LOCK=str(self.lock), HARMONIA_LOCK_ROOT=str(self.root), LOCK_WAIT="1"),
                check=False, timeout=10,
            )
            self.assertEqual(timed.returncode, 75, timed.stderr)
            self.assertIn("超时", timed.stderr)
        finally:
            stop(holder)

        crash_stamp = self.tmp / "shell-crash"
        crashed = spawn([
            "bash", str(script), "bash", "-c", f"echo up > {crash_stamp}; sleep 30",
        ], env=env)
        try:
            wait_file(crash_stamp)
            self.assertTrue(Path(str(self.lock) + ".meta.json").exists())
        finally:
            stop(crashed)
        blocked = run_mod([
            "run", *self.common("--entry", "ratings", "--lease", "3600"),
            "--", sys.executable, "-c", "print('ran')",
        ])
        self.assertEqual(blocked.returncode, 76, blocked.stderr)
        self.assertEqual(status_of(blocked)["error"], "crash_lease_fail_closed")
        self.assertNotIn("ran", blocked.stdout)

    def test_guard_missing_entrypoint_and_publish_execute_blocked(self):
        missing = run_mod([
            "run", *self.common("--entry", "ratings"),
            "--", "bash", str(BIN / "guard-ratings"),
        ])
        self.assertEqual(missing.returncode, 76, missing.stderr)
        self.assertIn("missing_real_entrypoint", missing.stderr)
        publish = run_mod([
            "run",
            *self.common("--entry", "publish", "--cas-file", str(self.cas), "--cas-hash", self.cas_hash),
            "--", "bash", str(BIN / "guard-publish"), "--execute",
        ])
        self.assertEqual(publish.returncode, 76, publish.stderr)
        self.assertIn("publish_execute_blocked", publish.stderr)
        real = self.tmp / "real_ratings.py"
        sentinel = self.tmp / "sentinel"
        real.write_text(textwrap.dedent(f"""
            from unified_writer.contract import assert_contract
            assert_contract("ratings")
            open({str(sentinel)!r}, "w").write("ok")
        """), encoding="utf-8")
        opened = run_mod(
            ["run", *self.common("--entry", "ratings"), "--", "bash", str(BIN / "guard-ratings")],
            env=base_env(HARMONIA_REAL_RATINGS=str(real)),
        )
        self.assertEqual(opened.returncode, 0, opened.stderr)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "ok")

    def test_increment_requires_cas_and_plan_does_not_execute(self):
        proc = run_mod(["run", *self.common("--entry", "download_writeback"), "--", sys.executable, "-c", "print(1)"])
        self.assertEqual(proc.returncode, 76, proc.stderr)
        self.assertEqual(status_of(proc)["error"], "cas_required")
        plan = run_mod(["plan", "--entry", "increment"])
        self.assertEqual(plan.returncode, 0, plan.stderr)
        body = json.loads(plan.stdout)
        self.assertEqual(body["locks_in_order"], ["library"])
        self.assertTrue(body["requires_cas"])
        self.assertTrue(body["formal_switch_blocked"])
        self.assertFalse(body["will_execute"])
        self.assertFalse(body["real_present"])
        pub = run_mod(["plan", "--entry", "publish"])
        self.assertEqual(json.loads(pub.stdout)["locks_in_order"], ["library", "publish"])

    def test_stop_write_check_blocks_switch_without_creating_lock(self):
        missing = self.tmp / "no-such.lock"
        mode = self.tmp / "MODE.json"
        mode.write_text((REF / "MODE.json").read_text(encoding="utf-8"), encoding="utf-8")
        before = mode.read_text(encoding="utf-8")
        proc = subprocess.run(
            ["bash", str(BIN / "check.sh"), "--library-lock", str(missing), "--mode-file", str(mode)],
            capture_output=True, text=True, env=base_env(), check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        body = json.loads(proc.stdout)
        self.assertTrue(body["formal_switch_blocked"])
        self.assertFalse(body["ok_to_claim_global"])
        self.assertFalse(body["production_installed"])
        self.assertIn("exit_0_means_switch_still_blocked", body["meaning"])
        self.assertTrue(body["library_lock"]["absent"])
        self.assertFalse(missing.exists())
        self.assertEqual(mode.read_text(encoding="utf-8"), before)
        self.assertEqual(body["mode"]["ratings"], "shadow")
        ids = {item["id"] for item in body["missing"]}
        self.assertIn("make_deploy_variants.py", ids)
        self.assertIn("lomax_routines_not_updated", ids)
        self.assertIn("windows_claim_not_proven_on_hora", ids)
        self.assertTrue(body["ready_for_install"])
        self.assertFalse(body["ready_for_live"])
        self.assertTrue(body["formal_switch_blocked"])

    def test_install_rollback_preserves_library_and_mode(self):
        dest = self.tmp / "dest"
        ops = dest / "work" / "ops"
        ops.mkdir(parents=True)
        library = dest / "work" / "library.json"
        library.write_text('{"keep":true}\n', encoding="utf-8")
        mode = dest / "work" / "ops" / "MODE.json"
        mode.write_text('{"ratings":"shadow","gapfill":"shadow","dailycheck":"shadow"}\n', encoding="utf-8")
        old = ops / "with_library_lock.sh"
        old.write_text("echo OLD\n", encoding="utf-8")
        lib_hash = sha256(library)
        mode_hash = sha256(mode)
        installed = subprocess.run(
            ["bash", str(BIN / "install.sh"), "--dest", str(dest)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertIn("compat-begin", (ops / "with_library_lock.sh").read_text(encoding="utf-8"))
        self.assertEqual(sha256(library), lib_hash)
        self.assertEqual(sha256(mode), mode_hash)
        smoke = subprocess.run(
            ["bash", str(ops / "with_library_lock.sh"), sys.executable, "-c",
             "from unified_writer.contract import assert_contract; assert_contract('library_compat'); print('installed-ok')"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(smoke.returncode, 0, smoke.stderr)
        self.assertIn("installed-ok", smoke.stdout)
        self.assertEqual(sha256(library), lib_hash)
        rolled = subprocess.run(
            ["bash", str(BIN / "rollback.sh"), "--dest", str(dest)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(rolled.returncode, 0, rolled.stderr)
        self.assertEqual((ops / "with_library_lock.sh").read_text(encoding="utf-8"), "echo OLD\n")
        self.assertFalse((ops / "unified_writer").exists())
        self.assertEqual(sha256(library), lib_hash)
        self.assertEqual(sha256(mode), mode_hash)

    def test_install_refuses_pages_and_production_paths(self):
        pages = self.tmp / "pages"
        (pages / "img").mkdir(parents=True)
        (pages / "index.html").write_text("homepage", encoding="utf-8")
        refused = subprocess.run(
            ["bash", str(BIN / "install.sh"), "--dest", str(pages)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(refused.returncode, 76, refused.stderr)
        self.assertEqual((pages / "index.html").read_text(encoding="utf-8"), "homepage")
        music = self.tmp / "harmonia-handoff" / "music-library"
        (music / "work" / "ops").mkdir(parents=True)
        allowed = subprocess.run(
            ["bash", str(BIN / "install.sh"), "--dest", str(music)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertTrue((music / "work" / "ops" / "unified_writer" / "prod_guard.py").is_file())


if __name__ == "__main__":
    raise SystemExit(0 if unittest.main(verbosity=2, exit=False).result.wasSuccessful() else 1)
