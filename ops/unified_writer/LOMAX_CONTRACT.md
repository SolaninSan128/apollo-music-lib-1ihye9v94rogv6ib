# Lomax contract (version 2)

Interface version 2. Version 1’s filename deny list is not the lock. Date-specific `ingest_YYYY_MM_DD.py` and `daily_artist_YYYYMMDD.py` stay valid names. They import `run_all_queues` and call `commit_library_json`, `guarded_build`, and `guarded_pages_push`.

Schedules are unchanged. See `ROUTINE_BOUNDARIES.md`. This package does not edit routine text.

# Lomax contract (version 1)

Do not change the four routine schedules, MODE, or the homepage from this package. Exit 75 means the library flock or the Windows publish claim is busy. Exit 76 means fail closed (CAS, fence, crash lease, or a publish without a claim).

`patch-install` copies the importable package to `work/ops/unified_writer` on the real tree `/workspace/harmonia-handoff/music-library`. Patched scripts insert that `work/ops` directory on `sys.path`. Set `HARMONIA_LIBRARY_LOCK=work/.library.lock` only when the shell is not already sitting in `work/ops` (the installed `with_library_lock.sh` finds the lock itself).

Outer `with_library_lock.sh` holds the flock and exports the contract, including the inherited lock fd. The inner `guard_library_mutation` sees that contract and does not open the lock file again, so ratings does not deadlock. A child that waits out `LOCK_WAIT` and then acquires is a normal grant, not a failure: keep any test holder alive for longer than `LOCK_WAIT` when the assertion is “must not write”.

Set `HARMONIA_UNIFIED_WRITER` to `work/ops` if the process is not already importing from that directory.

## Fixed commands

Ratings, Pages only, no hora main publish:

```bash
work/ops/with_library_lock.sh python3 work/apply_ratings.py --publish
```

After the hash-gated patch, `apply_ratings.py` also takes that same flock itself before it writes. `--dry-run` still does not write.

03:00 writeback is `commit_library_json` (or ops `gapfill` live, which already holds the flock) then `guarded_build` and `guarded_pages_push`. It does not call `harmonia_publish.py`.

06:00 / 07:00, inside the generated script, after downloads:

```python
import os
import run_all_queues as rq
rq.commit_library_json("library.json", lib, expected_sha=old_sha)  # refusal exits 76
rq.guarded_build()
# HARMONIA_PAGES_DEPLOY is music-library/deploy, the git work tree.
# A dated package directory is not that tree and is refused.
rq.guarded_pages_publish(os.environ["HARMONIA_PAGES_DEPLOY"], ["index.html", "index_pages.html"], message)
```

`guarded_pages_push` only runs `git push origin main`. It does not add or commit. `guarded_pages_publish` is the step that adds, commits, then pushes, in that order. Set `HARMONIA_MUSIC_ROOT` to the music-library directory. Relative lock and deploy paths join that root, not the process cwd.

07:00 collision uses the kernel lock, not mtime. A missing or unresolvable lock stops (76). A held lock keeps waiting (75). This function does not create a lock file.

```python
from unified_writer.prod_guard import collision_blocks
state = collision_blocks("work/.library.lock")
if state["exit_code"] == 76:
    raise SystemExit(76)
if state["blocked"]:
    raise SystemExit(75)
```

`make_deploy_variants.py` is run under `rq.guarded_build([sys.executable, "work/make_deploy_variants.py", pkg])` or `LibraryChain.run`. Its own source was not in the pack.

## Main-site publish (Feynman, separate)

```bash
HARMONIA_LIBRARY_LOCK=work/.library.lock \
HARMONIA_LOCK_ROOT=work/.unified_lock \
HARMONIA_UNIFIED_WRITER=work/ops \
python3 cand/publish/harmonia_publish.py <package-dir> --execute
```

That arms the claim and uploads `PUBLISH_CLAIM.json`. `pub_remote.py` will not replace `index.html` without it. Ratings and 03:00 must not use this command. Spotify / HongKong apply stays out of this change.

## Check

```bash
work/ops/check.sh
```

Exit 0 means `formal_switch_blocked` is still true and `ready_for_live` is false.
