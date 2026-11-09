# Unified writer guard

Deployable single-writer guard for the Harmonia library lock and the Windows publish boundary. Copying this directory into the Pages repo does **not** install it on the box, does not restart the scheduler, and does not publish.

Current production homepage and `PLAY_RESOLVE` (`https://harmonia.corgihora.bid/music/resolve` on main `464286b`) are outside this directory. MODE stays whatever the server already has (handoff sample is all `shadow`). IPv6 routing is not in this repo and is not changed.

## What is compatible

- Linux `flock(2)` on the existing `work/.library.lock` inode, including util-linux `flock` and the legacy shell script.
- Fixed order: library, then publish. Release is the reverse.
- Owner token, monotonic fence, and CAS of a caller-supplied file.
- Windows publish mutex is a `mkdir` claim (`winpub_lock.py` does not import `fcntl`). A remote pid is not proof the publisher died. An unexpired claim conflicts. An expired claim is taken over only with a matching hash; otherwise it fails closed.
- A crash that leaves meta and drops the kernel lock fails closed until the lease ends **and** the hash matches.

## Install the production patches

`bin/patch-install` checks each on-host sha256, writes `*.bak.unified-writer-v1`, and applies anchors that do not include redacted secret lines. It also copies this package to `<root>/work/ops/unified_writer`, which is the import path the patched scripts use. The real root `/workspace/harmonia-handoff/music-library` is allowed. A drifted file exits 76. It does not publish, download, or edit MODE. A v1 tree from the previous bundle is upgraded in place with the read-then-lock hash check; the first backup is left as it is.

```bash
bin/patch-install --root /workspace/harmonia-handoff/music-library \
  --publish-root /workspace/musicbox_hora/cand/publish \
  --music-dl-root /workspace/musicbox_hora/cand/music-dl
```

`--check` only reports. `--fixture` is for the redacted copies under `fixtures/prod` and must not be used on the host. `ready_for_install` means the patch set applies. `ready_for_live` stays false until Lomax saves the new calls and the Windows claim has been proven on hora.

## Commands

From this directory, against a temp root only:

```bash
python3 -m unittest unified_writer.test_suite
bin/check.sh
bin/install.sh --dest /tmp/harmonia-writer-preview
bin/rollback.sh --dest /tmp/harmonia-writer-preview
```

`check.sh` exits 0 when the formal switch is **still blocked**. See `LOMAX_CONTRACT.md` for the four routine commands and `MISSING_PRODUCTION_FILES.md` for the programs this artifact cannot wrap because they were not shipped.

## Not done by these commands

No Spotify apply, no HongKong download, no music download, no Pages push, no process stop, no MODE edit, no write to `library.json` or `index.html`.
