#!/usr/bin/env python3
"""hora 侧八音盒远程操作入口（box 候选脚本经 ssh 调用；2026-11-08）。
用法: python hctl.py <base64(JSON)>   JSON = {"op": ..., ...}
所有 op 的 stdout/退出码与 yecaoyun 上原 bash 命令保持一致，box 端解析逻辑不用变。
环境: MUSIC_DL_HOME（默认本文件目录） HARMONIA_ROOT（默认 C:/harmonia/root，/opt/... 路径映射用）
      ALIST_URL ALIST_USER ALIST_PASS_FILE（hora 本机 Alist；密码不打印）
"""
import base64, json, os, subprocess, sys, time, urllib.request
HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.environ.get("MUSIC_DL_HOME", HERE)
ROOT = os.environ.get("HARMONIA_ROOT", "C:/harmonia/root")
ALIST = os.environ.get("ALIST_URL", "http://<REDACTED_IP>:5244").rstrip("/")
PY = sys.executable
CHILD_ENV = dict(os.environ, MUSIC_DL_HOME=HOME, PYTHONIOENCODING="utf-8")
CHILD_ENV.setdefault("ALIST_URL", ALIST)
CHILD_ENV.setdefault("ALIST_PASS_FILE", os.path.join(HOME, "data", "alist_pass"))  # hora 本机 Alist 凭据（ACL 仅 SYSTEM/Administrators）

def mp(p):
    """Linux 路径 -> hora 路径；/opt/music-dl/* -> MUSIC_DL_HOME"""
    if p.startswith("/opt/music-dl/") or p == "/opt/music-dl":
        return os.path.join(HOME, p[len("/opt/music-dl/"):]) if p != "/opt/music-dl" else HOME
    return ROOT + p

def alist_post(path, body, token=None):
    h = {"Content-Type": "application/json"}
    if token: h["Authorization"] = token
    r = urllib.request.Request(ALIST + path, data=json.dumps(body).encode(), headers=h)
    try:
        return urllib.request.urlopen(r, timeout=60).read().decode("utf-8", "replace")
    except Exception as e:
        return json.dumps({"code": 599, "message": str(e)})

def py(script, *args, timeout=None):
    return subprocess.run([PY, os.path.join(HOME, script), *map(str, args)], env=CHILD_ENV,
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)

def kill_matching(needle):
    """结束命令行包含 needle 的 python 进程（不碰其他进程）。返回个数。"""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | ? { $_.CommandLine -like '*%s*' -and $_.ProcessId -ne %d } | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force; 1 }") % (needle.replace("'", "''"), os.getpid())
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True)
    return len([x for x in r.stdout.split() if x.strip() == "1"])

