#!/usr/bin/env python3
"""Serial download queues: citypop remainder -> daily -> fujii -> suchmos.
Each song has remote timeout; hung CDN gets killed and retried once.

Login guard (2026-10-20): before each batch the VPS Netease login is checked
(check-login.py, cached 10 min). Remote download exit 42 = login invalid -> this song and
all remaining songs of the batch go to RETRY_QUEUE.json (reason=login_invalid) and the
batch stops (process exits 42). Exit 43 = lossless exists but FLAC unavailable -> queued
as downgraded_mp3, batch continues. Rerun later with retry_after_login.py.
"""
import json, os, shlex, subprocess, sys, time
from pathlib import Path

try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import retry_queue as rq
except Exception as _e:  # never let the guard break the queue
    rq = None
    print(f"WARN retry_queue import failed: {_e}", file=sys.stderr)

EXIT_LOGIN_INVALID = 42
EXIT_DOWNGRADE = 43

import sys as _s; _s.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harmonia_target as T  # 迁 hora 候选：HARMONIA_TARGET=yecaoyun(默认)/hora/hora-test

# ---- 本地存储回执（2026-11-09）：完成/skip 须带 storage/verify/local_path；local 模式不认裸 ok ----
_AUDIO_DEST_CACHE = None

def audio_dest_mode() -> str:
    """hora: 问 hctl；否则环境/默认 115。"""
    global _AUDIO_DEST_CACHE
    if _AUDIO_DEST_CACHE:
        return _AUDIO_DEST_CACHE
    v = (os.environ.get("HARMONIA_AUDIO_DEST") or "").strip().lower()
    if v in ("local", "115"):
        _AUDIO_DEST_CACHE = v
        return v
    if T.is_hora():
        try:
            r = T.hctl("audio_dest", timeout=30)
            d = json.loads((r.stdout or "").strip().splitlines()[-1])
            _AUDIO_DEST_CACHE = d.get("audio_dest") or "115"
            return _AUDIO_DEST_CACHE
        except Exception:
            pass
    _AUDIO_DEST_CACHE = "115"
    return _AUDIO_DEST_CACHE

def local_evidence(logic_path: str) -> dict:
    """查 hora 本地文件+账本 verified；非 hora/失败返回 ok=False。"""
    if not logic_path or not T.is_hora():
        return {"ok": False, "verified": False, "error": "no_path_or_not_hora"}
    try:
        r = T.hctl("local_stat", timeout=120, path=logic_path)
        return json.loads((r.stdout or "").strip().splitlines()[-1])
    except Exception as e:
        return {"ok": False, "verified": False, "error": f"{type(e).__name__}: {e}"[:200]}

_VERIFY_OK = frozenset({"sha1_src+dst", "sha1_dst_only"})

def _sha1_hex_ok(s) -> bool:
    if not isinstance(s, str):
        return False
    s = s.strip().upper()
    return len(s) == 40 and all(c in "0123456789ABCDEF" for c in s)

def _local_path_under_root(p) -> bool:
    r"""物理路径必须落在 D:\Harmonia\音乐库 下（防路径逃逸）。"""
    if not isinstance(p, str) or not p.strip():
        return False
    n = p.replace("/", "\\")
    while "\\\\" in n:
        n = n.replace("\\\\", "\\")
    parts = [x for x in n.split("\\") if x]
    if ".." in parts:
        return False
    low = n.lower()
    root = "d:\\harmonia\\音乐库"
    return low == root or low.startswith(root + "\\")


def local_receipt_ok(ev_or_row: dict) -> bool:
    """统一严格门闸（attach / load_done / retry 共用）。不得仅凭 verified 或任意 truthy verify。"""
    if not isinstance(ev_or_row, dict):
        return False
    if ev_or_row.get("storage") != "local":
        return False
    if "verified" in ev_or_row and ev_or_row.get("verified") is not True:
        return False
    if ev_or_row.get("exists") is False:
        return False
    if not _local_path_under_root(ev_or_row.get("local_path")):
        return False
    if ev_or_row.get("verify") not in _VERIFY_OK:
        return False
    size = ev_or_row.get("size")
    if type(size) is not int or size <= 0:
        return False
    if not _sha1_hex_ok(ev_or_row.get("sha1") or ""):
        return False
    if ev_or_row.get("ledger_size") is not None and ev_or_row.get("ledger_size") != size:
        return False
    if ev_or_row.get("ledger_sha1") and str(ev_or_row["ledger_sha1"]).strip().upper() != str(ev_or_row["sha1"]).strip().upper():
        return False
    return True


