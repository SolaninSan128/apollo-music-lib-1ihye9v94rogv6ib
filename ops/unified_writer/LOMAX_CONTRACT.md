# Lomax contract (version 2)

Interface version 2. Version 1’s filename deny list is not the lock. Date-specific `ingest_YYYY_MM_DD.py` and `daily_artist_YYYYMMDD.py` stay valid names. They import `run_all_queues` and call `commit_library_json`, `guarded_build`, and `guarded_pages_push`.

Schedules are unchanged. See `ROUTINE_BOUNDARIES.md`. This package does not edit routine text.

# Lomax contract (version 1)

Do not change the four routine schedules, MODE, or the homepage from this package. Exit 75 means the library flock or the Windows publish claim is busy. Exit 76 means fail closed (CAS, fence, crash lease, or a publish without a claim).

Set `HARMONIA_LIBRARY_LOCK` to `work/.library.lock` and `HARMONIA_UNIFIED_WRITER` to the directory that contains the `unified_writer` package (`work/ops` after `patch-install` copies it there).

## Fixed commands

Ratings, Pages only, no hora main publish:

```bash
work/ops/with_library_lock.sh python3 work/apply_ratings.py --publish
```

After the hash-gated patch, `apply_ratings.py` also takes that same flock itself before it writes. `--dry-run` still does not write.

03:00 writeback is `commit_library_json` (or ops `gapfill` live, which already holds the flock) then `guarded_build` and `guarded_pages_push`. It does not call `harmonia_publish.py`.

06:00 / 07:00, inside the generated script, after downloads:

```python
import run_all_queues as rq
rq.commit_library_json("library.json", lib, expected_sha=old_sha)
rq.guarded_build()
rq.guarded_pages_push(deploy_dir)
```

07:00 collision, instead of a 20-minute mtime check, once the prompt is saved:

```python
from unified_writer.prod_guard import collision_blocks
if collision_blocks("work/.library.lock")["blocked"]:
    raise SystemExit(0)
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