def main():
    a = json.loads(base64.b64decode(sys.argv[1]).decode("utf-8"))
    op = a["op"]
    if op == "cat":
        try:
            sys.stdout.buffer.write(open(mp(a["path"]), "rb").read()); return 0
        except FileNotFoundError:
            print("cat: %s: No such file or directory" % a["path"], file=sys.stderr); return 1
    if op == "alist_login":
        pw = open(os.environ.get("ALIST_PASS_FILE", os.path.join(HOME, "data", "alist_pass")), encoding="utf-8").read().strip()
        print(alist_post("/api/auth/login", {"username": os.environ.get("ALIST_USER", "admin"), "password": pw})); return 0
    if op in ("alist_mkdir", "alist_list", "alist_rename"):
        # AUDIO_DEST=local 时：对 /115/音乐库 下路径改走本地盘，返回与 Alist 同形 JSON，避免 work 仍强制依赖 115 列表/改名
        import local_audio as LA
        body = a.get("body") or {}
        if LA.audio_dest() == "local" and str(body.get("path") or "").startswith(LA.LOGIC_ROOT):
            try:
                if op == "alist_list":
                    local = LA.logic_to_local(body["path"])
                    content = []
                    if os.path.isdir(local):
                        for name in sorted(os.listdir(local)):
                            fp = os.path.join(local, name)
                            content.append({"name": name, "is_dir": os.path.isdir(fp), "size": (os.path.getsize(fp) if os.path.isfile(fp) else 0)})
                    print(json.dumps({"code": 200, "message": "local", "data": {"content": content, "total": len(content)}}, ensure_ascii=False)); return 0
                if op == "alist_mkdir":
                    os.makedirs(LA.logic_to_local(body["path"]), exist_ok=True)
                    print(json.dumps({"code": 200, "message": "local_mkdir"}, ensure_ascii=False)); return 0
                if op == "alist_rename":
                    src = LA.logic_to_local(body["path"]); parent = os.path.dirname(src)
                    dst = os.path.join(parent, body["name"])
                    if os.path.exists(dst) and os.path.abspath(dst) != os.path.abspath(src):
                        print(json.dumps({"code": 500, "message": "local rename target exists"}, ensure_ascii=False)); return 0
                    os.rename(src, dst)
                    print(json.dumps({"code": 200, "message": "local_rename"}, ensure_ascii=False)); return 0
            except Exception as e:
                print(json.dumps({"code": 500, "message": f"local {op}: {e}"}, ensure_ascii=False)); return 0
        ep = {"alist_mkdir": "/api/fs/mkdir", "alist_list": "/api/fs/list", "alist_rename": "/api/fs/rename"}[op]
        out = alist_post(ep, a["body"], a["token"])
        if op != "alist_mkdir": print(out)
        return 0
    if op == "check_login":
        r = py("check-login.py", "--max-age", int(a.get("max_age", 0)), "--quiet", timeout=120)
        sys.stdout.write(r.stdout); sys.stderr.write(r.stderr); return r.returncode
    if op in ("local_stat", "local_verified"):
        import local_audio as LA
        st = LA.local_stat(a["path"])
        print(json.dumps(st, ensure_ascii=False))
        if op == "local_verified":
            return 0 if st.get("verified") else 2
        return 0 if st.get("ok") else 2
    if op == "local_put":  # 已有临时文件 -> 本地库（隔离测试/手工入库）
        import local_audio as LA
        r = LA.place_local(a["src"], a["dest"], a.get("name"), src_sha1=a.get("sha1"))
        print(json.dumps(r, ensure_ascii=False)); return 0
    if op == "audio_dest":
        import local_audio as LA
        print(json.dumps({"audio_dest": LA.audio_dest(), "local_root": LA.LOCAL_ROOT}, ensure_ascii=False)); return 0
    if op == "dl_single":   # == MUSIC_BATCH=.. timeout T download-netease-single(.py|-daily.py) id dest; echo EXIT:$?
        script = a.get("script", "download-netease-single.py")
        env = dict(CHILD_ENV, MUSIC_BATCH=a.get("batch") or "")
        if a.get("dry_run"): env["DL_DRY_RUN"] = "1"
        if a.get("audio_dest"): env["HARMONIA_AUDIO_DEST"] = str(a["audio_dest"])
        elif os.environ.get("HARMONIA_AUDIO_DEST"): env["HARMONIA_AUDIO_DEST"] = os.environ["HARMONIA_AUDIO_DEST"]
        p = subprocess.Popen([PY, os.path.join(HOME, script), str(a["song_id"]), a["dest"]], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
        try:
            out, err = p.communicate(timeout=int(a.get("timeout_s") or 900)); rc = p.returncode
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)  # 连同 ffmpeg 子进程
            out, err = p.communicate(); rc = 124      # 同 coreutils timeout
        sys.stdout.write(out); sys.stderr.write(err); print("EXIT:%d" % rc); return 0
    if op == "kill_dl":
        n = kill_matching("download-netease-single.py %s" % a["song_id"]) if a.get("song_id") is not None else 0
        tmp = os.path.join(HOME, "tmp")
        for _ in range(5):   # Windows 句柄释放是异步的，重试几次
            left = os.listdir(tmp) if os.path.isdir(tmp) else []
            for f in left:
                try: os.remove(os.path.join(tmp, f))
                except OSError: pass
            if not (os.listdir(tmp) if os.path.isdir(tmp) else []): break
            time.sleep(1)
        print("killed %d" % n); return 0
    if op == "retry_drain":   # mv jsonl -> .merging-STAMP && cat
        f = mp(a["path"]); m = "%s.merging-%s" % (f, a["stamp"])
        if os.path.exists(f) and os.path.getsize(f) > 0:
            os.replace(f, m); sys.stdout.write(open(m, encoding="utf-8").read())
        return 0
    if op == "retry_commit":  # cat merging >> merged && rm merging
        f = mp(a["path"]); m = "%s.merging-%s" % (f, a["stamp"])
        if os.path.exists(m):
            with open(m, encoding="utf-8") as src, open(f + ".merged", "a", encoding="utf-8") as dst: dst.write(src.read())
            os.remove(m)
        return 0
    if op == "kw_dl":   # == download-to-115.sh "<kw>" "<dest>" [--dry-run]
        extra = (["--dry-run"] if a.get("dry_run") else []) + [str(x) for x in a.get("args", [])]  # --artist/--title/--duration/--netease-id/--confirm-*
        r = py("keyword_to_115.py", a["keyword"], a["dest"], *extra, timeout=int(a.get("timeout_s") or 900))
        sys.stdout.write(r.stdout); sys.stderr.write(r.stderr); return r.returncode
    if op == "make_qr":
        r = py("make-login-qr.py", *a.get("args", []), timeout=120)
        sys.stdout.write(r.stdout); sys.stderr.write(r.stderr); return r.returncode
    if op == "add_only":   # staging -> dest 原子独占添加：os.rename 在 Windows 目标已存在即失败，不会覆盖
        import hashlib, shutil
        st, de = mp(a["stage"]), mp(a["dest"]); res = {"added": [], "same": [], "conflict": []}
        h = lambda f: hashlib.sha256(open(f, "rb").read()).hexdigest()
        for n in sorted(os.listdir(st)):
            src, dst = os.path.join(st, n), os.path.join(de, n)
            try: os.rename(src, dst); res["added"].append(n); continue
            except FileExistsError: pass
            if h(src) == h(dst): os.remove(src); res["same"].append(n)
            else: res["conflict"].append(n)   # 保留现有文件，不覆盖；暂存件留在 stage 供核查
        if not os.listdir(st): os.rmdir(st)
        print(json.dumps(res, ensure_ascii=False)); return 3 if res["conflict"] else 0
    if op == "mdl":   # route_old 白名单：等价 /opt/music-dl/<script> args
        ok = {"check-avail.py", "check-login.py", "make-login-qr.py", "download-netease-single.py", "download-netease-single-daily.py", "download-netease-id.py"}
        sc = a.get("script")
        if sc not in ok: print("script not allowed", file=sys.stderr); return 2
        r = py(sc, *[str(x) for x in a.get("args", [])], timeout=int(a.get("timeout_s") or 870))
        sys.stdout.write(r.stdout); sys.stderr.write(r.stderr); return r.returncode
    print("unknown op %s" % op, file=sys.stderr); return 2

if __name__ == "__main__":
    sys.exit(main())