def attach_local_evidence(row: dict, logic_path: str | None = None) -> dict:
    """为 ok/skip 行补齐结构化证据；local 下未过 local_receipt_ok 则降为 fail。"""
    path = logic_path or row.get("alist_path")
    mode = audio_dest_mode()
    if mode != "local":
        row.setdefault("storage", row.get("storage") or "115")
        return row
    ev = local_evidence(path)
    row["storage"] = "local"
    row["local_path"] = ev.get("local_path")
    row["size"] = ev.get("size")
    row["sha1"] = ev.get("sha1")
    row["verify"] = ev.get("verify")
    if local_receipt_ok(ev):
        return row
    row["ok"] = "fail"
    row["reason"] = "local_not_verified:" + str(
        ev.get("verify_error") or ev.get("error") or ev.get("ledger_status") or "incomplete_evidence"
    )[:200]
    return row

WORK = Path(os.environ.get("HARMONIA_WORK", "/workspace/harmonia-handoff/music-library/work"))  # 候选：可指向隔离账本
SSH_HOST = "root@<REDACTED_HOST>"
SSHPASS_FILE = "<REDACTED_VPS_SSHPASS_PATH>" if os.path.exists("<REDACTED_VPS_SSHPASS_PATH>") else "<REDACTED_VPS_SECRET_PATH>"  # 2026-11-08 box 重启丢 /tmp 后回退持久路径
REMOTE_TIMEOUT = 300  # seconds per song on VPS

CITYPOP_DEST = "/115/音乐库/歌单/city-pop-dj-set"
DAILY_DEST = "/115/音乐库/日推/2026-10-20"
FUJII_DEST = "/115/音乐库/歌手/藤井風"
SUCHMOS_DEST = "/115/音乐库/歌手/Suchmos"

LAUFEY_DEST = "/115/音乐库/歌手/Laufey"
LAUFEY = [
    {"song_id": 1963941809, "artist": "Laufey", "title": "Falling Behind"},
    {"song_id": 1967957096, "artist": "Laufey", "title": "Fragile"},
    {"song_id": 1993476481, "artist": "Laufey", "title": "Love to Keep Me Warm"},
    {"song_id": 2021141844, "artist": "Laufey", "title": "Let You Break My Heart Again (Live at The Symphony)"},
    {"song_id": 1822031017, "artist": "Laufey", "title": "I Wish You Love"},
    {"song_id": 1912034355, "artist": "Laufey", "title": "Valentine"},
    {"song_id": 2047103213, "artist": "Laufey", "title": "From The Start"},
    {"song_id": 2021141005, "artist": "Laufey", "title": "Valentine (Live at The Symphony)"},
    {"song_id": 1865675742, "artist": "Laufey", "title": "Let You Break My Heart Again"},
]


