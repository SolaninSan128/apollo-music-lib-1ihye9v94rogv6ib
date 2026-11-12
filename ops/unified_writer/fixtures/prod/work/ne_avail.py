#!/usr/bin/env python3
"""Box-side wrapper: check Netease availability on the VPS (2026-11-03).
Usage: python3 ne_avail.py [--verify] <song_id> [song_id ...]
Prints the VPS JSON lines (one per id). Exit 0 all available, 1 some unavailable, 2 error.
Python: from ne_avail import check; check([id,...]) -> list of dicts
"""
import json, sys
sys.path.insert(0, "/workspace/harmonia-handoff/music-library/work")
import retry_queue as rq

def check(ids, verify=False):
    ids = [str(int(x)) for x in ids]
    r = rq.ssh("/opt/music-dl/check-avail.py " + ("--verify " if verify else "") + " ".join(ids), timeout=180)
    out = [json.loads(l) for l in (r.stdout or "").splitlines() if l.startswith("{")]
    if r.returncode == 2 or not out or "error" in out[0]:
        raise RuntimeError((r.stdout or r.stderr or "")[-300:])
    return out

if __name__ == "__main__":
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    if not a:
        print(__doc__, file=sys.stderr); sys.exit(2)
    try:
        rs = check(a, "--verify" in sys.argv)
    except Exception as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False)); sys.exit(2)
    for x in rs: print(json.dumps(x, ensure_ascii=False))
    sys.exit(0 if all(x["available"] for x in rs) else 1)
