# Publish wrap point (not applied)

`harmonia_publish.py` was in the handoff. Its import `harmonia_target` was only a redacted stub: no `is_hora`, `label`, `rpath`, `_ssh`, `_scp`, `HOPT`, `HHOST`, or `PY`. `pub_remote.py` was not included. This package does not vendor a second publisher and does not run `--execute`.

When those two files exist on the publish host, the live `main()` must call `assert_contract("publish")` after argument parsing and before any `--execute` or rollback write. Bare `--execute` must keep failing closed unless the process is the wrapper's child and `HARMONIA_WIN_LOCK_HELPER` is set. See `LOMAX_CONTRACT.md`.

Do not treat this note as an installed patch.