CITYPOP = [
    {"song_id": 1406486090, "artist": "石橋和子", "title": "イーヨー"},
    {"song_id": 22693846, "artist": "山下達郎", "title": "DAYDREAM"},
    {"song_id": 659423, "artist": "竹内まりや", "title": "Plastic Love"},
    {"song_id": 1454602852, "artist": "三田寛子", "title": "ピンク・シャドウ"},
    {"song_id": 2004668855, "artist": "伊勢正三", "title": "Orange Grove"},
    {"song_id": 1454263179, "artist": "上田正樹", "title": "Mr. Piano Man"},
    {"song_id": 2055882960, "artist": "横山輝一", "title": "レイニー・デイ"},
    {"song_id": 28827709, "artist": "斉藤由貴", "title": "振袖にピースサイン"},
    {"song_id": 544151438, "artist": "Nulbarich", "title": "ain't on the map yet"},
]
DAILY = [
    {"song_id": 22652382, "artist": "くるり", "title": "ばらの花"},
    {"song_id": 2098539882, "artist": "Hedigan's", "title": "LOVE (XL)"},
    {"song_id": 1817101975, "artist": "The Poles", "title": "Good Morning Sunshine"},
    {"song_id": 27809039, "artist": "ミツメ", "title": "煙突"},
]
FUJII = [
    {"song_id": 2679492671, "artist": "藤井風", "title": "真っ白"},
    {"song_id": 2136341653, "artist": "藤井風", "title": "ガーデン (Live at Tiny Desk)"},
    {"song_id": 2611404792, "artist": "藤井風", "title": "Feelin’ Go(o)d"},
    {"song_id": 2136341779, "artist": "藤井風", "title": "きらり (Live at Tiny Desk)"},
    {"song_id": 1449537105, "artist": "藤井風", "title": "調子のっちゃって"},
    {"song_id": 1988121721, "artist": "藤井風", "title": "grace"},
    {"song_id": 1449536598, "artist": "藤井風", "title": "さよならべいべ"},
    {"song_id": 1449537102, "artist": "藤井風", "title": "もうええわ"},
    {"song_id": 1449537101, "artist": "藤井風", "title": "何なんw"},
    {"song_id": 1930773666, "artist": "藤井風", "title": "ガーデン"},
    {"song_id": 1449536597, "artist": "藤井風", "title": "特にない"},
    {"song_id": 1449537108, "artist": "藤井風", "title": "帰ろう"},
    {"song_id": 1930010660, "artist": "藤井風", "title": "まつり"},
    {"song_id": 1841909290, "artist": "藤井風", "title": "きらり"},
    {"song_id": 2135625944, "artist": "藤井風", "title": "満ちてゆく"},
    {"song_id": 2090470651, "artist": "藤井風", "title": "花"},
    {"song_id": 1449537106, "artist": "藤井風", "title": "死ぬのがいいわ"},
]
SUCHMOS = [
    {"song_id": 1416405116, "artist": "Suchmos", "title": "Pacific Blues (Live at YOKOHAMA STADIUM 2019.09.08)"},
    {"song_id": 33071065, "artist": "Suchmos", "title": "Alright"},
    {"song_id": 1354221542, "artist": "Suchmos", "title": "WHY"},
    {"song_id": 574505189, "artist": "Suchmos", "title": "FUNNY GOLD"},
    {"song_id": 33071067, "artist": "Suchmos", "title": "Pacific"},
    {"song_id": 33071060, "artist": "Suchmos", "title": "GIRL"},
    {"song_id": 33071068, "artist": "Suchmos", "title": "Miree BAY ver."},
]

PROGRESS_FILES = {
    "city-pop": WORK / "PLAYLIST_CITYPOP_PROGRESS.json",
    "daily": WORK / "DAILY_2026_10_20_PROGRESS.json",
    "藤井風": WORK / "FUJII_KAZE_PROGRESS.json",
    "Suchmos": WORK / "SUCHMOS_PROGRESS.json",
    "Laufey": WORK / "LAUFEY_PROGRESS.json",
}


def ssh(cmd: str, timeout=900) -> subprocess.CompletedProcess:
    if T.is_hora(): return T.route_old(cmd, timeout)   # 防绕回旧机
    env = os.environ.copy()
    env["SSHPASS"] = open(SSHPASS_FILE).read().strip()
    return subprocess.run(
        ["sshpass", "-e", "ssh", "-o", "StrictHostKeyChecking=no",
         "-o", "ConnectTimeout=20", SSH_HOST, cmd],
        capture_output=True, text=True, timeout=timeout, env=env,
    )


def alist_token() -> str:
    r = ssh(
        "PASS=$(cat /root/.alist_admin_pass); "
        "curl -s -X POST http://<REDACTED_IP>:5244/api/auth/login "
        "-H 'Content-Type: application/json' "
        "-d \"{\\\"username\\\":\\\"admin\\\",\\\"password\\\":\\\"$PASS\\\"}\""
    )
    return (json.loads(r.stdout).get("data") or {}).get("token") or ""


def alist_mkdir(token: str, path: str):
    parts = path.strip("/").split("/")
    cur = ""
    for p in parts:
        cur += "/" + p
        ssh(
            "TOKEN=%s; curl -s http://<REDACTED_IP>:5244/api/fs/mkdir "
            "-H \"Authorization: $TOKEN\" -H 'Content-Type: application/json' "
            "-d %s >/dev/null" % (json.dumps(token), shlex.quote(json.dumps({"path": cur}))),
            timeout=60,
        )


