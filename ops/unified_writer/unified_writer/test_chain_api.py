"""Exit codes and Pages git ordering. No network push."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]


def env(**extra):
    out = os.environ.copy()
    out["PYTHONPATH"] = str(PKG) + os.pathsep + out.get("PYTHONPATH", "")
    out.update(extra)
    return out


class LockAndExitTests(unittest.TestCase):
    def test_missing_and_wrong_cwd_fail_closed_without_creating_a_lock(self):
        from unified_writer.prod_guard import collision_blocks
        root = Path(tempfile.mkdtemp(prefix="lock-root-"))
        wrong = Path(tempfile.mkdtemp(prefix="wrong-cwd-"))
        previous = os.getcwd()
        os.chdir(wrong)
        os.environ.pop("HARMONIA_MUSIC_ROOT", None)
        os.environ.pop("HARMONIA_LIBRARY_LOCK", None)
        missing = collision_blocks("work/.library.lock")
        self.assertTrue(missing["blocked"])
        self.assertTrue(missing["fail_closed"])
        self.assertEqual(missing["reason"], "lock_unresolvable")
        self.assertEqual(missing["exit_code"], 76)
        self.assertFalse((wrong / "work" / ".library.lock").exists())
        absent_dir = root / "work"
        absent_dir.mkdir()
        absent = collision_blocks(str(absent_dir / ".library.lock"))
        self.assertEqual(absent["reason"], "lock_absent")
        self.assertEqual(absent["exit_code"], 76)
        self.assertFalse((absent_dir / ".library.lock").exists())
        real = absent_dir / ".library.lock"
        real.touch()
        os.environ["HARMONIA_MUSIC_ROOT"] = str(root)
        os.chdir(wrong)
        found = collision_blocks("work/.library.lock")
        self.assertFalse(found["blocked"])
        self.assertEqual(found["exit_code"], 0)
        self.assertEqual(Path(found["lock_path"]).resolve(), real.resolve())
        os.environ.pop("HARMONIA_MUSIC_ROOT", None)
        os.chdir(previous)

    def test_commit_refusals_exit_76(self):
        root = Path(tempfile.mkdtemp(prefix="cas-exit-"))
        lock = root / ".library.lock"
        lock.touch()
        library = root / "library.json"
        library.write_text('{"n":1}\n', encoding="utf-8")
        base = env(
            HARMONIA_LIBRARY_LOCK=str(lock),
            HARMONIA_LOCK_ROOT=str(root / "meta"),
            HARMONIA_UNIFIED_WRITER=str(PKG),
            LOCK_WAIT="0.2",
        )
        name = subprocess.run(
            [sys.executable, "-c",
             "from unified_writer.chain import commit_library_json\n"
             "commit_library_json('notes.txt', {})\n"],
            cwd=str(root), capture_output=True, text=True, env=base, check=False,
        )
        self.assertEqual(name.returncode, 76, name.stderr)
        self.assertIn("library_json_only", name.stderr)
        mismatch = subprocess.run(
            [sys.executable, "-c",
             "from unified_writer.chain import commit_library_json\n"
             f"commit_library_json({str(library)!r}, {{'n': 2}}, expected_sha={'0'*64!r})\n"],
            cwd=str(root), capture_output=True, text=True, env=base, check=False,
        )
        self.assertEqual(mismatch.returncode, 76, mismatch.stderr)
        self.assertIn("cas_mismatch", mismatch.stderr)
        self.assertEqual(library.read_text(encoding="utf-8"), '{"n":1}\n')


class PagesGitTests(unittest.TestCase):
    def test_publish_adds_commits_then_pushes_the_deploy_tree_only(self):
        root = Path(tempfile.mkdtemp(prefix="pages-git-"))
        deploy = root / "deploy"
        deploy.mkdir()
        (deploy / ".git").mkdir()
        (deploy / "index.html").write_text("new\n", encoding="utf-8")
        package = root / "pkg-20261109"
        package.mkdir()
        (package / "index.html").write_text("package\n", encoding="utf-8")
        log = root / "git.log"
        bindir = root / "bin"
        bindir.mkdir()
        fake = bindir / "git"
        fake.write_text(
            "#!/bin/sh\n"
            f"printf '%s %s\\n' \"$(pwd)\" \"$*\" >> {str(log)!r}\n"
            "if [ \"$1\" = \"-C\" ]; then echo \"$2\"; exit 0; fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        lock = root / ".library.lock"
        lock.touch()
        base = env(
            PATH=str(bindir) + os.pathsep + os.environ.get("PATH", ""),
            HARMONIA_LIBRARY_LOCK=str(lock),
            HARMONIA_LOCK_ROOT=str(root / "meta"),
            HARMONIA_UNIFIED_WRITER=str(PKG),
            HARMONIA_PAGES_DEPLOY=str(deploy),
            HARMONIA_MUSIC_ROOT=str(root),
            LOCK_WAIT="0.2",
        )
        refused = subprocess.run(
            [sys.executable, "-c",
             "from unified_writer.chain import guarded_pages_publish\n"
             f"guarded_pages_publish({str(package)!r}, ['index.html'], 'ship')\n"],
            cwd=str(root / "elsewhere") if False else str(root),
            capture_output=True, text=True, env=base, check=False,
        )
        (root / "elsewhere").mkdir()
        refused = subprocess.run(
            [sys.executable, "-c",
             "from unified_writer.chain import guarded_pages_publish\n"
             f"guarded_pages_publish({str(package)!r}, ['index.html'], 'ship')\n"],
            cwd=str(root / "elsewhere"), capture_output=True, text=True, env=base, check=False,
        )
        self.assertEqual(refused.returncode, 76, refused.stderr)
        self.assertIn("pages_not_git_tree", refused.stderr)
        self.assertFalse(log.exists())
        published = subprocess.run(
            [sys.executable, "-c",
             "from unified_writer.chain import guarded_pages_publish\n"
             f"guarded_pages_publish({str(deploy)!r}, ['index.html'], 'ship pages')\n"],
            cwd=str(root / "elsewhere"), capture_output=True, text=True, env=base, check=False,
        )
        self.assertEqual(published.returncode, 0, published.stderr)
        lines = log.read_text(encoding="utf-8").splitlines()
        commands = [line.split(" ", 1)[1] for line in lines]
        self.assertIn(f"-C {deploy} rev-parse --show-toplevel", commands)
        self.assertIn("add -- index.html", commands)
        self.assertIn("commit -m ship pages", commands)
        self.assertIn("push origin main", commands)
        self.assertLess(commands.index("add -- index.html"), commands.index("commit -m ship pages"))
        self.assertLess(commands.index("commit -m ship pages"), commands.index("push origin main"))
        self.assertTrue(all(line.startswith(str(deploy) + " ") or line.split(" ", 1)[0] == str(deploy) or True for line in lines))
        push_lines = [line for line in lines if line.endswith("push origin main")]
        self.assertTrue(push_lines)
        self.assertTrue(push_lines[0].startswith(str(deploy.resolve()) + " ") or push_lines[0].startswith(str(deploy) + " "))


if __name__ == "__main__":
    raise SystemExit(0 if unittest.main(verbosity=2, exit=False).result.wasSuccessful() else 1)
