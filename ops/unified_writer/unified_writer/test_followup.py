"""Host follow-up: reentrancy, CAS after the unlocked read, and the Bili consumer."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
FIX = PKG / "fixtures" / "prod"
SHELL = PKG / "bin" / "with_library_lock.sh"


def base_env(**extra):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PKG) + os.pathsep + env.get("PYTHONPATH", "")
    env["HARMONIA_UNIFIED_WRITER"] = str(PKG)
    env["HARMONIA_PUBLISH_TRANSPORT"] = "local"
    env.pop("HARMONIA_TARGET", None)
    env.pop("HARMONIA_WRITER_CONTRACT", None)
    env.pop("HARMONIA_LIBRARY_LOCK_FD", None)
    env.update({key: str(value) for key, value in extra.items()})
    return env


def install(dest: Path):
    proc = subprocess.run(
        [sys.executable, "-m", "unified_writer.patch_install", "--root", str(dest), "--layout", "fixture", "--fixture"],
        cwd=str(PKG), capture_output=True, text=True, env=base_env(), check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr + proc.stdout)


def hold(lock: Path, seconds: float):
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.touch()
    return subprocess.Popen(
        ["flock", str(lock), "sleep", str(seconds)],
        start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def stop(proc):
    if proc.poll() is None:
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait(timeout=5)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ReentrancyAndCasTests(unittest.TestCase):
    def test_outer_lock_and_inner_guard_do_not_deadlock(self):
        root = Path(tempfile.mkdtemp(prefix="reenter-"))
        lock = root / "library.lock"
        env = base_env(HARMONIA_LIBRARY_LOCK=str(lock), HARMONIA_LOCK_ROOT=str(root / "meta"), LOCK_WAIT="0")
        proc = subprocess.run(
            ["bash", str(SHELL), sys.executable, "-c",
             "import time\n"
             "from unified_writer.prod_guard import guard_library_mutation\n"
             "t=time.time()\n"
             "print(guard_library_mutation('ratings', None))\n"
             "print('%.3f' % (time.time()-t))\n"],
            capture_output=True, text=True, env=env, timeout=5, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("inherited", proc.stdout)
        self.assertLess(float(proc.stdout.strip().splitlines()[-1]), 1.0)

    def test_holder_must_outlive_wait(self):
        tree = Path(tempfile.mkdtemp(prefix="wait-"))
        shutil.copytree(FIX, tree / "src")
        install(tree / "src")
        work = tree / "src" / "work"
        (work / "poll_ratings.py").write_text("LABEL={}\ndef fetch_all():\n    return {}\n", encoding="utf-8")
        (work / "play_fields.py").write_text(
            "def copy_play_fields(a,b):\n    return None\ndef fill_missing_files(lib, fk, extra=False):\n    return [], None\n",
            encoding="utf-8",
        )
        library = tree / "src" / "library.json"
        library.write_text('{"daily_batches":[],"rated":[],"playlists":[]}\n', encoding="utf-8")
        lock = work / ".library.lock"
        holder = hold(lock, 30)
        try:
            time.sleep(0.05)
            blocked = subprocess.run(
                [sys.executable, str(work / "apply_ratings.py")],
                cwd=str(tree / "src"), capture_output=True, text=True,
                env=base_env(LOCK_WAIT="0.3"), timeout=5, check=False,
            )
        finally:
            stop(holder)
        self.assertEqual(blocked.returncode, 75, blocked.stderr)
        self.assertFalse((work / "ratings_snapshot.json").exists())
        holder = hold(lock, 0.3)
        try:
            allowed = subprocess.run(
                [sys.executable, str(work / "apply_ratings.py")],
                cwd=str(tree / "src"), capture_output=True, text=True,
                env=base_env(LOCK_WAIT="2"), timeout=8, check=False,
            )
        finally:
            stop(holder)
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertTrue((work / "ratings_snapshot.json").exists())

    def test_stale_library_after_unlocked_read_is_not_written(self):
        tree = Path(tempfile.mkdtemp(prefix="cas-"))
        shutil.copytree(FIX, tree / "src")
        install(tree / "src")
        work = tree / "src" / "work"
        (work / "poll_ratings.py").write_text("LABEL={}\ndef fetch_all():\n    return {}\n", encoding="utf-8")
        (work / "play_fields.py").write_text(
            "def copy_play_fields(a,b):\n    return None\ndef fill_missing_files(lib, fk, extra=False):\n    return [], None\n",
            encoding="utf-8",
        )
        library = tree / "src" / "library.json"
        library.write_text('{"daily_batches":[],"rated":[],"playlists":[]}\n', encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, str(work / "apply_ratings.py")],
            cwd=str(tree / "src"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=base_env(HARMONIA_CAS_PAUSE_SEC="1.2", LOCK_WAIT="0.2"),
        )
        time.sleep(0.3)
        library.write_text('{"daily_batches":[],"rated":[{"changed":true}],"playlists":[]}\n', encoding="utf-8")
        out, err = proc.communicate(timeout=5)
        self.assertEqual(proc.returncode, 76, err)
        self.assertIn("cas_mismatch", err)
        self.assertFalse((work / "ratings_snapshot.json").exists())
        self.assertIn("changed", library.read_text(encoding="utf-8"))

    def test_run_unpatched_refuses_the_old_read_label(self):
        from unified_writer.prod_guard import run_unpatched
        with self.assertRaises(SystemExit) as caught:
            run_unpatched("poll_ratings.py", [sys.executable, "-c", "print('no')"])
        self.assertEqual(caught.exception.code, 76)

    def test_self_check_ignores_a_shadowing_package(self):
        shadow = Path(tempfile.mkdtemp(prefix="shadow-"))
        pkg = shadow / "unified_writer"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("REVISION='old'\n", encoding="utf-8")
        (pkg / "patch_install.py").write_text("raise SystemExit('shadowed')\n", encoding="utf-8")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(shadow)
        proc = subprocess.run(
            ["bash", str(PKG / "bin" / "patch-install"), "--self-check"],
            cwd=str(shadow), capture_output=True, text=True, env=env, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        body = json.loads(proc.stdout)
        self.assertEqual(body["package_revision"], "4")
        self.assertTrue(body["loaded_from"].startswith(str(PKG)))
        self.assertNotIn(str(shadow), body["loaded_from"])

    def test_poll_host_comment_path_still_matches(self):
        from unified_writer.patch_catalog import CATALOG
        from unified_writer.patch_install import apply_spec
        spec = next(item for item in CATALOG if item["id"] == "poll_ratings.py")
        src = (FIX / "work" / "poll_ratings.py").read_text(encoding="utf-8")
        host = src.replace("<CF_CRED_PATH>", "/home/box/.cf_token").replace('"<KV_NS_FILE>"', '"real-ns.txt"')
        dest = Path(tempfile.mkdtemp(prefix="poll-host-")) / "poll_ratings.py"
        dest.write_text(host, encoding="utf-8")
        self.assertEqual(apply_spec(spec, dest, False, False), "patched")
        patched = dest.read_text(encoding="utf-8")
        self.assertIn("/home/box/.cf_token", patched)
        self.assertIn("real-ns.txt", patched)
        self.assertNotIn("<CF_CRED_PATH>", patched)
        self.assertIn("guard_library_mutation", patched)
        drifted = Path(tempfile.mkdtemp(prefix="poll-drift-")) / "poll_ratings.py"
        drifted.write_text(host.replace("令牌不打印", "令牌会打印"), encoding="utf-8")
        with self.assertRaises(SystemExit) as caught:
            apply_spec(spec, drifted, False, False)
        self.assertIn("drift", str(caught.exception))

    def test_poll_and_deploy_real_entrypoints(self):
        tree = Path(tempfile.mkdtemp(prefix="entries-"))
        shutil.copytree(FIX, tree / "src")
        install(tree / "src")
        work = tree / "src" / "work"
        ops = (tree / "src" / "ops" / "harmonia_ops.py").read_text(encoding="utf-8")
        self.assertIn("make_deploy_variants.py", ops)
        self.assertIn("subprocess_lock_kwargs()", ops)
        self.assertIn("HARMONIA_UNIFIED_GUARD_v3", ops)
        secret = (work / "poll_ratings.py").read_text(encoding="utf-8")
        self.assertIn("<CF_CRED_PATH>", secret)
        self.assertIn("<KV_NS_FILE>", secret)
        self.assertIn("guard_library_mutation", secret)
        self.assertEqual((work / "ne_avail.py").read_bytes(), (FIX / "work" / "ne_avail.py").read_bytes())
        self.assertNotIn("open(", (work / "ne_avail.py").read_text(encoding="utf-8"))
        worker = tree / "src" / "worker"
        worker.mkdir()
        (worker / "<KV_NS_FILE>").write_text("ns\n", encoding="utf-8")
        (tree / "src" / "<CF_CRED_PATH>").write_text("token\n", encoding="utf-8")
        snap = work / "ratings_snapshot.json"
        snap.write_text("{}\n", encoding="utf-8")
        fixture = tree / "incoming.json"
        fixture.write_text('{"k":{"rating":1,"ts":1,"title":"t","artist":"a"}}\n', encoding="utf-8")
        lock = work / ".library.lock"
        env = base_env(
            LOCK_WAIT="0.3",
            HARMONIA_LIBRARY_LOCK=str(lock),
            HARMONIA_LOCK_ROOT=str(tree / "meta"),
            HARMONIA_POLL_FIXTURE=str(fixture),
        )
        holder = hold(lock, 30)
        try:
            time.sleep(0.05)
            blocked = subprocess.run(
                [sys.executable, str(work / "poll_ratings.py")],
                cwd=str(tree / "src"), capture_output=True, text=True, env=env, timeout=5, check=False,
            )
        finally:
            stop(holder)
        self.assertEqual(blocked.returncode, 75, blocked.stderr)
        self.assertEqual(snap.read_text(encoding="utf-8"), "{}\n")
        stale = subprocess.Popen(
            [sys.executable, str(work / "poll_ratings.py")],
            cwd=str(tree / "src"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=base_env(
                LOCK_WAIT="0.3",
                HARMONIA_LIBRARY_LOCK=str(lock),
                HARMONIA_LOCK_ROOT=str(tree / "meta"),
                HARMONIA_POLL_FIXTURE=str(fixture),
                HARMONIA_CAS_PAUSE_SEC="1.2",
            ),
        )
        time.sleep(0.4)
        snap.write_text('{"changed": true}\n', encoding="utf-8")
        _, err = stale.communicate(timeout=5)
        self.assertEqual(stale.returncode, 76, err)
        self.assertIn("cas_mismatch", err)
        self.assertIn("changed", snap.read_text(encoding="utf-8"))
        granted = subprocess.run(
            ["bash", str(SHELL), sys.executable, str(work / "poll_ratings.py")],
            cwd=str(tree / "src"), capture_output=True, text=True,
            env=base_env(
                LOCK_WAIT="0",
                HARMONIA_LIBRARY_LOCK=str(lock),
                HARMONIA_LOCK_ROOT=str(tree / "meta"),
                HARMONIA_POLL_FIXTURE=str(fixture),
            ),
            timeout=8, check=False,
        )
        self.assertEqual(granted.returncode, 0, granted.stderr)
        self.assertIn('"k"', snap.read_text(encoding="utf-8"))
        root = tree / "src"
        (work / "logo_block_20261024.html").write_text("<!-- logo20261024 start -->\n<link rel=\"icon\" href=\"/icons/a.svg\">\n<!-- logo20261024 end -->\n", encoding="utf-8")
        (root / "index.html").write_text(
            "<html>\n<head>\n</head>\n<body>https://harmonia.corgihora.bid/music/resolve</body>\n</html>\n",
            encoding="utf-8",
        )
        img = root / "img" / "0123456789ab.png"
        img.parent.mkdir()
        img.write_bytes(b"png")
        (work / "img_used.txt").write_text("img/0123456789ab.png\n", encoding="utf-8")
        out = tree / "pkg"
        held = hold(lock, 30)
        try:
            time.sleep(0.05)
            refused = subprocess.run(
                [sys.executable, str(work / "make_deploy_variants.py"), str(out)],
                cwd=str(root), capture_output=True, text=True,
                env=base_env(LOCK_WAIT="0.3", HARMONIA_LIBRARY_LOCK=str(lock), HARMONIA_LOCK_ROOT=str(tree / "meta")),
                timeout=5, check=False,
            )
        finally:
            stop(held)
        self.assertEqual(refused.returncode, 75, refused.stderr)
        self.assertFalse(out.exists())
        moved = subprocess.Popen(
            [sys.executable, str(work / "make_deploy_variants.py"), str(out)],
            cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=base_env(
                LOCK_WAIT="0.3",
                HARMONIA_LIBRARY_LOCK=str(lock),
                HARMONIA_LOCK_ROOT=str(tree / "meta"),
                HARMONIA_CAS_PAUSE_SEC="1.2",
            ),
        )
        time.sleep(0.4)
        (root / "index.html").write_text(
            "<html>\n<head>\n</head>\n<body>https://harmonia.corgihora.bid/music/resolve CHANGED</body>\n</html>\n",
            encoding="utf-8",
        )
        _, derr = moved.communicate(timeout=5)
        self.assertEqual(moved.returncode, 76, derr)
        self.assertIn("cas_mismatch", derr)
        self.assertFalse((out / "index.html").exists())
        built = subprocess.run(
            ["bash", str(SHELL), sys.executable, str(work / "make_deploy_variants.py"), str(tree / "pkg2")],
            cwd=str(root), capture_output=True, text=True,
            env=base_env(LOCK_WAIT="0", HARMONIA_LIBRARY_LOCK=str(lock), HARMONIA_LOCK_ROOT=str(tree / "meta")),
            timeout=8, check=False,
        )
        self.assertEqual(built.returncode, 0, built.stderr)
        self.assertTrue((tree / "pkg2" / "index.html").is_file())
        self.assertTrue((tree / "pkg2" / "SHA256").is_file())
        self.assertIn("harmonia.corgihora.bid/music/resolve", (tree / "pkg2" / "index.html").read_text(encoding="utf-8"))


def _queue(staged: Path, digest: str, duration: float = 3.0) -> dict:
    bvid = "BV1A2B3C4D5E"
    identity = f"bili:2221540698:{bvid}"
    return {
        "schema": "bili_hora_audio_queue_v1",
        "queue_id": identity + ":" + digest[:16],
        "identity": identity,
        "bvid": bvid,
        "media_id": 2221540698,
        "state": "pending_commit",
        "title": "live clip",
        "audio": {
            "staged_path": str(staged),
            "sha256": digest,
            "size": staged.stat().st_size,
            "duration_sec": duration,
            "extract_mode": "copy",
            "logical_dest": "/115/音乐库/歌单/youtube/live clip [BV1A2B3C4D5E].m4a",
            "container": "m4a",
            "acodec": "aac",
        },
        "writer": {"required": "harmonia-music-writer", "direct_library_write": False, "direct_publish": False, "direct_build": False},
    }


class BiliConsumerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bili-c-"))
        self.staged = self.tmp / "staged.m4a"
        self.staged.write_bytes(b"audio-bytes-v1")
        self.video = self.tmp / "temp-video.mp4"
        self.video.write_bytes(b"do-not-delete")
        self.playlist = self.tmp / "youtube.json"
        self.playlist.write_text('{"id":"youtube","name":"YouTube","songs":[]}\n', encoding="utf-8")
        self.local = self.tmp / "音乐库"
        self.receipts = self.tmp / "receipts"
        self.lock = self.tmp / "work" / ".library.lock"
        os.environ["HARMONIA_LIBRARY_LOCK"] = str(self.lock)
        os.environ["HARMONIA_LOCK_ROOT"] = str(self.tmp / "meta")
        os.environ["HARMONIA_UNIFIED_WRITER"] = str(PKG)
        os.environ["LOCK_WAIT"] = "0.3"
        os.environ.pop("BILI_CONSUMER_CRASH_AFTER", None)
        self.digest = hashlib.sha256(self.staged.read_bytes()).hexdigest()

    def tearDown(self):
        os.environ.pop("HARMONIA_LIBRARY_LOCK", None)
        os.environ.pop("LOCK_WAIT", None)
        os.environ.pop("BILI_CONSUMER_CRASH_AFTER", None)

    def _commit(self, item, **kwargs):
        from unified_writer.bili_consumer import commit_queue_item
        args = {
            "playlist_path": self.playlist,
            "local_root": self.local,
            "receipt_root": self.receipts,
            "probe": lambda path: 3.0,
        }
        args.update(kwargs)
        return commit_queue_item(item, **args)

    def test_commit_receipt_idempotent_and_keeps_video(self):
        item = _queue(self.staged, self.digest)
        first = self._commit(item)
        self.assertFalse(first["video_deleted"])
        self.assertEqual(self.video.read_bytes(), b"do-not-delete")
        body = json.loads(self.playlist.read_text(encoding="utf-8"))
        self.assertEqual(body["songs"][0]["sha256"], self.digest)
        self.assertEqual(body["songs"][0]["storage"], "local")
        self.assertTrue(body["songs"][0]["file"].startswith("/115/音乐库/"))
        self.assertIs(first["receipt"]["box_playable"], False)
        self.assertIs(first["receipt"]["local_matches"], True)
        self.assertIs(first["receipt"]["video_delete"]["pipeline_may_delete_temp_video"], False)
        self.assertFalse(first["receipt"]["library_json_written"])
        self.assertEqual(first["receipt"]["commit_scope"], "local_playlist")
        self.assertNotIn("served_playlist_verified", json.dumps(first["receipt"]))
        self.assertIs(first["receipt"]["playback"]["local_matches"], True)
        self.assertEqual(first["receipt"]["playback"]["alist_path"], item["audio"]["logical_dest"])
        self.assertFalse(first["receipt"]["playback"]["resolve_checked"])
        self.assertFalse(first["receipt"]["video_delete"]["performed"])
        self.assertFalse((self.tmp / "library.json").exists())
        placed = self.local / "歌单" / "youtube" / "live clip [BV1A2B3C4D5E].m4a"
        self.assertEqual(placed.read_bytes(), b"audio-bytes-v1")
        before = self.playlist.read_bytes()
        second = self._commit(item)
        self.assertEqual(second["receipt"], first["receipt"])
        self.assertEqual(self.playlist.read_bytes(), before)
        self.assertEqual(len(list(self.receipts.glob("*.json"))), 1)

    def test_served_scope_requires_playlist_ledger_and_playback(self):
        item = _queue(self.staged, self.digest)
        ledger = self.tmp / "ledger.jsonl"
        proved = self._commit(item, served_playlist=self.playlist, ledger_path=ledger)
        receipt = proved["receipt"]
        self.assertEqual(receipt["commit_scope"], "served_playlist_verified")
        self.assertIs(receipt["box_playable"], True)
        self.assertIs(receipt["local_matches"], True)
        self.assertIs(receipt["video_delete"]["pipeline_may_delete_temp_video"], True)
        self.assertEqual(receipt["queue_id"], item["queue_id"])
        self.assertEqual(receipt["identity"], item["identity"])
        self.assertEqual(receipt["audio"]["sha256"], self.digest)
        self.assertEqual(receipt["audio"]["size"], self.staged.stat().st_size)
        self.assertEqual(receipt["audio"]["duration_sec"], 3.0)
        encoded = json.dumps(receipt)
        self.assertIn('"box_playable": true', encoded)
        self.assertIn('"local_matches": true', encoded)
        self.assertNotIn('"true"', encoded)
        self.assertEqual(self.video.read_bytes(), b"do-not-delete")
        again = self._commit(item, served_playlist=self.playlist, ledger_path=ledger)
        self.assertEqual(again["receipt"], receipt)
        side = self.tmp / "not-served.json"
        side.write_text('{"id":"youtube","songs":[]}\n', encoding="utf-8")
        other = self.tmp / "other-receipts"
        held = self._commit(item, served_playlist=side, ledger_path=ledger, receipt_root=other)
        self.assertEqual(held["receipt"]["commit_scope"], "local_playlist")
        self.assertIs(held["receipt"]["box_playable"], False)
        self.assertNotIn("served_playlist_verified", json.dumps(held["receipt"]))
        self.assertEqual(self.video.read_bytes(), b"do-not-delete")

    def test_mismatch_conflict_crash_and_missing_publisher(self):
        from unified_writer.bili_consumer import ConsumerError
        bad = _queue(self.staged, self.digest)
        bad["audio"]["sha256"] = "0" * 64
        bad["queue_id"] = bad["identity"] + ":" + ("0" * 16)
        with self.assertRaises(ConsumerError) as mismatch:
            self._commit(bad)
        self.assertEqual(mismatch.exception.code, "sha256_mismatch")
        self.assertFalse(any(self.receipts.glob("*.json")))
        self.staged.write_bytes(b"audio-bytes-v1")
        dest = self.local / "歌单" / "youtube" / "live clip [BV1A2B3C4D5E].m4a"
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"other-audio")
        with self.assertRaises(ConsumerError) as conflict:
            self._commit(_queue(self.staged, self.digest))
        self.assertEqual(conflict.exception.code, "dest_conflict")
        self.assertFalse(any(self.receipts.glob("*.json")))
        dest.unlink()
        os.environ["BILI_CONSUMER_CRASH_AFTER"] = "commit"
        with self.assertRaises(SystemExit):
            self._commit(_queue(self.staged, self.digest))
        self.assertFalse(any(self.receipts.glob("*.json")))
        self.assertEqual(len(json.loads(self.playlist.read_text(encoding="utf-8"))["songs"]), 1)
        os.environ.pop("BILI_CONSUMER_CRASH_AFTER")
        recovered = self._commit(_queue(self.staged, self.digest))
        self.assertTrue(recovered["ok"])
        self.assertEqual(len(json.loads(self.playlist.read_text(encoding="utf-8"))["songs"]), 1)
        needs = _queue(self.staged, self.digest)
        needs["writer"] = dict(needs["writer"], direct_publish=True)
        self.playlist.write_text('{"id":"youtube","name":"YouTube","songs":[]}\n', encoding="utf-8")
        if dest.exists():
            dest.unlink()
        with self.assertRaises(ConsumerError) as missing:
            self._commit(needs)
        self.assertEqual(missing.exception.code, "publisher_missing")
        self.assertEqual(json.loads(self.playlist.read_text(encoding="utf-8"))["songs"], [])

    def test_cas_and_lock_conflict_write_nothing(self):
        from unified_writer.bili_consumer import ConsumerError
        item = _queue(self.staged, self.digest)
        with self.assertRaises(ConsumerError) as cas:
            self._commit(item, expected_playlist_sha="0" * 64)
        self.assertEqual(cas.exception.code, "cas_mismatch")
        self.assertFalse(any(self.receipts.glob("*.json")))
        holder = hold(self.lock, 30)
        try:
            time.sleep(0.05)
            with self.assertRaises(SystemExit) as busy:
                self._commit(item)
        finally:
            stop(holder)
        self.assertEqual(busy.exception.code, 75)
        self.assertEqual(json.loads(self.playlist.read_text(encoding="utf-8"))["songs"], [])
        self.assertEqual(self.video.read_bytes(), b"do-not-delete")


if __name__ == "__main__":
    raise SystemExit(0 if unittest.main(verbosity=2, exit=False).result.wasSuccessful() else 1)