def alist_list(token: str, path: str) -> list:
    r = ssh(
        "TOKEN=%s; curl -s http://<REDACTED_IP>:5244/api/fs/list "
        "-H \"Authorization: $TOKEN\" -H 'Content-Type: application/json' "
        "-d %s" % (json.dumps(token), shlex.quote(json.dumps({"path": path, "page": 1, "per_page": 500}))),
        timeout=60,
    )
    try:
        d = json.loads(r.stdout)
    except Exception:
        return []
    if d.get("code") != 200:
        return []
    return [x.get("name") for x in ((d.get("data") or {}).get("content") or [])]


def alist_rename(token: str, src_path: str, new_name: str) -> bool:
    r = ssh(
        "TOKEN=%s; curl -s http://<REDACTED_IP>:5244/api/fs/rename "
        "-H \"Authorization: $TOKEN\" -H 'Content-Type: application/json' "
        "-d %s" % (json.dumps(token), shlex.quote(json.dumps({"path": src_path, "name": new_name}))),
        timeout=60,
    )
    try:
        return json.loads(r.stdout).get("code") == 200
    except Exception:
        return False


def safe_name(s: str) -> str:
    for ch in '\\/:*?"<>|、':
        s = s.replace(ch, "_")
    while ".." in s:
        s = s.replace("..", ".")
    return s.strip(" .")


def preferred_filename(artist: str, title: str, fmt: str) -> str:
    return f"{safe_name(artist)} - {safe_name(title)}.{fmt}"


def title_match(filename: str, title: str) -> bool:
    if not (filename.endswith(".flac") or filename.endswith(".mp3")):
        return False
    stem = filename.rsplit(".", 1)[0]
    if " - " in stem:
        t = stem.split(" - ", 1)[1]
        if t == title or t == safe_name(title):
            return True
    # weaker: exact preferred stem
    return filename.startswith(safe_name(title)) or f" - {title}." in filename or f" - {safe_name(title)}." in filename


def find_existing(names: list, title: str, artist: str):
    for fmt in ("flac", "mp3"):
        pref = preferred_filename(artist, title, fmt)
        if pref in names:
            return pref
    for n in names:
        if title_match(n, title):
            return n
    return None


def kill_remote(song_id: int):
    try:
        ssh(
            f"pkill -f 'download-netease-single.py {song_id}' || true; "
            f"pkill -f 'timeout .*download-netease-single.py {song_id}' || true; "
            f"rm -f /opt/music-dl/tmp/*",
            timeout=30,
        )
    except Exception:
        pass


