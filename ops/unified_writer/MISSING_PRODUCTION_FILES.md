# Missing production files

These paths are named by the handoff and are not in this Pages repo or the sanitized attachment. The guard fails closed when they are required. Nothing here invents their behavior.

| Id | Expected path | Why it blocks a real switch |
| --- | --- | --- |
| `apply_ratings.py` | `work/apply_ratings.py` | Ratings `--publish` writer. Not shipped. |
| `build.py` | `music-library/build.py` | Pages build. Not shipped. |
| `run_all_queues.py` | `work/run_all_queues.py` | Download writeback. Not shipped. |
| `harmonia_target.py` | `cand/publish/harmonia_target.py` | Stub only. No SSH helpers. |
| `pub_remote.py` | `cand/publish/pub_remote.py` | Windows publish executor. Not shipped. |
| `harmonia_ops.py` | `work/ops/harmonia_ops.py` | Only `library_lock()` was excerpted. |
| Lomax prompts | (not in the handoff) | Cannot show the four routines already call this wrapper. |

Also absent: a wake URL, and any proof the Windows mkdir claim is installed on the hora publish host. `formal_switch_blocked` stays true.
