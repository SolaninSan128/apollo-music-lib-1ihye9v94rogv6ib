#!/usr/bin/env python3
"""hora 本地曲库落盘（机器私有）。逻辑路径仍 /115/音乐库/...；物理根 D:\\Harmonia\\音乐库。
HARMONIA_AUDIO_DEST=local|115（默认读 data/AUDIO_DEST 文件，再默认 115）。
完成=本地 size(+可选 sha1) 入账本 verified；不把 115 上传当完成条件。
"""
import hashlib, json, os, shutil, time
from datetime import datetime

LOGIC_ROOT = "/115/音乐库"
LOCAL_ROOT = os.environ.get("HARMONIA_LOCAL_ROOT", r"D:\Harmonia\音乐库")
LEDGER = os.environ.get("HARMONIA_LOCAL_LEDGER", r"D:\Harmonia\music-migrate\ledger.jsonl")
DEST_FILE = os.path.join(os.environ.get("MUSIC_DL_HOME", os.path.dirname(__file__)), "data", "AUDIO_DEST")

def audio_dest():
    v = (os.environ.get("HARMONIA_AUDIO_DEST") or "").strip().lower()
    if v in ("local", "115"):
        return v
    try:
        if os.path.exists(DEST_FILE):
            t = open(DEST_FILE, encoding="utf-8").read().strip().lower()
            if t in ("local", "115"):
                return t
    except Exception:
        pass
    return "115"

def logic_to_local(logic_path: str) -> str:
    p = logic_path.replace("\\", "/")
    if not p.startswith(LOGIC_ROOT):
        raise ValueError(f"not under {LOGIC_ROOT}: {p}")
    rel = p[len(LOGIC_ROOT):].lstrip("/")
    return os.path.join(LOCAL_ROOT, rel.replace("/", os.sep))

def sha1_file(path, buf=1024 * 1024):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            b = f.read(buf)
            if not b:
                break
            h.update(b)
    return h.hexdigest().upper()

def _now():
    return datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")