def download_once(token: str, song: dict, dest: str, batch: str = "", ignore_mp3: bool = False) -> dict:
    song_id = song["song_id"]
    artist = song["artist"]
    title = song["title"]
    names = alist_list(token, dest)
    if ignore_mp3:  # retry of a downgraded song: only an existing FLAC counts
        names = [n for n in names if n.endswith(".flac")]
    hit = find_existing(names, title, artist)
    if hit:
        fmt = hit.rsplit(".", 1)[-1]
        row = {
            "title": title, "artist": artist,
            "alist_path": f"{dest}/{hit}", "format": fmt,
            "ok": "skip", "reason": "already_exists", "song_id": song_id,
        }
        # local 模式：目录里有名不够，必须本地 verified；未过则继续下载/校验链
        if audio_dest_mode() == "local":
            row = attach_local_evidence(row)
            if row.get("ok") == "skip":
                return row
            # 未 verified：不 return，落入下载（可能补账本或重下）
            print(f"local skip rejected ({row.get('reason')}), will fetch: {title}", flush=True)
        else:
            return row

    print(f"DL {artist} / {title} id={song_id} -> {dest}", flush=True)
    # timeout on remote so CDN hang cannot block forever
    cmd = (
        f"MUSIC_BATCH={shlex.quote(batch or '')} "
        f"timeout {REMOTE_TIMEOUT} /opt/music-dl/download-netease-single.py "
        f"{song_id} {shlex.quote(dest)}; echo EXIT:$?"
    )
    result = None
    err = None
    special = None
    try:
        if T.is_hora():
            r = T.hctl("dl_single", timeout=REMOTE_TIMEOUT + 90, song_id=song_id, dest=dest, batch=batch or "",
                       timeout_s=REMOTE_TIMEOUT, dry_run=os.environ.get("DL_DRY_RUN") == "1",
                       audio_dest=audio_dest_mode())
        else:
            r = ssh(cmd, timeout=REMOTE_TIMEOUT + 60)
        stdout = (r.stdout or "").strip().splitlines()
        stderr = (r.stderr or "")
        if stderr:
            print(stderr[-2000:], file=sys.stderr, flush=True)
        exit_line = next((l for l in reversed(stdout) if l.startswith("EXIT:")), "")
        for line in reversed(stdout):
            line = line.strip()
            if line.startswith("{"):
                try:
                    result = json.loads(line)
                    break
                except Exception:
                    pass
        if exit_line == f"EXIT:{EXIT_LOGIN_INVALID}" or (result or {}).get("login_invalid"):
            special = "login_invalid"
        elif exit_line == f"EXIT:{EXIT_DOWNGRADE}" or (result or {}).get("downgraded"):
            special = "downgraded_mp3"
        elif exit_line == "EXIT:44" or (result or {}).get("unavailable"):
            special = "unavailable"  # 不可下载/下架 (2026-11-03): skip, never queued as retry
        elif "flac_fetch_failed" in str((result or {}).get("error") or ""):
            special = "other"
        if not (result and result.get("ok")):
            if "EXIT:124" in exit_line or (r.returncode == 0 and "EXIT:124" in (r.stdout or "")):
                err = f"remote_timeout_{REMOTE_TIMEOUT}s"
            else:
                err = (result or {}).get("error") if result else (
                    stdout[-2] if len(stdout) >= 2 else (stdout[-1] if stdout else f"rc={r.returncode}")
                )
            kill_remote(song_id)
    except subprocess.TimeoutExpired:
        err = "ssh_timeout"
        kill_remote(song_id)
    except Exception as e:
        err = str(e)[:300]
        kill_remote(song_id)

    if not (result and result.get("ok")):
        row = {
            "title": title, "artist": artist,
            "alist_path": None, "format": None,
            "ok": "fail", "reason": str(err)[:500], "song_id": song_id,
        }
        if special:
            row["retry_reason"] = special
            if special == "login_invalid":
                row["reason"] = "login_invalid"
            elif special == "unavailable":
                row["ok"] = "skip_unavailable"
                row["reason"] = "不可下载/下架: " + str((result or {}).get("reason") or "")
        return row

    uploaded = result.get("alist_path") or ""
    fmt = result.get("format") or uploaded.rsplit(".", 1)[-1]
    want = preferred_filename(artist, title, fmt)
    actual = uploaded.rsplit("/", 1)[-1] if uploaded else ""
    final = uploaded
    if actual and actual != want:
        print(f"RENAME {actual} -> {want}", flush=True)
        if alist_rename(token, uploaded, want):
            final = f"{dest}/{want}"
    row = {
        "title": title, "artist": artist,
        "alist_path": final, "format": fmt,
        "ok": "ok", "song_id": song_id,
        "netease_title": result.get("title"),
        "netease_artist": result.get("artist"),
        "storage": result.get("storage"),
        "verify": result.get("verify"),
        "local_path": result.get("local_path"),
    }
    # local：下载 JSON 证据不足时再查 local_stat；未 verified 不得记 ok
    if audio_dest_mode() == "local":
        row = attach_local_evidence(row, final)
    else:
        row.setdefault("storage", result.get("storage") or "115")
    return row


def download_with_retry(token: str, song: dict, dest: str, batch: str = "", ignore_mp3: bool = False) -> dict:
    row = download_once(token, song, dest, batch, ignore_mp3)
    if row.get("ok") == "fail" and "timeout" in str(row.get("reason") or ""):
        print(f"RETRY after timeout: {song['title']}", flush=True)
        time.sleep(5)
        token2 = alist_token() or token
        row = download_once(token2, song, dest, batch, ignore_mp3)
    return row


def queue_retry(batch: str, dest: str, songs: list, reason: str, detail: str = ""):
    if not rq or not songs:
        return
    try:
        n = rq.add([{"song_id": s["song_id"], "title": s["title"], "artist": s["artist"],
                     "dest": dest, "reason": reason, "batch": batch, "detail": detail} for s in songs])
        print(f"RETRY_QUEUE +{n} ({reason}): " + ", ".join(s["title"] for s in songs), flush=True)
    except Exception as e:
        print(f"WARN retry queue write failed: {e}", file=sys.stderr, flush=True)


def merge_vps_retry():
    if not rq:
        return
    try:
        got, new = rq.merge_vps()
        if got:
            print(f"merged {got} VPS retry records ({new} new) into RETRY_QUEUE.json", flush=True)
    except Exception as e:
        print(f"WARN merge VPS retry failed: {e}", file=sys.stderr, flush=True)


