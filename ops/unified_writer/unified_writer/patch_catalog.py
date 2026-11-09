"""Precise anchors for the production scripts.

Each old string is away from redacted secret lines. The installer checks the
on-host sha256 before replacing anything, and refuses a drifted file.
Placeholders are never written back over host secrets.
"""
from __future__ import annotations

GUARD = "HARMONIA_UNIFIED_GUARD_v1"

CATALOG = [
    {
        "id": "apply_ratings.py",
        "src_sha256": "47305076feff6a68ed5db98c331d2aa988445b8d61430395803cdc5c1a698b4b",
        "pack_sha256": "47305076feff6a68ed5db98c331d2aa988445b8d61430395803cdc5c1a698b4b",
        "fixture": "work/apply_ratings.py",
        "host": "work/apply_ratings.py",
        "root": "music",
        "anchors": [
            {
                "old": "    if dry: return\n    open(os.path.join(HERE,\"taste_digest.txt\"),\"w\",encoding=\"utf-8\").write(digest+\"\\n\")\n",
                "new": (
                    "    if dry: return\n"
                    "    import sys as _pg_sys, os as _pg_os\n"
                    "    _u = _pg_os.environ.get(\"HARMONIA_UNIFIED_WRITER\")\n"
                    "    if _u and _u not in _pg_sys.path:\n"
                    "        _pg_sys.path.insert(0, _u)\n"
                    "    _ops = _pg_os.path.join(HERE, \"ops\")\n"
                    "    if _ops not in _pg_sys.path:\n"
                    "        _pg_sys.path.insert(0, _ops)\n"
                    "    import unified_writer.prod_guard as _pg\n"
                    "    _pg.guard_library_mutation(\"ratings\", LIBP)  # HARMONIA_UNIFIED_GUARD_v1\n"
                    "    open(os.path.join(HERE,\"taste_digest.txt\"),\"w\",encoding=\"utf-8\").write(digest+\"\\n\")\n"
                ),
            },
            {
                "old": "            subprocess.check_call([\"git\",\"push\",\"origin\",\"main\"],cwd=DEPLOY)\n",
                "new": "            _pg.ordinary_git_push(DEPLOY)  # HARMONIA_UNIFIED_GUARD_v1\n",
            },
        ],
    },
    {
        "id": "build.py",
        "src_sha256": "cabeff9e69e4b2617c36c14e3923ec23f9edd189fb6608e789802ced36212806",
        "pack_sha256": "cabeff9e69e4b2617c36c14e3923ec23f9edd189fb6608e789802ced36212806",
        "fixture": "music-library/build.py",
        "host": "build.py",
        "root": "music",
        "anchors": [
            {
                "old": (
                    "    img_split.save_cache()\n"
                    "    if changed:\n"
                    "        json.dump(lib, open(LIB, \"w\", encoding=\"utf-8\"), ensure_ascii=False, indent=2)\n"
                    "    tpl = open(os.path.join(HERE, \"template.html\"), encoding=\"utf-8\").read()\n"
                ),
                "new": (
                    "    img_split.save_cache()\n"
                    "    import sys as _pg_sys, os as _pg_os\n"
                    "    _u = _pg_os.environ.get(\"HARMONIA_UNIFIED_WRITER\")\n"
                    "    if _u and _u not in _pg_sys.path:\n"
                    "        _pg_sys.path.insert(0, _u)\n"
                    "    _ops = _pg_os.path.join(HERE, \"work\", \"ops\")\n"
                    "    if _ops not in _pg_sys.path:\n"
                    "        _pg_sys.path.insert(0, _ops)\n"
                    "    import unified_writer.prod_guard as _pg\n"
                    "    _pg.guard_library_mutation(\"build_push\", LIB)  # HARMONIA_UNIFIED_GUARD_v1\n"
                    "    if changed:\n"
                    "        json.dump(lib, open(LIB, \"w\", encoding=\"utf-8\"), ensure_ascii=False, indent=2)\n"
                    "    tpl = open(os.path.join(HERE, \"template.html\"), encoding=\"utf-8\").read()\n"
                ),
            },
        ],
    },
    {
        "id": "run_all_queues.py",
        "src_sha256": "70a9237b3b05a9d6221d41c41d563c543a7bba9ef50c0b04d65abda97b717fa7",
        "pack_sha256": "d11515112d6aa7cf31aea8e0c7b677930d13e7ff6bd4909d4585b34e013162b7",
        "fixture": "work/run_all_queues.py",
        "host": "work/run_all_queues.py",
        "root": "music",
        "anchors": [
            {
                "old": "def save_progress(batch: str, dest: str, results: list, path: Path):\n    path.write_text(json.dumps({\n",
                "new": (
                    "def save_progress(batch: str, dest: str, results: list, path: Path):\n"
                    "    if Path(path).name.lower() in (\"library.json\", \"index.html\", \"index_pages.html\"):\n"
                    "        raise SystemExit(\"HARMONIA_UNIFIED_GUARD_v1 published_target_refused\")\n"
                    "    path.write_text(json.dumps({\n"
                ),
            },
            {
                "old": (
                    "    def kill_remote(song_id: int):\n"
                    "        try: T.hctl(\"kill_dl\", timeout=60, song_id=song_id)\n"
                    "        except Exception: pass\n"
                    "\n"
                    "if __name__ == \"__main__\":\n"
                    "    main()\n"
                ),
                "new": (
                    "    def kill_remote(song_id: int):\n"
                    "        try: T.hctl(\"kill_dl\", timeout=60, song_id=song_id)\n"
                    "        except Exception: pass\n"
                    "\n"
                    "def commit_library_json(path, obj, expected_sha=None):\n"
                    "    \"\"\"Date-specific scripts call this after `import run_all_queues`. HARMONIA_UNIFIED_GUARD_v1\"\"\"\n"
                    "    import os as _os, sys as _sys\n"
                    "    _u = _os.environ.get(\"HARMONIA_UNIFIED_WRITER\")\n"
                    "    if _u and _u not in _sys.path:\n"
                    "        _sys.path.insert(0, _u)\n"
                    "    _ops = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), \"ops\")\n"
                    "    if _ops not in _sys.path:\n"
                    "        _sys.path.insert(0, _ops)\n"
                    "    from unified_writer.chain import commit_library_json as _impl\n"
                    "    return _impl(path, obj, expected_sha=expected_sha)\n"
                    "\n"
                    "def guarded_build(argv=None):\n"
                    "    \"\"\"build.py under the library lock. HARMONIA_UNIFIED_GUARD_v1\"\"\"\n"
                    "    import os as _os, sys as _sys\n"
                    "    _u = _os.environ.get(\"HARMONIA_UNIFIED_WRITER\")\n"
                    "    if _u and _u not in _sys.path:\n"
                    "        _sys.path.insert(0, _u)\n"
                    "    from unified_writer.chain import run_guarded\n"
                    "    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))\n"
                    "    return run_guarded(argv or [_sys.executable, _os.path.join(root, \"build.py\")], cwd=root)\n"
                    "\n"
                    "def guarded_pages_push(deploy_dir):\n"
                    "    \"\"\"Ordinary Pages push. Not the hora main-site publish. HARMONIA_UNIFIED_GUARD_v1\"\"\"\n"
                    "    import os as _os, sys as _sys\n"
                    "    _u = _os.environ.get(\"HARMONIA_UNIFIED_WRITER\")\n"
                    "    if _u and _u not in _sys.path:\n"
                    "        _sys.path.insert(0, _u)\n"
                    "    from unified_writer.prod_guard import guard_library_mutation, ordinary_git_push\n"
                    "    guard_library_mutation(\"build_push\", None)\n"
                    "    ordinary_git_push(deploy_dir)\n"
                    "\n"
                    "if __name__ == \"__main__\":\n    main()\n"
                ),
            },
        ],
    },
    {
        "id": "harmonia_ops.py",
        "src_sha256": "6281e6db4c1308d0904c3664a724fd2a69c134d063effd51b8eaa56b92bb2b79",
        "pack_sha256": "2b373a513fabf1873d41e8700b1685b40bec4dbd005565419e438de9dfbff461",
        "fixture": "ops/harmonia_ops.py",
        "host": "work/ops/harmonia_ops.py",
        "root": "music",
        "anchors": [
            {
                "old": "def dump_json(path, obj):\n    tmp = \"%s.tmp.%d\" % (path, os.getpid())\n",
                "new": (
                    "def dump_json(path, obj):\n"
                    "    if os.path.basename(path) == \"library.json\":\n"
                    "        import sys as _pg_sys, os as _pg_os\n"
                    "        _u = _pg_os.environ.get(\"HARMONIA_UNIFIED_WRITER\")\n"
                    "        if _u and _u not in _pg_sys.path:\n"
                    "            _pg_sys.path.insert(0, _u)\n"
                    "        import unified_writer.prod_guard as _pg\n"
                    "        _pg.guard_library_mutation(_pg_os.environ.get(\"HARMONIA_WRITER_ENTRY\") or \"library_compat\", path)  # HARMONIA_UNIFIED_GUARD_v1\n"
                    "    tmp = \"%s.tmp.%d\" % (path, os.getpid())\n"
                ),
            },
            {
                "old": "def git(ctx, *args, check=True, timeout=120):\n    r = subprocess.run([\"git\", \"-C\", ctx.deploy] + list(args), capture_output=True, text=True, timeout=timeout)\n",
                "new": (
                    "def git(ctx, *args, check=True, timeout=120):\n"
                    "    if args and args[0] == \"push\":\n"
                    "        bad = [a for a in args if a in (\"--force\", \"-f\", \"--force-with-lease\") or (isinstance(a, str) and a.startswith(\"+\"))]\n"
                    "        if bad:\n"
                    "            raise RuntimeError(\"ordinary_push_only %s\" % (bad,))  # HARMONIA_UNIFIED_GUARD_v1\n"
                    "    r = subprocess.run([\"git\", \"-C\", ctx.deploy] + list(args), capture_output=True, text=True, timeout=timeout)\n"
                ),
            },
            {
                "old": (
                    "        f.seek(0); f.truncate(); f.write(\"%s pid=%d since=%s\\n\" % (who, os.getpid(), iso())); f.flush()\n"
                    "        log(\"拿到写锁（等了 %.0f 秒）\" % (time.time() - t0))\n"
                    "        yield\n"
                    "    finally:\n"
                    "        fcntl.flock(f, fcntl.LOCK_UN)\n"
                    "        f.close()\n"
                    "        log(\"释放写锁\")\n"
                ),
                "new": (
                    "        f.seek(0); f.truncate(); f.write(\"%s pid=%d since=%s\\n\" % (who, os.getpid(), iso())); f.flush()\n"
                    "        log(\"拿到写锁（等了 %.0f 秒）\" % (time.time() - t0))\n"
                    "        import sys as _pg_sys, os as _pg_os\n"
                    "        _u = _pg_os.environ.get(\"HARMONIA_UNIFIED_WRITER\")\n"
                    "        if _u and _u not in _pg_sys.path:\n"
                    "            _pg_sys.path.insert(0, _u)\n"
                    "        _ops = _pg_os.path.dirname(_pg_os.path.abspath(__file__))\n"
                    "        if _ops not in _pg_sys.path:\n"
                    "            _pg_sys.path.insert(0, _ops)\n"
                    "        import unified_writer.prod_guard as _pg\n"
                    "        _pg.begin_held_flock(f.fileno(), ctx.lock_path, who, ctx.lib_path)  # HARMONIA_UNIFIED_GUARD_v1\n"
                    "        try:\n"
                    "            yield\n"
                    "        finally:\n"
                    "            _pg.end_held_flock()\n"
                    "    finally:\n"
                    "        fcntl.flock(f, fcntl.LOCK_UN)\n"
                    "        f.close()\n"
                    "        log(\"释放写锁\")\n"
                ),
            },
            {
                "old": (
                    "        r = subprocess.run([sys.executable, os.path.join(ctx.work, \"apply_ratings.py\"), \"--publish\"], cwd=ctx.root,\n"
                    "                           capture_output=True, text=True, timeout=3600)\n"
                ),
                "new": (
                    "        import unified_writer.prod_guard as _pg\n"
                    "        r = subprocess.run([sys.executable, os.path.join(ctx.work, \"apply_ratings.py\"), \"--publish\"], cwd=ctx.root,\n"
                    "                           capture_output=True, text=True, timeout=3600, **_pg.subprocess_lock_kwargs())  # HARMONIA_UNIFIED_GUARD_v1\n"
                ),
            },
            {
                "old": "    b = subprocess.run([sys.executable, os.path.join(ctx.root, \"build.py\")], cwd=ctx.root, capture_output=True, text=True, timeout=1800)\n",
                "new": (
                    "    import unified_writer.prod_guard as _pg\n"
                    "    b = subprocess.run([sys.executable, os.path.join(ctx.root, \"build.py\")], cwd=ctx.root, capture_output=True, text=True, timeout=1800, **_pg.subprocess_lock_kwargs())  # HARMONIA_UNIFIED_GUARD_v1\n"
                ),
            },
        ],
    },
    {
        "id": "with_library_lock.sh",
        "src_sha256": "f1b3e30b087859f502d2ca998d83cfbc84a2cc9eae32eb75b1b6bf6ee04ba146",
        "pack_sha256": "f1b3e30b087859f502d2ca998d83cfbc84a2cc9eae32eb75b1b6bf6ee04ba146",
        "fixture": "ops/with_library_lock.sh",
        "host": "work/ops/with_library_lock.sh",
        "root": "music",
        "replace_from": "bin/with_library_lock.sh",
        "marker": "HARMONIA_UNIFIED_GUARD_v1",
        "anchors": [],
    },
    {
        "id": "harmonia_publish.py",
        "src_sha256": "2ba62536235a28e93454304c9ea077ad9512e7fbb346a9c8c4fe3881afa0735a",
        "pack_sha256": "2ba62536235a28e93454304c9ea077ad9512e7fbb346a9c8c4fe3881afa0735a",
        "fixture": "publish/harmonia_publish.py",
        "host": "harmonia_publish.py",
        "root": "publish",
        "anchors": [
            {
                "old": "import harmonia_target as T\nWEB = \"/var/www/musicbox\"\n",
                "new": (
                    "import harmonia_target as T\n"
                    "def _harmonia_arm_publish(deploy_dir, index_sha):\n"
                    "    import os, sys\n"
                    "    p = os.environ.get(\"HARMONIA_UNIFIED_WRITER\")\n"
                    "    if p and p not in sys.path:\n"
                    "        sys.path.insert(0, p)\n"
                    "    import unified_writer.prod_guard as _pg\n"
                    "    _pg.arm_site_publish(deploy_dir, index_sha)  # HARMONIA_UNIFIED_GUARD_v1\n"
                    "WEB = \"/var/www/musicbox\"\n"
                ),
            },
            {
                "old": (
                    "    print(\"[%s] 包校验 OK index=%s… 图片=%d\" % (T.label(), S[\"index.html\"][:12], len(sums(os.path.join(a.dir, \"IMG_SHA256\")))))\n"
                    "    web = T.rpath(WEB)\n"
                ),
                "new": (
                    "    print(\"[%s] 包校验 OK index=%s… 图片=%d\" % (T.label(), S[\"index.html\"][:12], len(sums(os.path.join(a.dir, \"IMG_SHA256\")))))\n"
                    "    if a.execute:\n"
                    "        _cas_src = a.rollback if a.rollback else os.path.join(a.dir, \"index.html\")\n"
                    "        _harmonia_arm_publish(a.dir, hashlib.sha256(open(_cas_src, \"rb\").read()).hexdigest())  # HARMONIA_UNIFIED_GUARD_v1\n"
                    "        if a.rollback:\n"
                    "            import shutil as _shutil\n"
                    "            _shutil.copy2(os.path.join(a.dir, \"PUBLISH_CLAIM.json\"), os.path.join(os.path.dirname(os.path.abspath(a.rollback)), \"PUBLISH_CLAIM.json\"))\n"
                    "    web = T.rpath(WEB)\n"
                ),
            },
            {
                "old": "        T._scp([os.path.join(a.dir, f) for f in (\"index.html\", \"SHA256\", \"IMG_SHA256\")], st + \"/\")\n",
                "new": "        T._scp([os.path.join(a.dir, f) for f in (\"index.html\", \"SHA256\", \"IMG_SHA256\", \"PUBLISH_CLAIM.json\")], st + \"/\")\n",
            },
            {
                "old": "T._scp([os.path.join(a.dir, f) for f in (\"index.html\", \"img.tar.gz\", \"SHA256\", \"IMG_SHA256\")], st + \"/\")",
                "new": "T._scp([os.path.join(a.dir, f) for f in (\"index.html\", \"img.tar.gz\", \"SHA256\", \"IMG_SHA256\", \"PUBLISH_CLAIM.json\")], st + \"/\")",
            },
        ],
    },
    {
        "id": "harmonia_target.py",
        "src_sha256": "87ba06cfe859d19597364eddb08093e026d232518ced5cc38d6ba43b4eb7c356",
        "pack_sha256": "fc011a8538cc3a15f7a89f8adc4c07e2382cb24a46e5f28ac9168acd9deff79d",
        "fixture": "publish/harmonia_target.py",
        "host": "harmonia_target.py",
        "root": "publish",
        "anchors": [
            {
                "old": "    raise RuntimeError(\"HARMONIA_TARGET=hora：拒绝向旧 VPS 执行命令（防绕回）：%s\" % cmd[:80])\n",
                "new": (
                    "    raise RuntimeError(\"HARMONIA_TARGET=hora：拒绝向旧 VPS 执行命令（防绕回）：%s\" % cmd[:80])\n"
                    "\n"
                    "def ensure_win_claim(claim_json_path):\n"
                    "    \"\"\"Copy the publish claim onto the Windows host before index replace. HARMONIA_UNIFIED_GUARD_v1\"\"\"\n"
                    "    remote_dir = \"C:/harmonia/pub/winpub/publish.claim.d\"\n"
                    "    if is_hora():\n"
                    "        _ssh('mkdir \"%s\"' % remote_dir.replace(\"/\", \"\\\\\"), check=False)\n"
                    "    else:\n"
                    "        _ssh(\"mkdir -p \" + remote_dir, check=False)\n"
                    "    _scp([claim_json_path], remote_dir + \"/meta.json\")\n"
                ),
            },
        ],
    },
    {
        "id": "pub_remote.py",
        "src_sha256": "23201a49782d1d6408edd192710a28abed0a0070c77804437ec188cb4ef402de",
        "pack_sha256": "23201a49782d1d6408edd192710a28abed0a0070c77804437ec188cb4ef402de",
        "fixture": "publish/pub_remote.py",
        "host": "pub_remote.py",
        "root": "publish",
        "anchors": [
            {
                "old": "def publish(st, web, tag):\n",
                "new": (
                    "def _harmonia_load_claim():\n"
                    "    import os, sys\n"
                    "    here = os.path.dirname(os.path.abspath(__file__))\n"
                    "    if here not in sys.path:\n"
                    "        sys.path.insert(0, here)\n"
                    "    from win_publish_guard import require_backup_claim, require_index_claim\n"
                    "    return require_index_claim, require_backup_claim\n"
                    "def publish(st, web, tag):\n"
                ),
            },
            {
                "old": "    if sha(tmp) != S['index.html']: raise SystemExit('INDEX TMP MISMATCH')\n    os.replace(tmp, idx); rep['index_sha'] = sha(idx); rep['index_ok'] = rep['index_sha'] == S['index.html']\n",
                "new": (
                    "    if sha(tmp) != S['index.html']: raise SystemExit('INDEX TMP MISMATCH')\n"
                    "    _harmonia_load_claim()[0](st, S['index.html'])  # HARMONIA_UNIFIED_GUARD_v1\n"
                    "    os.replace(tmp, idx); rep['index_sha'] = sha(idx); rep['index_ok'] = rep['index_sha'] == S['index.html']\n"
                ),
            },
            {
                "old": "def rollback(web, bk):\n    idx = os.path.join(web, 'index.html'); tmp = idx + '.part'; shutil.copyfile(bk, tmp); os.replace(tmp, idx)\n",
                "new": (
                    "def rollback(web, bk):\n"
                    "    _harmonia_load_claim()[1](bk)  # HARMONIA_UNIFIED_GUARD_v1\n"
                    "    idx = os.path.join(web, 'index.html'); tmp = idx + '.part'; shutil.copyfile(bk, tmp); os.replace(tmp, idx)\n"
                ),
            },
        ],
    },
    {
        "id": "hctl.py",
        "src_sha256": "a69e69a4dfe2307f052dffe4e68a26c8a7e999d4ffcbf8cfde5a3f3141cf4261",
        "pack_sha256": "a8a00024f0d22b3477da6c1a3f843cf24d0abd990b14ea0bf636b6f6467ec1e7",
        "fixture": "music-dl/hctl.py",
        "host": "hctl.py",
        "root": "musicdl",
        "anchors": [
            {
                "old": "            try: os.rename(src, dst); res[\"added\"].append(n); continue\n",
                "new": (
                    "            if os.path.basename(dst).lower() in (\"library.json\", \"index.html\", \"index_pages.html\"):\n"
                    "                raise SystemExit(\"HARMONIA_UNIFIED_GUARD_v1 published_target_refused\")\n"
                    "            try: os.rename(src, dst); res[\"added\"].append(n); continue\n"
                ),
            },
        ],
    },
    {
        "id": "local_audio.py",
        "src_sha256": "3d6320d6a32cc85b2ea5f7aa48ca453fc8373ea6f99ee03417fb245f2781903f",
        "pack_sha256": "3d6320d6a32cc85b2ea5f7aa48ca453fc8373ea6f99ee03417fb245f2781903f",
        "fixture": "music-dl/local_audio.py",
        "host": "local_audio.py",
        "root": "musicdl",
        "anchors": [
            {
                "old": (
                    "    if sha1_file(partial) != dst_sha:\n"
                    "        raise RuntimeError(\"partial sha1 mismatch\")\n"
                    "    os.replace(partial, dst)\n"
                ),
                "new": (
                    "    if sha1_file(partial) != dst_sha:\n"
                    "        raise RuntimeError(\"partial sha1 mismatch\")\n"
                    "    if os.path.basename(dst).lower() in (\"library.json\", \"index.html\", \"index_pages.html\"):\n"
                    "        raise SystemExit(\"HARMONIA_UNIFIED_GUARD_v1 published_target_refused\")\n"
                    "    os.replace(partial, dst)\n"
                ),
            },
        ],
    },
]

ADD_FILES = [
    {
        "id": "win_publish_guard.py",
        "fixture": "publish/win_publish_guard.py",
        "host": "win_publish_guard.py",
        "root": "publish",
        "source": "unified_writer/win_publish_guard.py",
    }
]
