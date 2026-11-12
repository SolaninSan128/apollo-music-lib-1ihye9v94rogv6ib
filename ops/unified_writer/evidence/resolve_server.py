#!/usr/bin/env python3
"""PLAY_RESOLVE for 八音盒 GitHub Pages.
GET /music/resolve?path=...  -> JSON {"url": "https://...signed playable"}
Also returns play_url/audio_src for compatibility.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json, os, urllib.parse, urllib.request, pathlib

ALIST = os.environ.get("ALIST_URL", "http://127.0.0.1:5244")
PASS_FILE = os.environ.get("ALIST_PASS_FILE", "/root/.alist_admin_pass")
PUBLIC_ORIGIN = os.environ.get("MUSIC_PUBLIC_ORIGIN", "https://207.57.120.158.sslip.io:8443")
MUSIC_ROOT = "/115/音乐库"
LOCAL_ROOT = os.environ.get("HARMONIA_LOCAL_ROOT", r"D:\Harmonia\音乐库")
LOCAL_LEDGER = os.environ.get("HARMONIA_LOCAL_LEDGER", r"D:\Harmonia\music-migrate\ledger.jsonl")
LOCAL_URL_PREFIX = "/local-audio"  # nginx -> this server; 仅账本 verified 才启用
_verified_cache = {"mtime": None, "map": {}}

def _load_verified():
    """src(/115/音乐库/...) -> {dst, size, verify, sha1_dst}；仅 copied/same_skip。"""
    try:
        mtime = os.path.getmtime(LOCAL_LEDGER) if os.path.exists(LOCAL_LEDGER) else None
    except OSError:
        mtime = None
    if mtime is not None and _verified_cache["mtime"] == mtime:
        return _verified_cache["map"]
    m = {}
    if os.path.exists(LOCAL_LEDGER):
        with open(LOCAL_LEDGER, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try: e = json.loads(line)
                except Exception: continue
                if e.get("status") in ("copied", "same_skip") and e.get("src") and e.get("dst"):
                    m[e["src"]] = e
    _verified_cache["mtime"] = mtime
    _verified_cache["map"] = m
    return m

def local_file_for(path115):
    e = _load_verified().get(path115)
    if not e: return None
    dst = e.get("dst") or ""
    if not dst or not os.path.isfile(dst): return None
    try:
        if e.get("size") and os.path.getsize(dst) != int(e["size"]):
            return None
    except OSError:
        return None
    return e

def token():
    passwd = pathlib.Path(PASS_FILE).read_text().strip()
    body = json.dumps({"username": os.environ.get("ALIST_USER", "admin"), "password": passwd}).encode()
    req = urllib.request.Request(ALIST + "/api/auth/login", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())["data"]["token"]

def fs_get(tok, path):
    body = json.dumps({"path": path, "password": ""}).encode()
    req = urllib.request.Request(ALIST + "/api/fs/get", data=body, headers={"Authorization": tok, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

def fs_list_refresh(tok, path):
    body = json.dumps({"path": path, "refresh": True, "per_page": 1}).encode()
    req = urllib.request.Request(ALIST + "/api/fs/list", data=body, headers={"Authorization": tok, "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=30).read()
    except Exception:
        pass

def normalize_path(raw, prefer=None):
    raw = urllib.parse.unquote((raw or "").strip())
    if not raw:
        raise ValueError("missing path")
    if not raw.startswith("/"):
        raw = MUSIC_ROOT.rstrip("/") + "/" + raw.lstrip("/")
    # Accept paths relative to /115/音乐库 or full /115/...
    if prefer in ("flac", "mp3") and not raw.lower().endswith((".flac", ".mp3", ".m4a")):
        raw = raw + "." + prefer
    if prefer == "mp3" and raw.lower().endswith(".flac"):
        raw = raw[:-5] + ".mp3"
    if prefer == "flac" and raw.lower().endswith(".mp3"):
        raw = raw[:-4] + ".flac"
    return raw

def to_proxy_path(path):
    """/115/... -> /115proxy/... so Alist web_proxy streams with audio MIME."""
    if path.startswith("/115proxy/"):
        return path
    if path.startswith("/115/"):
        return "/115proxy/" + path[len("/115/"):]
    return path

def enc_path(path):
    parts = path.split("/")
    return "/".join(urllib.parse.quote(p, safe="") if p else "" for p in parts)

class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("[resolve]", fmt % args)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Cache-Control", "no-store")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _serve_local(self, rel):
        """Range-capable local audio from D:\\Harmonia\\音乐库 (verified paths only via resolve)."""
        rel = urllib.parse.unquote(rel).lstrip("/").replace("/", os.sep)
        # prevent traversal
        root = os.path.abspath(LOCAL_ROOT)
        full = os.path.abspath(os.path.join(LOCAL_ROOT, rel))
        if not full.startswith(root + os.sep) and full != root:
            self.send_response(403); self.end_headers(); return
        if not os.path.isfile(full):
            self.send_response(404); self.end_headers(); return
        size = os.path.getsize(full)
        rng = self.headers.get("Range")
        ctype = "audio/flac" if full.lower().endswith(".flac") else ("audio/mpeg" if full.lower().endswith(".mp3") else "application/octet-stream")
        if full.lower().endswith(".m4a"): ctype = "audio/mp4"
        start, end = 0, size - 1
        status = 200
        if rng and rng.startswith("bytes="):
            try:
                a,b = rng[6:].split("-",1)
                if a: start = int(a)
                if b: end = int(b)
                end = min(end, size-1); start = max(0, start)
                if start > end: raise ValueError("bad range")
                status = 206
            except Exception:
                self.send_response(416); self.end_headers(); return
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("X-Harmonia-Source", "local")
        self._cors()
        self.end_headers()
        if self.command == "HEAD": return
        with open(full, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(1024*1024, left))
                if not chunk: break
                self.wfile.write(chunk); left -= len(chunk)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path.startswith(LOCAL_URL_PREFIX + "/"):
            return self._serve_local(u.path[len(LOCAL_URL_PREFIX):])
        if u.path not in ("/music/resolve", "/resolve"):
            self.send_response(404)
            self._cors()
            self.end_headers()
            self.wfile.write(b'{"ok":false,"error":"not found"}')
            return
        qs = urllib.parse.parse_qs(u.query)
        try:
            prefer = (qs.get("prefer") or qs.get("format") or [None])[0]
            path115 = normalize_path((qs.get("path") or [None])[0], prefer)
            # local-first: only when ledger verified
            loc = local_file_for(path115)
            if loc:
                xfproto = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
                host = (self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "").split(",")[0].strip()
                host_only = host.rsplit(":", 1)[0] if host.count(":") == 1 and not host.startswith("[") else host
                if host and ("corgihora.bid" in host_only or "trycloudflare" in host_only or host_only.endswith(".cfargotunnel.com")):
                    play_origin = f"https://{host_only}"
                elif host and not host.startswith("127."):
                    scheme = "https" if xfproto == "https" else (xfproto or "http")
                    play_origin = f"{scheme}://{host_only}"
                else:
                    play_origin = PUBLIC_ORIGIN
                rel = path115[len(MUSIC_ROOT):].lstrip("/") if path115.startswith(MUSIC_ROOT) else path115.lstrip("/")
                play_url = f"{play_origin}{LOCAL_URL_PREFIX}/{enc_path(rel).lstrip('/')}"
                out = {"ok": True, "url": play_url, "play_url": play_url, "audio_src": play_url,
                       "path": path115, "source": "local", "verify": loc.get("verify"),
                       "name": os.path.basename(loc.get("dst") or path115), "size": loc.get("size"),
                       "format": "flac" if path115.lower().endswith(".flac") else ("mp3" if path115.lower().endswith(".mp3") else "unknown")}
                body = json.dumps(out, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self._cors()
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Harmonia-Source", "local")
                self.end_headers()
                self.wfile.write(body)
                return
            path_play = to_proxy_path(path115)
            tok = token()
            data = fs_get(tok, path_play)
            if data.get("code") != 200:
                # refresh parent then retry (115proxy cache lag)
                parent = path_play.rsplit("/", 1)[0]
                fs_list_refresh(tok, "/115proxy")
                fs_list_refresh(tok, parent)
                data = fs_get(tok, path_play)
            if data.get("code") != 200:
                # fall back to /115 for metadata then still play via proxy path if possible
                data = fs_get(tok, path115)
            if data.get("code") != 200:
                raise RuntimeError(data.get("message") or "fs/get failed")
            info = data["data"]
            sign = info.get("sign") or ""
            # Re-get on proxy path specifically for correct sign when fallback used
            if not path_play.startswith("/115proxy/") or info.get("name") is None:
                pass
            data2 = fs_get(tok, path_play)
            if data2.get("code") == 200:
                info = data2["data"]
                sign = info.get("sign") or sign
            # Request-aware origin:
            # - trycloudflare.com / *.cfargotunnel.com => always https (Pages needs HTTPS url)
            # - bare IP / musicbox http => http is fine
            # - sslip.io => PUBLIC_ORIGIN (:8443)
            xfproto = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
            host = (self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "").split(",")[0].strip()
            # strip port for matching
            host_only = host.rsplit(":", 1)[0] if host.count(":") == 1 and not host.startswith("[") else host
            tunnelish = (
                "trycloudflare.com" in host_only
                or host_only.endswith(".cfargotunnel.com")
                or "cloudflare" in (self.headers.get("CF-Ray") or "").lower()
                or "trycloudflare" in host_only
            )
            if tunnelish and host_only:
                play_origin = f"https://{host_only}"
            elif host and not host.startswith("127.") and "sslip.io" not in host_only:
                # Direct IP / custom host via nginx :80
                is_ip = all(part.isdigit() for part in host_only.split("."))
                scheme = "https" if xfproto == "https" else (xfproto or "http")
                if is_ip:
                    # keep port if non-default was in Host
                    play_origin = f"{scheme}://{host}" if (":" in host and not host.endswith(":80")) else f"{scheme}://{host_only}"
                else:
                    play_origin = f"{scheme}://{host_only}"
            else:
                play_origin = PUBLIC_ORIGIN
            play_url = f"{play_origin}/p{enc_path(path_play)}?sign={urllib.parse.quote(sign, safe='')}"
            fmt = "flac" if path115.lower().endswith(".flac") else ("mp3" if path115.lower().endswith(".mp3") else "unknown")
            out = {
                "ok": True,
                "url": play_url,
                "play_url": play_url,
                "audio_src": play_url,
                "path": path115,
                "proxy_path": path_play,
                "name": info.get("name"),
                "size": info.get("size"),
                "format": fmt,
                "sign": sign,
                "source": "115proxy",
            }
            body = json.dumps(out, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self._cors()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            body = json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self._cors()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

if __name__ == "__main__":
    host, port = "127.0.0.1", int(os.environ.get("MUSIC_RESOLVE_PORT", "5260"))
    print(f"PLAY_RESOLVE on {host}:{port} origin={PUBLIC_ORIGIN}")
    ThreadingHTTPServer((host, port), H).serve_forever()