def login_ok_or_stop(batch: str, dest: str, pending: list, progress_path: Path, ordered: list):
    """Pre-batch login check; on invalid login queue everything pending and exit 42."""
    if not rq:
        return
    try:
        st, code = rq.check_login_remote(max_age=600)
    except Exception as e:
        print(f"WARN login precheck failed to run: {e}", file=sys.stderr, flush=True)
        return
    if code == EXIT_LOGIN_INVALID:
        print(f"=== BATCH {batch} LOGIN INVALID: {st.get('detail')} ===", flush=True)
        queue_retry(batch, dest, pending, "login_invalid", st.get("detail") or "")
        merge_vps_retry()
        save_progress(batch, dest, ordered, progress_path)
        print(f"=== BATCH {batch} STOPPED reason=login_invalid queued={len(pending)} ===", flush=True)
        sys.exit(EXIT_LOGIN_INVALID)
    print(f"login precheck: rc={code} {st.get('detail')}", flush=True)


def line(row: dict) -> str:
    path = row.get("alist_path") or "-"
    fmt = row.get("format") or "-"
    ok = row.get("ok")
    if ok == "fail":
        return f"{row['title']} | {row['artist']} | {path} | {fmt} | fail({row.get('reason')})"
    if ok == "skip":
        return f"{row['title']} | {row['artist']} | {path} | {fmt} | skip"
    if ok == "skip_unavailable":
        return f"{row['title']} | {row['artist']} | - | - | 下架/不可下载({row.get('reason')})"
    return f"{row['title']} | {row['artist']} | {path} | {fmt} | ok"


def load_done(path: Path) -> dict:
    """进度里的历史 ok/skip：local 模式下必须能证明本地 verified，否则不算完成（会重跑该首）。"""
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    mode = audio_dest_mode()
    for r in data.get("results") or []:
        if r.get("ok") not in ("ok", "skip", "skipped") or not r.get("song_id"):
            continue
        r = dict(r)
        if r["ok"] == "skipped":
            r["ok"] = "skip"
        if mode == "local":
            rr = attach_local_evidence(dict(r), r.get("alist_path"))
            # attach 在未 verified 时会把 ok 改成 fail
            if rr.get("ok") == "fail" or not local_receipt_ok(rr):
                continue
            rr["ok"] = "skip"  # 已在本地 verified → 本轮视为 skip
            r = rr
        out[r["song_id"]] = r
    return out



