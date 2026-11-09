# R3 patch-install blocked on poll_ratings.py (exit 76)

Host sha256 `5457c994…`. Catalog expects fixture body_sha256 from **sanitized** pack where comment line still contains `<CF_CRED_PATH>` and NSID path uses `<KV_NS_FILE>`.

`secret_line_res` correctly strips host lines 12–13, but **does not strip the docstring/comment on line 7** that embeds `/home/box/.cf_token`. Host body_sha ≠ catalog body_sha → fail-closed drift. Do not bypass.

Fix for CloudAgent: compute body_sha256 from real host bytes with secret_line_res (and exclude the comment path), or ship fixture with real path shapes in non-secret comment / exclude that comment via regex. Do not paste redacted fixture onto host.

`make_deploy_variants.py` was not reached (install stops on first drift). `ne_avail.py` hash-checked only; bytes unchanged.

Package `work/ops/unified_writer/REVISION` was refreshed to `3` before the drift abort.