def append_ledger(entry: dict):
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    with open(LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

def place_local(src_file: str, logic_dir: str, filename: str | None = None, src_sha1: str | None = None) -> dict:
    """把已下载临时文件落到本地库。异内容不覆盖。返回结果 dict（含 alist_path 逻辑路径）。"""
    logic_dir = logic_dir.rstrip("/")
    name = filename or os.path.basename(src_file)
    logic_path = f"{logic_dir}/{name}"
    dst = logic_to_local(logic_path)
    size = os.path.getsize(src_file)
    dst_sha = sha1_file(src_file)
    if src_sha1:
        src_sha1 = src_sha1.upper()
        if dst_sha != src_sha1:
            raise RuntimeError(f"sha1 mismatch before place src={src_sha1} file={dst_sha}")
        verify = "sha1_src+dst"
    else:
        verify = "sha1_dst_only"  # 网易云下行通常无稳定源 hash；记本地 sha1 + size
    if os.path.exists(dst):
        if os.path.getsize(dst) == size and sha1_file(dst) == dst_sha:
            e = {"ts": _now(), "src": logic_path, "dst": dst, "size": size, "sha1_dst": dst_sha,
                 "sha1_src": src_sha1, "status": "same_skip", "verify": verify, "via": "download_local"}
            append_ledger(e)
            return {"ok": True, "alist_path": logic_path, "local_path": dst, "storage": "local",
                    "status": "same_skip", "verify": verify, "size": size, "sha1": dst_sha}
        e = {"ts": _now(), "src": logic_path, "dst": dst, "size_src": size, "size_dst": os.path.getsize(dst),
             "sha1_src": src_sha1, "sha1_new": dst_sha, "sha1_dst": sha1_file(dst),
             "status": "conflict", "verify": "kept_dst", "via": "download_local"}
        append_ledger(e)
        raise RuntimeError(f"conflict: local exists different content: {logic_path}")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    partial = dst + ".partial"
    shutil.copy2(src_file, partial)
    if os.path.getsize(partial) != size:
        raise RuntimeError("partial size mismatch")
    if sha1_file(partial) != dst_sha:
        raise RuntimeError("partial sha1 mismatch")
    os.replace(partial, dst)
    e = {"ts": _now(), "src": logic_path, "dst": dst, "size": size, "sha1_dst": dst_sha,
         "sha1_src": src_sha1, "status": "copied", "verify": verify, "via": "download_local"}
    append_ledger(e)
    return {"ok": True, "alist_path": logic_path, "local_path": dst, "storage": "local",
            "status": "copied", "verify": verify, "size": size, "sha1": dst_sha}

def ledger_latest(logic_path: str) -> dict | None:
    if not os.path.exists(LEDGER):
        return None
    hit = None
    with open(LEDGER, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("src") == logic_path:
                hit = e
    return hit

_SHA_CACHE = {}  # (path, size, mtime_ns) -> sha1；同 size+mtime 复用，避免同进程反复全文件 hash
VERIFY_OK = frozenset({"sha1_src+dst", "sha1_dst_only"})

def _sha1_hex_ok(s) -> bool:
    if not isinstance(s, str):
        return False
    s = s.strip().upper()
    return len(s) == 40 and all(c in "0123456789ABCDEF" for c in s)

def _file_sha1_cached(path: str) -> str:
    st = os.stat(path)
    key = (path, st.st_size, getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
    hit = _SHA_CACHE.get(key)
    if hit:
        return hit
    h = sha1_file(path)
    _SHA_CACHE[key] = h
    return h

def under_local_root(path: str) -> bool:
    root = os.path.abspath(LOCAL_ROOT)
    full = os.path.abspath(path)
    return full == root or full.startswith(root + os.sep)

def local_stat(logic_path: str) -> dict:
    """严格本地核验。
    verified=True 仅当：路径在 LOCAL_ROOT 下、文件存在、size>0、账本 status∈copied/same_skip、
    verify∈{sha1_src+dst,sha1_dst_only}、账本 size 与实文件一致、账本 sha1_dst 为 40 位 hex 且与实文件 sha1 一致。
    每次调用读取实文件 size；sha1 在 (path,size,mtime) 未变时可复用缓存，否则重算（非整库扫描）。
    """
    try:
        dst = logic_to_local(logic_path)
    except ValueError as e:
        return {"ok": False, "exists": False, "verified": False, "error": str(e)}
    if not under_local_root(dst):
        return {"ok": False, "exists": False, "verified": False, "error": "path_outside_local_root", "alist_path": logic_path}
    if not os.path.isfile(dst):
        return {"ok": False, "exists": False, "alist_path": logic_path, "storage": None, "verified": False}
    size = os.path.getsize(dst)
    sha = _file_sha1_cached(dst)
    led = ledger_latest(logic_path)
    verify = (led or {}).get("verify")
    led_size = (led or {}).get("size")
    led_sha = (led or {}).get("sha1_dst")
    verified = False
    reason = None
    if not led:
        reason = "no_ledger"
    elif led.get("status") not in ("copied", "same_skip"):
        reason = f"ledger_status:{led.get('status')}"
    elif verify not in VERIFY_OK:
        reason = f"bad_verify:{verify!r}"
    elif not isinstance(led_size, int) or isinstance(led_size, bool) or led_size <= 0:
        reason = "missing_or_invalid_ledger_size"
    elif led_size != size:
        reason = f"size_mismatch_ledger={led_size}_file={size}"
    elif not _sha1_hex_ok(led_sha or ""):
        reason = "missing_or_invalid_ledger_sha1"
    elif str(led_sha).strip().upper() != sha:
        reason = "sha1_mismatch"
    else:
        verified = True
    out = {"ok": True, "exists": True, "alist_path": logic_path, "local_path": dst,
           "size": size, "sha1": sha, "storage": "local", "verified": verified,
           "verify": verify if verified else verify, "ledger_status": (led or {}).get("status"),
           "ledger_size": led_size, "ledger_sha1": (str(led_sha).upper() if led_sha else None)}
    if not verified:
        out["verify_error"] = reason
    return out