def save_progress(batch: str, dest: str, results: list, path: Path):
    path.write_text(json.dumps({
        "batch": batch,
        "dest": dest,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "results": results,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def resolve_fujii_folder(token: str) -> str:
    """Prefer existing near-name folder under /115/音乐库/歌手/."""
    parent = "/115/音乐库/歌手"
    names = alist_list(token, parent)
    for cand in ("藤井風", "藤井风", "Fujii Kaze", "藤井 風"):
        if cand in names:
            print(f"Using existing artist folder: {cand}", flush=True)
            return f"{parent}/{cand}"
    # fuzzy
    for n in names:
        if n and ("藤井" in n) and ("風" in n or "风" in n or "Kaze" in n or "kaze" in n):
            print(f"Using fuzzy artist folder: {n}", flush=True)
            return f"{parent}/{n}"
    return FUJII_DEST


def run_batch(batch: str, songs: list, dest: str, progress_path: Path, mid_every: int = 0):
    token = alist_token()
    if not token:
        print("FATAL alist login", flush=True)
        sys.exit(1)
    if batch == "藤井風":
        dest = resolve_fujii_folder(token)
    alist_mkdir(token, dest)
    done = load_done(progress_path)
    results = []
    # keep prior ordered results for songs in this batch
    for s in songs:
        if s["song_id"] in done:
            results.append(done[s["song_id"]])

    print(f"=== BATCH {batch} START dest={dest} total={len(songs)} already={len(done)} ===", flush=True)
    pending = [s for s in songs if s["song_id"] not in done]
    if pending:
        login_ok_or_stop(batch, dest, pending, progress_path,
                         [done[s["song_id"]] for s in songs if s["song_id"] in done])
    for i, song in enumerate(songs):
        if song["song_id"] in done:
            print(f"[{i+1}/{len(songs)}] already {song['title']}", flush=True)
            continue
        token = alist_token() or token
        row = download_with_retry(token, song, dest, batch)
        results.append(row)
        done[song["song_id"]] = row
        # rewrite ordered
        ordered = [done[s["song_id"]] for s in songs if s["song_id"] in done]
        save_progress(batch, dest, ordered, progress_path)
        print(f"[{i+1}/{len(songs)}] {row.get('ok')} {row.get('alist_path') or row.get('reason')}", flush=True)
        rr = row.get("retry_reason")
        if rr == "login_invalid":
            rest = [s for s in songs[i:] if s["song_id"] not in done or s["song_id"] == song["song_id"]]
            queue_retry(batch, dest, rest, "login_invalid", "remote exit 42")
            merge_vps_retry()
            print(f"=== BATCH {batch} STOPPED reason=login_invalid queued={len(rest)} ===", flush=True)
            for r in ordered:
                print(line(r), flush=True)
            sys.exit(EXIT_LOGIN_INVALID)
        if rr in ("downgraded_mp3", "other"):
            queue_retry(batch, dest, [song], rr, str(row.get("reason") or "")[:200])
        if mid_every and (i + 1) % mid_every == 0:
            print(f"=== MID {batch} {i+1}/{len(songs)} ===", flush=True)
            for r in ordered:
                print(line(r), flush=True)

    ordered = [done[s["song_id"]] for s in songs if s["song_id"] in done]
    save_progress(batch, dest, ordered, progress_path)
    print(f"=== BATCH {batch} REPORT ===", flush=True)
    for r in ordered:
        print(line(r), flush=True)
    ok_n = sum(1 for r in ordered if r.get("ok") in ("ok", "skip"))
    fail_n = sum(1 for r in ordered if r.get("ok") == "fail")
    merge_vps_retry()
    print(f"=== BATCH {batch} DONE ok+skip={ok_n} fail={fail_n} ===", flush=True)
    return ordered, dest


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which in ("all", "citypop", "city-pop"):
        run_batch("city-pop", CITYPOP, CITYPOP_DEST, PROGRESS_FILES["city-pop"])
    if which in ("all", "daily"):
        run_batch("daily", DAILY, DAILY_DEST, PROGRESS_FILES["daily"])
    if which in ("all", "fujii", "藤井風"):
        # write playlist json for Lomax
        pl = [{"title": s["title"], "artist": s["artist"], "id": s["song_id"]} for s in FUJII]
        (WORK / "FUJII_KAZE_PLAYLIST.json").write_text(
            json.dumps({"artist": "藤井風", "count": len(pl), "songs": pl}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"FUJII playlist written count={len(pl)}", flush=True)
        run_batch("藤井風", FUJII, FUJII_DEST, PROGRESS_FILES["藤井風"], mid_every=10)
    if which in ("all", "suchmos", "Suchmos"):
        run_batch("Suchmos", SUCHMOS, SUCHMOS_DEST, PROGRESS_FILES["Suchmos"])
    if which in ("all", "laufey", "Laufey"):
        run_batch("Laufey", LAUFEY, LAUFEY_DEST, PROGRESS_FILES["Laufey"])



# ---- 迁 hora 候选：目标为 hora 时改走 hctl（输出与原 bash 一致） ----
if T.is_hora():
    def alist_token() -> str:
        r = T.hctl("alist_login", timeout=60)
        try: return (json.loads(r.stdout).get("data") or {}).get("token") or ""
        except Exception: return ""

    def alist_mkdir(token: str, path: str):
        parts = path.strip("/").split("/"); cur = ""
        for p in parts:
            cur += "/" + p
            T.hctl("alist_mkdir", timeout=60, token=token, body={"path": cur})

    def alist_list(token: str, path: str) -> list:
        r = T.hctl("alist_list", timeout=60, token=token, body={"path": path, "page": 1, "per_page": 500})
        try: d = json.loads(r.stdout)
        except Exception: return []
        if d.get("code") != 200: return []
        return [x.get("name") for x in ((d.get("data") or {}).get("content") or [])]

    def alist_rename(token: str, src_path: str, new_name: str) -> bool:
        r = T.hctl("alist_rename", timeout=60, token=token, body={"path": src_path, "name": new_name})
        try: return json.loads(r.stdout).get("code") == 200
        except Exception: return False

    def kill_remote(song_id: int):
        try: T.hctl("kill_dl", timeout=60, song_id=song_id)
        except Exception: pass

if __name__ == "__main__":
    main()
