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

    def test_unpatched_tools_are_not_claimed_live(self):
        from unified_writer.prod_guard import run_unpatched
        root = Path(tempfile.mkdtemp(prefix="unpatched-"))
        lock = root / "work" / ".library.lock"
        os.environ["HARMONIA_LIBRARY_LOCK"] = str(lock)
        os.environ["HARMONIA_LOCK_ROOT"] = str(root / "meta")
        os.environ["LOCK_WAIT"] = "0.3"
        os.environ["HARMONIA_UNIFIED_WRITER"] = str(PKG)
        reader = root / "read.py"
        reader.write_text("print('read-ok')\n", encoding="utf-8")
        holder = hold(lock, 30)
        try:
            time.sleep(0.05)
            read = run_unpatched("poll_ratings.py", [sys.executable, str(reader)])
            self.assertEqual(read["returncode"], 0)
            self.assertFalse(read["internally_guarded"])
            self.assertEqual(read["lock"], "unchecked_read")
            writer = root / "write.py"
            marker = root / "wrote"
            writer.write_text(f"open({str(marker)!r},'w').write('x')\n", encoding="utf-8")
            with self.assertRaises(SystemExit) as caught:
                run_unpatched("make_deploy_variants.py", [sys.executable, str(writer)])
        finally:
            stop(holder)
            os.environ.pop("HARMONIA_LIBRARY_LOCK", None)
            os.environ.pop("LOCK_WAIT", None)
        self.assertEqual(caught.exception.code, 75)
        self.assertFalse(marker.exists())


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
        return commit_queue_item(
            item,
            playlist_path=self.playlist,
            local_root=self.local,
            receipt_root=self.receipts,
            probe=lambda path: 3.0,
            **kwargs,
        )

    def test_commit_receipt_idempotent_and_keeps_video(self):
        item = _queue(self.staged, self.digest)
        first = self._commit(item)
        self.assertFalse(first["video_deleted"])
        self.assertEqual(self.video.read_bytes(), b"do-not-delete")
        body = json.loads(self.playlist.read_text(encoding="utf-8"))
        self.assertEqual(body["songs"][0]["sha256"], self.digest)
        self.assertEqual(body["songs"][0]["storage"], "local")
        self.assertTrue(body["songs"][0]["file"].startswith("/115/音乐库/"))
        placed = self.local / "歌单" / "youtube" / "live clip [BV1A2B3C4D5E].m4a"
        self.assertEqual(placed.read_bytes(), b"audio-bytes-v1")
        before = self.playlist.read_bytes()
        second = self._commit(item)
        self.assertEqual(second["receipt"], first["receipt"])
        self.assertEqual(self.playlist.read_bytes(), before)
        self.assertEqual(len(list(self.receipts.glob("*.json"))), 1)

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
