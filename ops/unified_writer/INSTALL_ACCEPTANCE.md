# Revision 4 acceptance

Not a live switch. `formal_switch_blocked` stays true. Do not merge, publish, or download.

## Install

Run this tree's launcher, not the copy already sitting in `work/ops/unified_writer` from revision 3.

```bash
ops/unified_writer/bin/patch-install --self-check
```

Expect `package_revision` 4 and `loaded_from` under this tree. Exit 76 `stale_package_shadow` means an older package was imported. Then:

```bash
ops/unified_writer/bin/patch-install \
  --root /workspace/harmonia-handoff/music-library \
  --publish-root /workspace/musicbox_hora/cand/publish \
  --music-dl-root /workspace/musicbox_hora/cand/music-dl
```

`poll_ratings.py` may differ from the sanitized fixture on exactly three lines: the docstring sentence `失败时用 <path> 读 KV 镜像（令牌不打印、不落盘）。`, the `NSID=open(...)` line, and the `TOK=open(...); ACC=open(...)` line. Those are normalized for the body hash. Every other line must match. The patch does not rewrite those host lines.

Expect `poll_ratings.py` and `make_deploy_variants.py` patched, `ne_avail.py` `stdout_only` with the same bytes. Revision 3 refreshed the package and then stopped on poll drift, so those two scripts were not patched.

## Receipt the downloader will accept for deletion

JSON booleans, not the strings `"true"` / `"false"`. All of these must hold, plus queue id, identity, revision, sha256, size, and duration:

- `box_playable`: `true`
- `commit_scope`: `served_playlist_verified`
- `local_matches`: `true`

`local_playlist` with `box_playable` `false` is a local commit. The downloader keeps the temp video. This writer does not delete it either.

`served_playlist_verified` is written only when all three are true on the files passed in:

1. `--served-playlist` is the served `_ytlist` file and, after the commit, contains this bvid with the same `file` logical path. On hora that file is `C:\harmonia\root\var\www\musicbox\_ytlist\youtube.json` (4 rows, generated Nov 3). A side copy is not that file.
2. `--ledger` has a `copied` or `same_skip` row for that logical path, sha1 and size match the file under `--local-root` (`D:\Harmonia\音乐库`, ledger default `D:\Harmonia\music-migrate\ledger.jsonl`).
3. That is the path the active resolver plays as `/local-audio/...`.

Active resolver evidence is `evidence/resolve_server.py` sha256 `8a064132c39e13e1009978a39990fca4a27996ba43ff0aef1c0ca2348b130380` (ledger, then `/local-audio`, else `/115proxy`). `evidence/resolve_server.src_opt.py` sha256 `d4eee4d8cce3078e23d8779fb0084e9140fc731a7337a1426ecc82be6c93ad7f` has no ledger and is not this path.

```bash
bin/bili-audio-commit \
  --queue <queue.json> \
  --playlist 'C:\harmonia\root\var\www\musicbox\_ytlist\youtube.json' \
  --served-playlist 'C:\harmonia\root\var\www\musicbox\_ytlist\youtube.json' \
  --local-root 'D:\Harmonia\音乐库' \
  --ledger 'D:\Harmonia\music-migrate\ledger.jsonl' \
  --receipt-root <receipts-inbox>
```

## Check, without flipping the live switch

```bash
ops/unified_writer/bin/check.sh \
  --root /workspace/harmonia-handoff/music-library \
  --mode-file /workspace/harmonia-handoff/music-library/work/ops/MODE.json \
  --evidence /path/to/generated-checks.json
```

`install_completed` is true only when `poll_ratings.py` and `make_deploy_variants.py` contain the guard call lines, `ne_avail.py` matches the stdout-only sha256, and `work/ops/unified_writer/REVISION` is 4. A file that only mentions `HARMONIA_UNIFIED_GUARD_v3` stays missing. `ready_for_live` stays false. `formal_switch_blocked` stays true because routine acceptance, MODE, claim lifecycle, and raw writers are still blockers.

`routines.reported_saved` records Lomax's report that all four get/update safety sections are saved, schedules, windows, and MODE unchanged. The contract path is `work/ops/LOMAX_CONTRACT.md`, not `work/ops/unified_writer/`. `routines.verified` stays false while independent signature verification is underway. This check does not invent acceptance evidence. Bili end-to-end is still unrun. The 28-test deletion-gate deploy is underway separately and does not change music-lock install readiness.

`--evidence` is accepted only for `harmonia_install_evidence_v1` with these generated values as JSON booleans and integers, not strings: poll held-lock exit 75 and `snapshot_unchanged` true; make held-lock exit 75 and `output_dir_created` false; fd inherited exit 0 and `inherited` true; CAS exit 76 and `cas_mismatch` true. That file does not clear the live switch.

Windows mutex, cand path only, no publish:

```bash
ops/unified_writer/bin/win-claim-probe --dir 'C:/harmonia/cand/unified_writer'
```

Exit 0 with `"exit_code": 75` and `"published": false` means the second taker was refused. `C:/harmonia/pub` and a directory that contains `index.html` are refused.

## Still not done on the hosts

- This run did not install revision 4 and did not run the consumer on hora.
- The downloader delete gate passed 28 tests and is not deployed on the host yet. Until that deploy, do not treat a receipt as a live delete.
- The four routine texts are unchanged. Spotify / HongKong is not applied.
- `library.json` is not the YouTube list. Pages does not serve `/_ytlist/youtube.json`.
