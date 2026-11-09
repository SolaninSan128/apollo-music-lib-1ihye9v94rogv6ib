# Routine boundaries (read, not edited)

Recovered prompts, not a license to publish or download. Schedules and business rules stay as written. Lomax saves any new calls after this interface is usable. This file does not change a routine.

| Routine | Id | Schedule (Asia/Shanghai, unchanged) |
| --- | --- | --- |
| 每日音乐小推＋每日歌手＋寻声 | `automation-1792430011738` | `CRON_TZ=Asia/Shanghai 0 6 * * *` (flexible ±15, firesAt `45 5 * * *`) |
| 评分同步 | `automation-1792430014615` | `0 0 * * *` and `0 12 * * *` |
| 凌晨查缺 | `automation-1792455381543` | `CRON_TZ=Asia/Shanghai 0 3 * * *` (firesAt `9 3 * * *`) |
| 日推补跑 | `automation-1793430016340` | `CRON_TZ=Asia/Shanghai 0 7 * * *` (early 0, late 15, firesAt `13 7 * * *`) |

Recovered prompt sha256 (these replace the earlier truncated copies):

- 06: `f8850cd50b86983fb1b493004b64da2d1dd70dee0f7131105516de15f89bf915`
- 07: `9f3c65223d6a68026680ab63d1d0f6b0185b8bc5264a8158d415eb26bfcfd912`

## What the prompts actually do

- 06 and 07 generate `work/ingest_YYYY_MM_DD.py` and `work/daily_artist_YYYYMMDD.py` at runtime. Those files import `run_all_queues` as a module. A fixed historical filename is not the entry.
- Download stays outside the library lock. `run_all_queues.save_progress` may write a progress file. It refuses `library.json` and `index.html`.
- Library write, `build.py`, `work/make_deploy_variants.py`, and an ordinary Pages `git push` (no `--force`) are the mutation chain. `PLAY_RESOLVE` stays `https://harmonia.corgihora.bid/music/resolve` unless `work/PLAY_RESOLVE.url` says otherwise.
- Ratings `--publish` builds and pushes Pages only. It does not call the hora main-site publisher. The 03:00 gapfill also stops at Pages.
- Main-site publish stays with Feynman: `harmonia_publish.py --execute` takes the library flock and the Windows mkdir claim, then `pub_remote.py` refuses to replace `index.html` without `PUBLISH_CLAIM.json` and a live claim. This package does not guess `--target`.
- 07 step 2 still says “mtime within 20 minutes” in the saved prompt. The replacement check is `collision_blocks(lock_path)`. It uses flock and the crash lease, not mtime. The prompt text is unchanged.
- `MODE.json` stays `{"ratings":"shadow","gapfill":"shadow","dailycheck":"shadow"}`.

## Calls a new date script can make

```python
import run_all_queues as rq
rq.run_batch(...)  # download / progress only
rq.commit_library_json(library_path, lib, expected_sha=old_sha)
rq.guarded_build()
rq.guarded_pages_push(deploy_dir)  # ordinary push
```

Or `unified_writer.chain.LibraryChain` (interface 2). Do not call `arm_site_publish` from ratings, gapfill, or the daily scripts.

Bili favorites use a separate consumer, `bili-audio-commit`. It is not one of these four routines and this file does not change their text. The pipeline deletes a temp video only after it validates a receipt; the consumer never deletes video and never writes 115.

## Not claimed

- The four routines have not been updated to call this interface.
- `make_deploy_variants.py`, `poll_ratings.py`, and `ne_avail.py` were not in the script pack, so their internals are not patched. Run them under `LibraryChain.run`.
- The Windows claim was not created on the live hora host.
- `ready_for_install` is the hash-gated patch. `ready_for_live` is false. `formal_switch_blocked` stays true.
