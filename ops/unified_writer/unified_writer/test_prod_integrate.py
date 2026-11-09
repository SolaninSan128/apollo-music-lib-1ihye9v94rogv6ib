"""Patched production entrypoints, with stub transport. No live publish."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
FIX = PKG / "fixtures" / "prod"


def env(**extra):
    base = os.environ.copy()
    base["PYTHONPATH"] = str(PKG) + os.pathsep + base.get("PYTHONPATH", "")
    base["HARMONIA_UNIFIED_WRITER"] = str(PKG)
    base["LOCK_WAIT"] = "0.4"
    base["HARMONIA_PUBLISH_TRANSPORT"] = "local"
    base.pop("HARMONIA_TARGET", None)
    base.update(extra)
    return base


def spawn_flock(lock: Path):
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.touch()
    return subprocess.Popen(
        ["flock", str(lock), "sleep", "60"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def stop(proc):
    if proc.poll() is None:
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait(timeout=5)


def install(dest: Path):
    proc = subprocess.run(
        [sys.executable, "-m", "unified_writer.patch_install",
         "--root", str(dest), "--layout", "fixture", "--fixture"],
        cwd=str(PKG),
        capture_output=True,
        text=True,
        env=env(),
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr + proc.stdout)
    return json.loads(proc.stdout)


class ProdIntegrateTests(unittest.TestCase):
    def tearDown(self):
        for name in ("harmonia_target", "run_all_queues", "pub_remote", "harmonia_publish", "local_audio"):
            sys.modules.pop(name, None)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="prod-int-"))
        self.tree = self.tmp / "tree"
        shutil.copytree(FIX, self.tree)
        install(self.tree)

    def test_install_is_idempotent_and_refuses_drift(self):
        again = install(self.tree)
        self.assertTrue(all(item["status"] == "already" for item in again["files"]))
        self.assertFalse(again["ready_for_live"])
        drifted = self.tmp / "drift"
        shutil.copytree(FIX, drifted)
        target = drifted / "work" / "apply_ratings.py"
        target.write_text(target.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-m", "unified_writer.patch_install",
             "--root", str(drifted), "--layout", "fixture", "--fixture"],
            cwd=str(PKG), capture_output=True, text=True, env=env(), check=False,
        )
        self.assertEqual(proc.returncode, 76, proc.stderr)
        self.assertIn("drift", proc.stderr)
        redacted = self.tmp / "src-only"
        redacted.mkdir()
        (redacted / "work").mkdir()
        shutil.copy(FIX / "work" / "run_all_queues.py", redacted / "work" / "run_all_queues.py")
        # pack hash is not the on-host hash; without --fixture this must fail
        from unified_writer.patch_catalog import CATALOG
        from unified_writer.patch_install import apply_spec
        spec = next(item for item in CATALOG if item["id"] == "run_all_queues.py")
        with self.assertRaises(SystemExit) as caught:
            apply_spec(spec, redacted / "work" / "run_all_queues.py", fixture=False, check_only=True)
        self.assertIn("drift", str(caught.exception))

    def test_apply_ratings_blocks_before_write_and_does_not_main_publish(self):
        work = self.tree / "work"
        (work / "poll_ratings.py").write_text(
            "LABEL={1:'收藏',2:'还不错',3:'没感觉',None:'未评'}\n"
            "def fetch_all():\n    return {}\n",
            encoding="utf-8",
        )
        (work / "play_fields.py").write_text(
            "def copy_play_fields(a,b):\n    return None\n"
            "def fill_missing_files(lib, fk, extra=False):\n    return [], None\n",
            encoding="utf-8",
        )
        library = self.tree / "library.json"
        library.write_text('{"daily_batches":[],"rated":[],"playlists":[]}\n', encoding="utf-8")
        original = library.read_text(encoding="utf-8")
        text = (work / "apply_ratings.py").read_text(encoding="utf-8")
        self.assertNotIn("arm_site_publish", text)
        self.assertNotIn("harmonia_publish", text)
        lock = work / ".library.lock"
        holder = spawn_flock(lock)
        try:
            time.sleep(0.05)
            blocked = subprocess.run(
                [sys.executable, str(work / "apply_ratings.py"), "--publish"],
                cwd=str(self.tree), capture_output=True, text=True, env=env(), check=False,
            )
        finally:
            stop(holder)
        self.assertEqual(blocked.returncode, 75, blocked.stderr)
        self.assertFalse((work / "ratings_snapshot.json").exists())
        self.assertEqual(library.read_text(encoding="utf-8"), original)
        allowed = subprocess.run(
            [sys.executable, str(work / "apply_ratings.py")],
            cwd=str(self.tree), capture_output=True, text=True, env=env(), check=False,
        )
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertTrue((work / "ratings_snapshot.json").exists())
        self.assertIn("无需变化", allowed.stdout)

    def test_build_blocks_before_index_and_keeps_resolve_host(self):
        root = self.tree / "music-library"
        (root / "work").mkdir(exist_ok=True)
        (root / "img").mkdir(exist_ok=True)
        (root / "work" / "img_split.py").write_text(
            "def publish(data, kind='cover'):\n    return 'img/stub.jpg', None\n"
            "def save_cache():\n    pass\n",
            encoding="utf-8",
        )
        (root / "library.json").write_text(
            '{"playlists":[{"songs":[]}],"rated":[],"daily_batches":[]}\n',
            encoding="utf-8",
        )
        (root / "template.html").write_text(
            'var PLAY_RESOLVE="https://harmonia.corgihora.bid/music/resolve";\n'
            '/*__DATA__*/null\n/*__FEED_TOKEN__*/""\n',
            encoding="utf-8",
        )
        lock = root / "work" / ".library.lock"
        holder = spawn_flock(lock)
        try:
            time.sleep(0.05)
            blocked = subprocess.run(
                [sys.executable, str(root / "build.py")],
                cwd=str(root), capture_output=True, text=True, env=env(), check=False,
            )
        finally:
            stop(holder)
        self.assertEqual(blocked.returncode, 75, blocked.stderr)
        self.assertFalse((root / "index.html").exists())
        allowed = subprocess.run(
            [sys.executable, str(root / "build.py")],
            cwd=str(root), capture_output=True, text=True, env=env(), check=False,
        )
        self.assertEqual(allowed.returncode, 0, allowed.stderr + allowed.stdout)
        html = (root / "index.html").read_text(encoding="utf-8")
        self.assertIn("https://harmonia.corgihora.bid/music/resolve", html)

    def test_dynamic_script_imports_queue_module(self):
        work = self.tree / "work"
        (work / "harmonia_target.py").write_text(
            "def is_hora():\n    return False\n"
            "def hctl(*a, **k):\n    raise RuntimeError('stub-transport')\n",
            encoding="utf-8",
        )
        library = self.tree / "library.json"
        library.write_text('{"v":1}\n', encoding="utf-8")
        before = library.read_bytes()
        script = work / "ingest_2026_11_09.py"
        script.write_text(textwrap.dedent("""
            import json, sys
            import run_all_queues as rq
            lib = json.loads(open(sys.argv[1], encoding="utf-8").read())
            lib["v"] = 2
            print(rq.commit_library_json(sys.argv[1], lib))
        """), encoding="utf-8")
        lock = work / ".library.lock"
        holder = spawn_flock(lock)
        try:
            time.sleep(0.05)
            blocked = subprocess.run(
                [sys.executable, str(script), str(library)],
                cwd=str(work), capture_output=True, text=True, env=env(), check=False,
            )
        finally:
            stop(holder)
        self.assertEqual(blocked.returncode, 75, blocked.stderr)
        self.assertEqual(library.read_bytes(), before)
        allowed = subprocess.run(
            [sys.executable, str(script), str(library)],
            cwd=str(work), capture_output=True, text=True, env=env(), check=False,
        )
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertEqual(json.loads(library.read_text(encoding="utf-8"))["v"], 2)
        progress = work / "DAILY_2026-11-09_PROGRESS.json"
        sys.path.insert(0, str(work))
        import importlib
        queues = importlib.import_module("run_all_queues")
        queues.save_progress("daily", "/115/音乐库/日推/2026-11-09", [], progress)
        self.assertTrue(progress.exists())
        with self.assertRaises(SystemExit):
            queues.save_progress("daily", "/115", [], library)

    def test_publish_claim_before_index_replace(self):
        publish = self.tree / "publish"
        remote = __import__("importlib").import_module
        sys.path.insert(0, str(publish))
        pub = remote("pub_remote")
        web = self.tmp / "web"
        (web / "img").mkdir(parents=True)
        (web / "index.html").write_text("old-index", encoding="utf-8")
        staging = self.tmp / "staging"
        staging.mkdir()
        (staging / "index.html").write_text("new-index", encoding="utf-8")
        tar_path = staging / "img.tar.gz"
        with tarfile.open(tar_path, "w:gz"):
            pass
        def sha(path):
            return hashlib.sha256(path.read_bytes()).hexdigest()
        (staging / "SHA256").write_text(
            f"{sha(staging / 'index.html')}  index.html\n{sha(tar_path)}  img.tar.gz\n",
            encoding="utf-8",
        )
        (staging / "IMG_SHA256").write_text("", encoding="utf-8")
        with self.assertRaises(SystemExit) as blocked:
            pub.publish(str(staging), str(web), "t1")
        self.assertEqual(blocked.exception.code, "publish_claim_missing")
        self.assertEqual((web / "index.html").read_text(encoding="utf-8"), "old-index")
        from unified_writer.winclaim import acquire_claim
        parent = self.tmp / "win"
        meta = acquire_claim(
            parent, token="tok", lease_sec=60, hostname="test", pid=os.getpid(),
            who="publish", cmd="test", resource_cas=sha(staging / "index.html"),
            cas_file=str(staging / "index.html"), wait_sec=0.2, remote_skew_sec=0,
        )
        claim = {
            "guard": "HARMONIA_UNIFIED_GUARD_v1",
            "contract_version": 1,
            "owner_token": meta["owner_token"],
            "fence": meta["fence"],
            "resource_cas": sha(staging / "index.html"),
            "claim_dir": str(parent / "publish.claim.d"),
            "lease_until": meta["lease_until"],
        }
        (staging / "PUBLISH_CLAIM.json").write_text(json.dumps(claim), encoding="utf-8")
        pub.publish(str(staging), str(web), "t2")
        self.assertEqual((web / "index.html").read_text(encoding="utf-8"), "new-index")

    def test_harmonia_publish_does_not_ssh_while_library_lock_is_held(self):
        publish = self.tree / "publish"
        calls = []
        sys.path.insert(0, str(publish))
        import harmonia_publish as hp
        import harmonia_target as target
        def fake_ssh(cmd, timeout=60, check=True, capture=True):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="stub", stderr="")
        target._ssh = fake_ssh
        target._scp = lambda *args, **kwargs: calls.append(("scp", args))
        pkg = self.tmp / "pkg"
        pkg.mkdir()
        (pkg / "index.html").write_text("page", encoding="utf-8")
        tar_path = pkg / "img.tar.gz"
        with tarfile.open(tar_path, "w:gz"):
            pass
        def sha(path):
            return hashlib.sha256(path.read_bytes()).hexdigest()
        (pkg / "SHA256").write_text(
            f"{sha(pkg / 'index.html')}  index.html\n{sha(tar_path)}  img.tar.gz\n",
            encoding="utf-8",
        )
        (pkg / "IMG_SHA256").write_text("", encoding="utf-8")
        lock = self.tmp / "work" / ".library.lock"
        saved = {key: os.environ.get(key) for key in ("HARMONIA_LIBRARY_LOCK", "HARMONIA_LOCK_ROOT", "LOCK_WAIT")}
        os.environ["HARMONIA_LIBRARY_LOCK"] = str(lock)
        os.environ["HARMONIA_LOCK_ROOT"] = str(self.tmp / "lockroot")
        os.environ["LOCK_WAIT"] = "0.4"
        holder = spawn_flock(lock)
        try:
            time.sleep(0.05)
            old_argv = sys.argv
            sys.argv = ["harmonia_publish.py", str(pkg), "--execute"]
            try:
                with self.assertRaises(SystemExit) as caught:
                    hp.main()
            finally:
                sys.argv = old_argv
        finally:
            stop(holder)
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.assertEqual(caught.exception.code, 75)
        self.assertEqual(calls, [])
        self.assertFalse((pkg / "PUBLISH_CLAIM.json").exists())

    def test_collision_ignores_mtime_and_audio_place_refuses_library_name(self):
        from unified_writer.prod_guard import collision_blocks
        lock = self.tmp / "work" / ".library.lock"
        library = self.tmp / "library.json"
        library.write_text("{}\n", encoding="utf-8")
        os.utime(library, None)
        self.assertFalse(collision_blocks(str(lock))["blocked"])
        self.assertFalse(collision_blocks(str(lock))["heuristic_mtime"])
        holder = spawn_flock(lock)
        try:
            time.sleep(0.05)
            state = collision_blocks(str(lock))
        finally:
            stop(holder)
        self.assertTrue(state["blocked"])
        self.assertEqual(state["reason"], "lock_held")
        sys.path.insert(0, str(self.tree / "music-dl"))
        import local_audio
        local_root = self.tmp / "audio"
        os.environ["HARMONIA_LOCAL_ROOT"] = str(local_root)
        os.environ["HARMONIA_LOCAL_LEDGER"] = str(self.tmp / "ledger.jsonl")
        local_audio.LOCAL_ROOT = str(local_root)
        local_audio.LEDGER = str(self.tmp / "ledger.jsonl")
        src = self.tmp / "song.bin"
        src.write_bytes(b"flac-bytes")
        placed = local_audio.place_local(str(src), "/115/音乐库/日推/2026-11-09", "song.flac")
        self.assertTrue(placed["ok"])
        with self.assertRaises(SystemExit):
            local_audio.place_local(str(src), "/115/音乐库/日推/2026-11-09", "library.json")
        self.assertFalse((local_root / "日推" / "2026-11-09" / "library.json").exists())

    def test_force_push_and_chain_cas(self):
        from unified_writer.chain import LibraryChain, run_guarded
        with self.assertRaises(SystemExit):
            run_guarded(["git", "push", "--force", "origin", "main"])
        library = self.tmp / "library.json"
        library.write_text('{"v":1}\n', encoding="utf-8")
        lock_parent = self.tmp / "work"
        lock_parent.mkdir()
        with LibraryChain("daily_ingest", str(library)) as chain:
            chain._sha = "0" * 64
            with self.assertRaises(SystemExit) as caught:
                chain.commit_json(library, {"v": 2})
        self.assertEqual(str(caught.exception), "cas_mismatch")
        self.assertEqual(json.loads(library.read_text(encoding="utf-8"))["v"], 1)


if __name__ == "__main__":
    raise SystemExit(0 if unittest.main(verbosity=2, exit=False).result.wasSuccessful() else 1)
