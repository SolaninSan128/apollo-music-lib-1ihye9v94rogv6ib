"""八音盒写入目标切换（2026-11-08 迁 hora 候选）。
HARMONIA_TARGET=yecaoyun（默认，行为同原脚本） | hora（正式 C:/harmonia/root） | hora-test（隔离副本 C:/harmonia/test-root）
接口均用原 Linux 路径（/opt/... /var/www/...），hora 时自动映射。
"""
import json, os, subprocess, time
def _file_target():
    f = os.path.join(os.path.dirname(os.path.abspath(__file__)), "HARMONIA_TARGET")
    try: return open(f).read().strip() or None
    except Exception: return None
TARGET = os.environ.get("HARMONIA_TARGET") or _file_target() or "yecaoyun"   # 切换后同目录 HARMONIA_TARGET 文件=hora，裸跑/裸 import 也走 hora
VPS = "root@<REDACTED_HOST>"
VPASS = "<REDACTED_VPS_SSHPASS_PATH>" if os.path.exists("<REDACTED_VPS_SSHPASS_PATH>") else "<REDACTED_VPS_SECRET_PATH>"
HKEY = "<REDACTED_SSH_KEY_PATH>"
HPROXY = "<REDACTED_TAILSCALE_PATH> --socket=<REDACTED_TAILSCALE_SOCK> nc %h %p"
HOPT = ["-i", HKEY, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ProxyCommand=" + HPROXY]
HHOST = "Administrator@<REDACTED_HOST>"
ROOT = {"hora": "C:/harmonia/root", "hora-test": "C:/harmonia/test-root"}.get(TARGET)
PY = "C:/Python313/python.exe"

def is_hora(): return ROOT is not None
def label(): return TARGET
def rpath(p):
    """Linux 路径 -> 目标路径"""
    return ROOT + p if is_hora() else p

def _ssh(cmd, timeout=60, check=True, capture=True):
    if is_hora():
        full = ["ssh"] + HOPT + [HHOST, cmd]
    else:
        full = ["sshpass", "-f", VPASS, "ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=15", VPS, cmd]
    for i in range(3):
        r = subprocess.run(full, capture_output=capture, text=True, timeout=timeout, encoding="utf-8", errors="replace")
        if r.returncode != 255: break
        time.sleep(4)
    if check and r.returncode != 0:
        raise subprocess.CalledProcessError(r.returncode, "ssh(%s)" % TARGET, r.stdout, r.stderr)
    return r

def _scp(files, dest, timeout=600):
    if is_hora():
        full = ["scp", "-q"] + HOPT + list(files) + ["%s:%s" % (HHOST, dest)]
    else:
        full = ["sshpass", "-f", VPASS, "scp", "-q", "-o", "StrictHostKeyChecking=no"] + list(files) + ["%s:%s" % (VPS, dest)]
    for i in range(3):
        r = subprocess.run(full, timeout=timeout)
        if r.returncode == 0: return
        time.sleep(5)
    raise subprocess.CalledProcessError(r.returncode, "scp(%s)" % TARGET)

def _ps(script, **kw):
    # Windows OpenSSH 默认 cmd；统一走 powershell
    return _ssh('powershell -NoProfile -Command "%s"' % script.replace('"', '\\"'), **kw)

def cat(path, timeout=60):
    if is_hora():
        r = _ssh('type "%s"' % rpath(path).replace("/", "\\"), timeout=timeout, check=False)
    else:
        r = _ssh("cat %s" % path, timeout=timeout, check=False)
    return r

def mkdir(path):
    if is_hora(): _ps("New-Item -ItemType Directory -Force '%s' | Out-Null" % rpath(path))
    else: _ssh("mkdir -p %s && chmod 755 %s" % (path, path))

def put_files(files, dirpath, timeout=600):
    files = list(files)
    if is_hora():   # 原子独占 add-only：先传到同盘唯一暂存目录，再由 hora 侧 os.rename 独占落位；同名同 sha=跳过，同名异 sha=冲突报错不覆盖
        import json as _j
        stage = dirpath.rstrip("/") + "/.stage-%d-%d" % (os.getpid(), int(time.time() * 1000))
        mkdir(stage)
        for i in range(0, len(files), 40):
            _scp(files[i:i + 40], rpath(stage) + "/", timeout=timeout)
        r = hctl("add_only", timeout=timeout, stage=stage, dest=dirpath)
        res = _j.loads((r.stdout or "{}").strip().splitlines()[-1])
        print("put_files add-only: added %d same %d conflict %d" % (len(res["added"]), len(res["same"]), len(res["conflict"])))
        if r.returncode: raise RuntimeError("put_files 冲突（同名不同内容，未覆盖）: %s" % res["conflict"][:5])
        return res
    for i in range(0, len(files), 40):
        _scp(files[i:i + 40], rpath(dirpath) + "/", timeout=timeout)
    _ssh("chmod 644 %s/*.jpg" % dirpath)

def put_json_atomic(local, remote_file, backup_tag=None):
    """scp 到同目录临时名 -> 校验 JSON -> (可选备份) -> 原子替换"""
    d, n = remote_file.rsplit("/", 1); rtmp = "%s/.%s.upload" % (d, n)
    _scp([local], rpath(rtmp), timeout=120)
    if is_hora():
        bk = ""
        if backup_tag:
            bk = "Copy-Item -Force '%s' ('C:/harmonia/backup/music-feed.json.bak-' + (Get-Date -Format yyyyMMdd-HHmmss) + '-%s');" % (rpath(remote_file), backup_tag)
        _ps("& '%s' C:/harmonia/jsoncheck.py '%s'; if($LASTEXITCODE){exit 3}; %s Move-Item -Force '%s' '%s'" % (PY, rpath(rtmp), bk, rpath(rtmp), rpath(remote_file)))
    else:
        bk = ("cp -p %s /root/music-feed.json.bak-$(date +%%Y%%m%%d-%%H%%M%%S)-%s && " % (remote_file, backup_tag)) if backup_tag else ""
        _ssh("%spython3 -c 'import json;json.load(open(\"%s\"))' && chmod 644 %s && mv -f %s %s" % (bk, rtmp, rtmp, rtmp, remote_file))

# ---- hora 远程操作（hctl.py），与 yecaoyun 原 bash 命令输出兼容 ----
import base64 as _b64
HORA_MUSIC_DL = os.environ.get("HARMONIA_MUSIC_DL", "C:\\harmonia\\cand\\music-dl")

def hctl(op, timeout=900, **kw):
    """在 hora 上执行 hctl.py；返回 CompletedProcess（stdout/stderr/returncode 同原命令语义）"""
    assert is_hora()
    arg = _b64.b64encode(json.dumps(dict(kw, op=op), ensure_ascii=False).encode("utf-8")).decode()
    root = ROOT.replace("/", "\\")
    cmd = ("set PYTHONIOENCODING=utf-8&& set MUSIC_DL_HOME=%s&& set HARMONIA_ROOT=%s&& "
           "set FFMPEG=C:\\harmonia\\ffmpeg\\ffmpeg.exe&& set FFPROBE=C:\\harmonia\\ffmpeg\\ffprobe.exe&& "
           "C:\\Python313\\python.exe %s\\hctl.py %s") % (HORA_MUSIC_DL, root, HORA_MUSIC_DL, arg)
    return _ssh(cmd, timeout=timeout, check=False)

def fetch(remote_path, local):
    """取回远端文件（hora 路径映射；/opt/music-dl -> HARMONIA_MUSIC_DL）"""
    if is_hora():
        rp = remote_path
        if rp.startswith("/opt/music-dl/"): rp = HORA_MUSIC_DL.replace("\\", "/") + rp[len("/opt/music-dl"):]
        else: rp = rpath(rp)
        full = ["scp", "-q"] + HOPT + ["%s:%s" % (HHOST, rp), local]
    else:
        full = ["sshpass", "-f", VPASS, "scp", "-q", "-o", "StrictHostKeyChecking=no", "%s:%s" % (VPS, remote_path), local]
    for i in range(3):
        if subprocess.run(full, timeout=120).returncode == 0: return
        time.sleep(4)
    raise RuntimeError("fetch failed: %s" % remote_path)


# ---- 防绕回旧机：hora 模式下任何「旧 VPS ssh 命令」只允许白名单 /opt/music-dl 脚本，转 hctl mdl；其余直接报错 ----
MDL_OK = {"check-avail.py", "check-login.py", "make-login-qr.py", "download-netease-single.py", "download-netease-single-daily.py", "download-netease-id.py"}
def route_old(cmd, timeout=900):
    import shlex, subprocess as _sp
    try: argv = shlex.split(cmd)
    except Exception: argv = []
    if argv and argv[0].startswith("/opt/music-dl/") and os.path.basename(argv[0]) in MDL_OK:
        return hctl("mdl", timeout=timeout, script=os.path.basename(argv[0]), args=argv[1:], timeout_s=max(30, timeout - 30))
    raise RuntimeError("HARMONIA_TARGET=hora：拒绝向旧 VPS 执行命令（防绕回）：%s" % cmd[:80])
