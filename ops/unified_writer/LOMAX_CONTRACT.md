# Lomax contract (version 1)

Do not change the four routine schedules, MODE, or the homepage from this package. Save the commands below. Running them on the server before the missing programs exist fails closed (exit 76). That is intentional.

Lock order is always `library`, then `publish`. Exit 75 means the kernel lock or the Windows publish claim is busy. Exit 76 means fail closed (legacy name, missing program, CAS, fence, or crash lease).

## Four routines

Ratings:

```bash
work/ops/with_unified_lock run --root work/.unified_lock --library-lock work/.library.lock \
  --entry ratings --wait "${LOCK_WAIT:-1800}" -- \
  work/ops/guard-ratings
```

Build and Pages push:

```bash
work/ops/with_unified_lock run --root work/.unified_lock --library-lock work/.library.lock \
  --entry build_push --wait "${LOCK_WAIT:-1800}" -- \
  work/ops/guard-build-push
```

Download stays outside the lock. The library writeback does not:

```bash
work/ops/with_unified_lock run --root work/.unified_lock --library-lock work/.library.lock \
  --entry download_writeback --cas-file work/library.json --cas-hash "$LIBRARY_SHA" \
  --wait "${LOCK_WAIT:-1800}" -- \
  work/ops/guard-download-writeback
```

Spotify / HongKong increment, only after a reviewed hash of the current library file. This package does not apply either candidate:

```bash
work/ops/with_unified_lock run --root work/.unified_lock --library-lock work/.library.lock \
  --entry increment --cas-file work/library.json --cas-hash "$LIBRARY_SHA" \
  --wait "${LOCK_WAIT:-1800}" -- \
  work/ops/guard-increment
```

Point `HARMONIA_REAL_RATINGS`, `HARMONIA_REAL_BUILD_PUSH`, `HARMONIA_REAL_DOWNLOAD_WRITEBACK`, and `HARMONIA_REAL_INCREMENT` at the real programs only after those files are on disk. Until then the guards exit 76.

## Env the child can trust

`HARMONIA_WRITER_CONTRACT=1`, `HARMONIA_CONTRACT_VERSION=1`, `HARMONIA_WRITER_ENTRY`, `HARMONIA_WRITER_TOKEN`, `HARMONIA_WRITER_FENCE_LIBRARY`, `HARMONIA_WRITER_FENCE_PUBLISH`, `HARMONIA_WRITER_KINDS`, `HARMONIA_LIBRARY_LOCK_FD`, `HARMONIA_PUBLISH_LOCK_FD`, `HARMONIA_LIBRARY_LOCK_PATH`, `HARMONIA_WIN_PUBLISH_CLAIM`, `HARMONIA_RESOURCE_CAS`, `HARMONIA_LOCK_ORDER=library,publish`.

`commit_cas` is the fenced replace. A raw write of `library.json` is still possible from an unwrapped program; that is why the switch stays blocked.

## Publish (not a Lomax routine)

```bash
work/ops/with_unified_lock run --root work/.unified_lock --library-lock work/.library.lock \
  --entry publish --cas-file "$PACKAGE/index.html" --cas-hash "$INDEX_SHA" --plan-only
```

Do not pass `harmonia_publish.py --execute`. The guard rejects `--execute` unless `HARMONIA_ALLOW_PUBLISH_EXECUTE=1` and `HARMONIA_WIN_LOCK_HELPER` are both set, and this package does not set them.

## Legacy flock

`work/ops/with_library_lock.sh <command>` still flocks `work/.library.lock` and still exits 75 on timeout. It now also records a token and fence. It does not take the publish claim and it does not make a bare `apply_ratings.py` safe.

## Check

```bash
work/ops/check.sh
work/ops/check.sh --self-test
```

Exit 0 from `check.sh` means `formal_switch_blocked` is still true.
