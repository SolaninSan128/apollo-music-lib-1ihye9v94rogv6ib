#!/usr/bin/env python3
"""PLAY_RESOLVE for 八音盒 GitHub Pages.
GET /music/resolve?path=...  -> JSON {"url": "https://...signed playable"}
Also returns play_url/audio_src for compatibility.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json, os, urllib.parse, urllib.request, pathlib

ALIST = "http://127.0.0.1:5244"
PASS_FILE = "/root/.alist_admin_pass"
PUBLIC_ORIGIN = os.environ.get("MUSIC_PUBLIC_ORIGIN", "https://207.57.120.158.sslip.io:8443")
MUSIC_ROOT = "/115/音乐库"

def token():
    passwd = pathlib.Path(PASS_FILE).read_text().strip()
    body = json.dumps({"username": "admin", "password": passwd}).encode()
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

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
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
    host, port = "127.0.0.1", 5260
    print(f"PLAY_RESOLVE on {host}:{port} origin={PUBLIC_ORIGIN}")
    ThreadingHTTPServer((host, port), H).serve_forever()
