#!/usr/bin/env python3
"""八音盒（Harmonia）例行检查脚本化 —— 2026-11-03 起，Feynman 写，替代 Lomax 的三项 AI 定时检查里的机械部分。

用法（时间均为上海时间）：
  python3 work/ops/harmonia_ops.py ratings    --mode shadow|live   # A 评分同步（00:00 / 12:00）
  python3 work/ops/harmonia_ops.py gapfill    --mode shadow|live   # B 查缺歌（03:00）
  python3 work/ops/harmonia_ops.py dailycheck --mode shadow|live   # C 6 点日推补跑检查（07:10）
  python3 work/ops/harmonia_ops.py compare <shadow 结果文件>        # 影子结果与 Lomax 实际产物对比（调度器自动在 +40~60 分钟跑）
通用参数：--root <music-library 目录>（测试用临时副本）  --handoff <deploy-YYYYMMDD-daily 所在目录>
          --date YYYY-MM-DD（dailycheck 指定日期）  --fake-download（只在测试副本里用：不连 VPS，假装下载成功）
shadow：只读。不写 library.json、不 build、不推 Pages、不下载、不通知；结果写 work/ops/shadow/<项>_<时间>.json。
live  ：真写。写 library.json / build / git push 全部在 work/.library.lock（flock）里做。
退出码：0 正常；75 = dailycheck 发现 6 点那次还在跑（调度器 20 分钟后再查）；其它 = 出错。
"""
import sys as _hs, os as _ho; _hs.path.insert(0, _ho.path.join(_ho.path.dirname(_ho.path.abspath(__file__)), '..'))
import argparse, contextlib, copy, datetime, fcntl, glob, hashlib, importlib.util, json, os, re, shutil
import subprocess, sys, tempfile, time, urllib.request

SH = datetime.timezone(datetime.timedelta(hours=8))
XS_PREFIX = "2000-01-02|x"
FAV_PREFIX = "2000-01-01|f"
GAPFILL_ASK_LIMIT = 15
RUNNING_WINDOW_MIN = 20
PKG_ITEMS = ("index.html", "img", "img.tar.gz", "SHA256", "INSTRUCTIONS.md", "USER_NOTICE.md")
PKG_MECH = ("index.html", "img", "img.tar.gz", "SHA256")      # make_deploy_variants.py 能生成
PKG_AI = ("INSTRUCTIONS.md", "USER_NOTICE.md")                # 要写人话，交 Lomax


# ───────────────────────── 基础 ─────────────────────────
class Ctx:
    def __init__(self, root, handoff=None, mode="shadow", fake_dl=False):
        self.root = os.path.abspath(root)
        self.work = os.path.join(self.root, "work")
        self.ops = os.path.join(self.work, "ops")
        self.shadow = os.path.join(self.ops, "shadow")
        self.deploy = os.path.join(self.root, "deploy")
        self.lib_path = os.path.join(self.root, "library.json")
        self.lock_path = os.path.join(self.work, ".library.lock")
        self.handoff = os.path.abspath(handoff or os.path.dirname(self.root))
        self.mode = mode
        self.fake_dl = fake_dl
        os.makedirs(self.shadow, exist_ok=True)
        os.makedirs(os.path.join(self.ops, "logs"), exist_ok=True)


def now():
    return datetime.datetime.now(SH)


def stamp(t=None):
    return (t or now()).strftime("%Y%m%d_%H%M")


def iso(t=None):
    return (t or now()).isoformat(timespec="seconds")


def log(*a):
    print("[%s]" % now().strftime("%m-%d %H:%M:%S"), *a, flush=True)


def load_json(path, tries=5):
    """读 JSON；别人正在写时可能读到半截，重试几次。"""
    for i in range(tries):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            if i == tries - 1:
                raise
            time.sleep(3)


def dump_json(path, obj):
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def norm(a, t):
    return re.sub(r"\s+", " ", ((a or "") + "|" + (t or "")).lower()).strip()


def ne_id(s):
    m = re.search(r"[?&]id=(\d+)", (s or {}).get("netease") or "")
    if m:
        return m.group(1)
    return str(s.get("netease_id")) if s and s.get("netease_id") else ""


def has_file(s):
    return bool((s or {}).get("file_flac") or (s or {}).get("file_mp3"))


def sp_id(u):
    m = re.search(r"track[/:]([A-Za-z0-9]{22})", u or "")
    return m.group(1) if m else ""


def git(ctx, *args, check=True, timeout=120):
    r = subprocess.run(["git", "-C", ctx.deploy] + list(args), capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError("git %s 失败：%s" % (" ".join(args), r.stderr.strip()[-400:]))
    return r.stdout


def commits_since(ctx, since_ts, until_ts=None):
    """deploy 仓库 since_ts 之后（含）的提交 [(hash, unix, subject)]，新→旧。"""
    out = git(ctx, "log", "--format=%H\t%ct\t%s", "-200", check=False)
    res = []
    for ln in out.splitlines():
        h, ct, s = ln.split("\t", 2)
        ct = int(ct)
        if ct >= since_ts and (until_ts is None or ct <= until_ts):
            res.append((h, ct, s))
    return res


def lib_at_commit(ctx, h):
    """从 deploy 某个提交的 index.html 里取出当时的 library（发布页里内嵌了完整 library）。"""
    html = git(ctx, "show", "%s:index.html" % h, timeout=120)
    m = re.search(r'\{"library":', html)
    return json.JSONDecoder().raw_decode(html[m.start():])[0]["library"] if m else None


@contextlib.contextmanager
def library_lock(ctx, timeout=1800, who="harmonia_ops"):
    """work/.library.lock 写锁（flock，和 with_library_lock.sh 是同一把）。"""
    f = open(ctx.lock_path, "a+")
    t0 = time.time()
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.time() - t0 > timeout:
                f.close()
                raise TimeoutError("等 work/.library.lock 超过 %d 秒" % timeout)
            time.sleep(5)
    try:
        f.seek(0); f.truncate(); f.write("%s pid=%d since=%s\n" % (who, os.getpid(), iso())); f.flush()
        log("拿到写锁（等了 %.0f 秒）" % (time.time() - t0))
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()
        log("释放写锁")


def poll_module(ctx):
    spec = importlib.util.spec_from_file_location("poll_ratings_ops", os.path.join(ctx.work, "poll_ratings.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def fetch_server(ctx):
    """Worker /ratings（只读 GET）。测试时可用环境变量 HARMONIA_SRV_FILE 指定一份 JSON 代替。"""
    f = os.environ.get("HARMONIA_SRV_FILE")
    if f:
        return load_json(f)
    return poll_module(ctx).fetch_all()


# ───────────────────────── 叫醒 ─────────────────────────
def wake(ctx, item, event_id, reason, files=(), notify=("Lomax",), detail=None, t=None):
    """需要 AI 时：写 WAKE 文件 + 有 webhook 就 POST。同一 event_id 只叫一次。shadow 只记录到 shadow/。"""
    t = t or now()
    live = ctx.mode == "live"
    base = ctx.ops if live else ctx.shadow
    sent_p = os.path.join(base, "wake_sent.json")
    sent = load_json(sent_p) if os.path.exists(sent_p) else {}
    rec = {"item": item, "event_id": event_id, "reason": reason, "notify": list(notify), "files": list(files),
           "detail": detail, "created_at": iso(t), "mode": ctx.mode}
    if event_id in sent:
        rec["dedup"] = "已叫过（%s），这次不再叫" % sent[event_id]["at"]
        return rec
    path = os.path.join(base, "WAKE_%s_%s.json" % (item, stamp(t)))
    n = 2
    while os.path.exists(path):
        path = os.path.join(base, "WAKE_%s_%s_%d.json" % (item, stamp(t), n)); n += 1
    rec["wake_file"] = path
    url_f = os.path.join(ctx.ops, "wake_webhook.url")
    url = open(url_f).read().strip() if os.path.exists(url_f) else ""
    if live and url:
        try:
            req = urllib.request.Request(url, data=json.dumps(rec, ensure_ascii=False).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                rec["webhook"] = "POST %d" % r.status
        except Exception as e:
            rec["webhook"] = "POST 失败：%s" % type(e).__name__
    elif url:
        rec["webhook"] = "shadow：有 webhook 地址，只记录不调用"
    else:
        rec["webhook"] = "没有 work/ops/wake_webhook.url，只写 WAKE 文件"
    dump_json(path, rec)
    sent[event_id] = {"at": iso(t), "file": path}
    dump_json(sent_p, sent)
    return rec


def save_result(ctx, item, t, data):
    p = os.path.join(ctx.shadow if ctx.mode == "shadow" else os.path.join(ctx.ops, "logs"), "%s_%s.json" % (item, t.strftime("%Y%m%d_%H%M%S")))
    data.setdefault("result_file", p)
    dump_json(p, data)
    log("结果 →", p)
    return p


def keep_pre_state(ctx, t, item):
    """把运行前的 library.json / ratings_snapshot.json 存一份，供对比。"""
    d = os.path.join(ctx.shadow, "state", "%s_%s" % (item, stamp(t)))
    os.makedirs(d, exist_ok=True)
    for src in (ctx.lib_path, os.path.join(ctx.work, "ratings_snapshot.json")):
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(d, os.path.basename(src)))
    return d


def prune_state(ctx, days=14):
    cut = time.time() - days * 86400
    for d in glob.glob(os.path.join(ctx.shadow, "state", "*")):
        if os.path.getmtime(d) < cut:
            shutil.rmtree(d, ignore_errors=True)


# ───────────────────────── 投影（用来比较两份 library） ─────────────────────────
def projection(lib):
    out = {}
    for b in lib.get("daily_batches", []):
        for s in b.get("songs", []):
            out["日推评分|%s|%s" % (b["date"], s.get("id"))] = s.get("rating")
    for r in lib.get("rated", []):
        out["歌手页|%s" % norm(r.get("artist"), r.get("title"))] = [r.get("rating"), bool(r.get("fav")), has_file(r)]
    for u in lib.get("unfavorited", []) or []:
        out["取消收藏存档|%s" % norm(u.get("artist"), u.get("title"))] = 1
    for p in lib.get("playlists", []):
        for s in p.get("songs", []):
            out["歌单文件|%s|%s" % (p.get("id"), norm(s.get("artist"), s.get("title")))] = has_file(s)
    return out


def proj_diff(a, b):
    keys = set(a) | set(b)
    return {k: [a.get(k), b.get(k)] for k in sorted(keys) if a.get(k) != b.get(k)}


# ───────────────────────── 沙盒（只为 shadow 预测） ─────────────────────────
SRV_HOOK = '''

# ---- harmonia_ops 沙盒注入：fetch_all 读固定文件，保证预测与差异用的是同一份服务端数据 ----
import os as _hops_os
if _hops_os.environ.get("HARMONIA_SRV_FILE"):
    def fetch_all():
        return json.load(open(_hops_os.environ["HARMONIA_SRV_FILE"], encoding="utf-8"))
'''


def make_sandbox(ctx, lib_src):
    """临时目录：library.json 副本 + work 顶层小文件副本（不是软链，防止写穿到真目录）。"""
    sb = tempfile.mkdtemp(prefix="hops_sb_")
    os.makedirs(os.path.join(sb, "work"))
    shutil.copy2(lib_src, os.path.join(sb, "library.json"))
    os.symlink(os.path.join(ctx.root, "worker"), os.path.join(sb, "worker"))  # 只读 kv_namespace_id.txt
    for f in os.listdir(ctx.work):
        p = os.path.join(ctx.work, f)
        if os.path.isfile(p) and (f.endswith((".py", ".json", ".txt")) and os.path.getsize(p) < 5_000_000):
            shutil.copy2(p, os.path.join(sb, "work", f))
    rewrite_hardcoded_paths(os.path.join(sb, "work"), sb)
    with open(os.path.join(sb, "work", "poll_ratings.py"), "a", encoding="utf-8") as f:
        f.write(SRV_HOOK)
    return sb


HARDCODED_ROOTS = ("/workspace/harmonia-handoff/music-library", "/workspace/music-library")


def rewrite_hardcoded_paths(work_dir, new_root):
    """副本里有些老脚本写死了真目录（例如 album_links.py 的 /workspace/music-library/library.json，
    /workspace/music-library 是真目录的软链）。复制出来的副本必须改成指向副本，否则会写穿到真 library.json。"""
    for f in glob.glob(os.path.join(work_dir, "*.py")):
        s = open(f, encoding="utf-8", errors="surrogateescape").read()
        t = s
        for r in HARDCODED_ROOTS:
            t = t.replace(r, new_root)
        if t != s:
            open(f, "w", encoding="utf-8", errors="surrogateescape").write(t)


# ───────────────────────── A 评分同步 ─────────────────────────
def srv_diff(old, new):
    added, changed, removed = [], [], []
    for k, v in new.items():
        o = old.get(k)
        if o is None:
            added.append(k)
        elif o.get("rating") != v.get("rating"):
            changed.append(k)
    for k in old:
        if k not in new:
            removed.append(k)
    return added, changed, removed


def xs_done_index(lib):
    pages = lib.get("xunsheng", [])
    keys = {p.get("request_key") for p in pages}
    seeds = {norm((p.get("seed") or {}).get("artist"), (p.get("seed") or {}).get("title")) for p in pages}
    sps = {sp_id((p.get("seed") or {}).get("spotify")) for p in pages} - {""}
    def done(k, v):
        sid = k[len(XS_PREFIX):]
        return k in keys or norm(v.get("artist"), v.get("title")) in seeds or (not sid.startswith("h") and sid in sps)
    return done


def xs_pending(lib, srv, before_ts=None):
    """寻声：rating=1、还没做页的请求（before_ts：只算这个时间之前登记的）。"""
    done = xs_done_index(lib)
    out = []
    for k, v in sorted(srv.items(), key=lambda x: x[1].get("ts", 0)):
        if not k.startswith(XS_PREFIX) or v.get("rating") != 1:
            continue
        if before_ts and v.get("ts", 0) >= before_ts:
            continue
        if not done(k, v):
            out.append({"key": k, "title": v.get("title"), "artist": v.get("artist"), "ts": v.get("ts"),
                        "requested_at": datetime.datetime.fromtimestamp(v.get("ts", 0) / 1000, SH).strftime("%Y-%m-%d %H:%M")})
    return out


def predict_ratings(ctx, lib_file, srv):
    """在沙盒里跑一遍 apply_ratings.py（不带 --publish），看它会把 library.json 改成什么样。"""
    sb = make_sandbox(ctx, lib_file)
    try:
        sf = os.path.join(sb, "srv.json")
        json.dump(srv, open(sf, "w", encoding="utf-8"), ensure_ascii=False)
        env = dict(os.environ, HARMONIA_SRV_FILE=sf)
        r = subprocess.run([sys.executable, os.path.join(sb, "work", "apply_ratings.py")], cwd=sb, env=env,
                           capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            raise RuntimeError("沙盒 apply_ratings 失败：%s" % r.stderr[-800:])
        out = r.stdout
        changed = "library.json 已更新" in out
        lines = [l for l in out.splitlines() if l.startswith("  [") or l.startswith("      ")]
        new_lib = load_json(os.path.join(sb, "library.json"))
        n_commit = len([l for l in lines if l.startswith("  [")])
        return {"changed": changed, "lines": lines, "commit_msg": "ratings: sync from Cloudflare KV (%d changes)" % n_commit,
                "lib": new_lib, "stdout_tail": out[-3000:]}
    finally:
        shutil.rmtree(sb, ignore_errors=True)


def job_ratings(ctx, args):
    t0 = now()
    res = {"item": "ratings", "mode": ctx.mode, "started_at": iso(t0), "root": ctx.root}
    snap_p = os.path.join(ctx.work, "ratings_snapshot.json")
    pre_dir = keep_pre_state(ctx, t0, "ratings") if ctx.mode == "shadow" else None
    lib_file = os.path.join(pre_dir, "library.json") if pre_dir else ctx.lib_path
    old_snap = load_json(snap_p) if os.path.exists(snap_p) else {}
    res["pre"] = {"library_mtime": iso(datetime.datetime.fromtimestamp(os.path.getmtime(ctx.lib_path), SH)),
                  "snapshot_mtime": iso(datetime.datetime.fromtimestamp(os.path.getmtime(snap_p), SH)) if os.path.exists(snap_p) else None,
                  "deploy_head": git(ctx, "rev-parse", "HEAD").strip(), "pre_state_dir": pre_dir}
    if os.path.getmtime(ctx.lib_path) > t0.timestamp() - 60:
        res["pre"]["warning"] = "library.json 在开跑前 1 分钟内被改过，可能 Lomax 已经先跑了"
    srv = fetch_server(ctx)
    a, c, r = srv_diff(old_snap, srv)
    lib = load_json(lib_file)
    xs_new = [k for k in a + c if k.startswith(XS_PREFIX) and srv[k].get("rating") == 1]
    pend = xs_pending(lib, srv)
    xs_new_pending = [p for p in pend if p["key"] in xs_new]
    res["server"] = {"total": len(srv), "snapshot_total": len(old_snap),
                     "added": a, "changed": [[k, old_snap[k].get("rating"), srv[k].get("rating")] for k in c], "removed": r,
                     "xunsheng_new_requests": xs_new, "xunsheng_pending_all": pend}
    pred = predict_ratings(ctx, lib_file, srv)
    pdiff = proj_diff(projection(lib), projection(pred["lib"]))
    res["plan"] = {"library_changes": pred["changed"], "apply_lines": pred["lines"], "projection_diff": pdiff,
                   "would_build": pred["changed"], "would_push_pages": pred["changed"], "commit_msg": pred["commit_msg"] if pred["changed"] else None,
                   "main_site_package": False}
    # 要不要叫醒 Lomax
    wakes = []
    chg_file = None
    if pred["changed"] or xs_new_pending:
        base = ctx.work if ctx.mode == "live" else ctx.shadow
        chg_file = os.path.join(base, "ratings_changes_%s.json" % stamp(t0))
        dump_json(chg_file, {"at": iso(t0), "server_diff": res["server"], "apply_lines": pred["lines"],
                             "projection_diff": pdiff, "xunsheng_new_pending": xs_new_pending,
                             "note": "给 Lomax：有变化就给宣驰写一句人话总结；有新寻声请求就排进次日 6 点"})
    if ctx.mode == "live" and pred["changed"]:
        pub = publish_ratings_live(ctx, res)
        res["live"] = pub
        if chg_file:
            d = load_json(chg_file); d["published"] = pub; dump_json(chg_file, d)
    if pred["changed"]:
        ev = "ratings:" + hashlib.sha1(json.dumps(pdiff, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]
        wakes.append(wake(ctx, "ratings", ev, "评分/收藏有变化（%d 处），请给宣驰写一句人话总结" % len(pdiff), files=[x for x in [chg_file] if x], t=t0,
                          detail={"apply_lines": pred["lines"][:30]}))
    for p in xs_new_pending:
        wakes.append(wake(ctx, "xunsheng", "xunsheng:%s:%s" % (p["key"], p["ts"]),
                          "新的寻声请求《%s》- %s，请排进次日 6 点" % (p["title"], p["artist"]), files=[x for x in [chg_file] if x], t=t0, detail=p))
    res["wake"] = wakes
    res["wake_lomax"] = bool(wakes)
    res["wake_reason"] = "；".join(w["reason"] for w in wakes) or "无变化、无新寻声请求，不叫醒"
    res["finished_at"] = iso()
    return res


def sync_deploy_img(ctx):
    """把本版网页引用、deploy/img 里还没有的图片补进去（只增不删）。返回新增文件名。"""
    used_f = os.path.join(ctx.work, "img_used.txt")
    if not os.path.exists(used_f):
        return []
    add = []
    for u in open(used_f).read().split():
        if not re.match(r"^img/[0-9a-f]{12}\.(jpg|png|webp|gif)$", u):
            continue
        dst = os.path.join(ctx.deploy, u)
        if not os.path.exists(dst) and os.path.exists(os.path.join(ctx.root, u)):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(os.path.join(ctx.root, u), dst)
            add.append(u)
    return add


def push_pages(ctx, msg, include_img=True):
    """deploy/：拷 index.html（Pages 版 = 根目录 build 结果）+ 新图片 → commit → push。须在锁内调用。"""
    for f in ("index.html", "apple-touch-icon.png", "manifest.webmanifest"):
        p = os.path.join(ctx.root, f)
        if os.path.exists(p):
            shutil.copy2(p, os.path.join(ctx.deploy, f))
    imgs = sync_deploy_img(ctx) if include_img else []
    git(ctx, "add", "index.html", *imgs)
    if subprocess.call(["git", "-C", ctx.deploy, "diff", "--staged", "--quiet"]) == 0:
        return {"pushed": False, "reason": "deploy 无变化", "img_added": imgs}
    git(ctx, "commit", "-m", msg)
    git(ctx, "push", "origin", "main", timeout=300)
    return {"pushed": True, "commit": git(ctx, "rev-parse", "HEAD").strip(), "img_added": imgs}


def publish_ratings_live(ctx, res):
    """live：锁内跑原版 apply_ratings.py --publish（写 library.json → build → deploy → commit → push），
    再补推它没带上的新图片（apply_ratings 只 git add index.html）。"""
    with library_lock(ctx, who="ratings"):
        head0 = git(ctx, "rev-parse", "HEAD").strip()
        r = subprocess.run([sys.executable, os.path.join(ctx.work, "apply_ratings.py"), "--publish"], cwd=ctx.root,
                           capture_output=True, text=True, timeout=3600)
        out = {"rc": r.returncode, "stdout_tail": r.stdout[-2500:], "stderr_tail": r.stderr[-1500:]}
        if r.returncode != 0:
            raise RuntimeError("apply_ratings --publish 失败 rc=%d：%s" % (r.returncode, r.stderr[-600:]))
        imgs = sync_deploy_img(ctx)
        if imgs:
            git(ctx, "add", *imgs)
            git(ctx, "commit", "-m", "ratings: 补 %d 张新图片" % len(imgs))
            git(ctx, "push", "origin", "main", timeout=300)
        out["img_added"] = imgs
        out["head_before"] = head0
        out["head_after"] = git(ctx, "rev-parse", "HEAD").strip()
    return out


# ───────────────────────── B 查缺歌 ─────────────────────────
def find_missing(lib):
    """playlists 里两种文件都没有的歌、rated 里没文件但有网易云链接的歌。没网易云链接的单列「跳过」。"""
    todo, skipped, seen = [], [], set()
    artist_dirs = {}
    for r in lib.get("rated", []):
        f = r.get("file_flac") or r.get("file_mp3") or ""
        if f.startswith("/115/音乐库/歌手/"):
            artist_dirs.setdefault(norm(r.get("artist"), ""), os.path.dirname(f))
    def add(where, s, dest):
        nid = ne_id(s)
        ent = {"where": where, "artist": s.get("artist"), "title": s.get("title"), "netease_id": nid, "dest": dest}
        if not nid:
            skipped.append(ent); return
        if nid in seen:
            return
        seen.add(nid); todo.append(ent)
    for p in lib.get("playlists", []):
        for s in p.get("songs", []):
            if not has_file(s):
                add("歌单 " + (p.get("name") or p.get("id") or ""), s, "/115/音乐库/歌单/%s" % (p.get("id") or "misc"))
    for s in lib.get("rated", []):
        if not has_file(s):
            d = artist_dirs.get(norm(s.get("artist"), "")) or "/115/音乐库/歌手/%s" % re.sub(r'[\\/:*?"<>|]', "_", (s.get("artist") or "未知").strip())
            add("歌手页", s, d)
    return todo, skipped


def download_songs(ctx, songs, tag):
    """走技术顾问的下载通道（work/run_all_queues.run_batch：VPS go-music-dl → 115/Alist，FLAC 优先，登录守卫）。
    子进程里跑（run_batch 遇登录失效会 sys.exit(42)）。返回 {netease_id: row}。"""
    results = {}
    by_dest = {}
    for s in songs:
        by_dest.setdefault(s["dest"], []).append(s)
    for dest, lst in by_dest.items():
        pf = os.path.join(ctx.work, "OPS_%s_%s_PROGRESS.json" % (tag, hashlib.sha1(dest.encode()).hexdigest()[:6]))
        if ctx.fake_dl:
            rows = [{"title": s["title"], "artist": s["artist"], "song_id": int(s["netease_id"]), "ok": "ok", "format": "flac",
                     "alist_path": "%s/%s - %s.flac" % (dest, s["artist"], s["title"])} for s in lst]
            dump_json(pf, {"batch": tag, "dest": dest, "results": rows, "fake": True}); rc = 0
        else:
            spec = [{"song_id": int(s["netease_id"]), "artist": s["artist"], "title": s["title"]} for s in lst]
            code = ("import sys,json;sys.path.insert(0,%r);import run_all_queues as r;from pathlib import Path;"
                    "r.REMOTE_TIMEOUT=600;pf=Path(%r);r.PROGRESS_FILES[%r]=pf;r.run_batch(%r,json.loads(%r),%r,pf)"
                    % (ctx.work, pf, tag, tag, json.dumps(spec, ensure_ascii=False), dest))
            p = subprocess.run([sys.executable, "-c", code], cwd=ctx.work, capture_output=True, text=True, timeout=3 * 3600)
            rc = p.returncode
            open(os.path.join(ctx.ops, "logs", "%s_dl.log" % tag), "a").write(p.stdout[-20000:] + p.stderr[-5000:])
        rows = (load_json(pf).get("results") or []) if os.path.exists(pf) else []
        for row in rows:
            results[str(row.get("song_id"))] = dict(row, rc=rc)
        for s in lst:
            results.setdefault(s["netease_id"], {"ok": "fail", "reason": "rc=%d 无进度记录" % rc, "rc": rc})
    return results


def write_back_paths(lib, rows):
    """按网易云 id 把下载好的路径写回 library 里所有同 id 且缺文件的条目。返回写回条数。"""
    n = 0
    def fill(s):
        nonlocal n
        row = rows.get(ne_id(s))
        if row and row.get("ok") in ("ok", "skip") and row.get("alist_path") and not has_file(s):
            s["file_flac" if (row.get("format") or "").lower() == "flac" else "file_mp3"] = row["alist_path"]
            n += 1
    for p in lib.get("playlists", []):
        for s in p.get("songs", []):
            fill(s)
    for s in lib.get("rated", []):
        fill(s)
    return n


def build_and_push(ctx, msg):
    """锁内调用：build.py → deploy（Pages）→ push。"""
    b = subprocess.run([sys.executable, os.path.join(ctx.root, "build.py")], cwd=ctx.root, capture_output=True, text=True, timeout=1800)
    if b.returncode != 0:
        raise RuntimeError("build.py 失败：%s" % b.stderr[-600:])
    warn = "比线上少了" in b.stderr
    if warn:
        return {"pushed": False, "reason": "build 播放对比警告：比线上少了能播放的歌，不发站", "build_stderr": b.stderr[-1500:]}
    r = push_pages(ctx, msg)
    r["build_tail"] = b.stdout[-800:]
    return r


def other_download_running():
    r = subprocess.run(["pgrep", "-af", r"run_all_queues|run_daily_dl_|run_xunsheng_dl_|retry_after_login|playlist_citypop_and_daily|backfill_batch"],
                       capture_output=True, text=True)
    me = str(os.getpid())
    return [l for l in r.stdout.splitlines() if l.split()[0] != me and "pgrep" not in l]


def job_gapfill(ctx, args):
    t0 = now()
    res = {"item": "gapfill", "mode": ctx.mode, "started_at": iso(t0), "root": ctx.root}
    if ctx.mode == "shadow":
        res["pre"] = {"pre_state_dir": keep_pre_state(ctx, t0, "gapfill"), "deploy_head": git(ctx, "rev-parse", "HEAD").strip()}
    lib = load_json(ctx.lib_path)
    todo, skipped = find_missing(lib)
    res["missing"] = todo
    res["skipped_no_netease"] = skipped
    wakes = []
    if not todo:
        res["plan"] = {"download": 0, "would_write_library": False, "would_build": False, "would_push_pages": False}
    elif len(todo) > GAPFILL_ASK_LIMIT:
        res["plan"] = {"download": 0, "reason": "缺 %d 首，超过 %d 首，先问宣驰" % (len(todo), GAPFILL_ASK_LIMIT)}
        wakes.append(wake(ctx, "gapfill", "gapfill:ask:%s" % hashlib.sha1(json.dumps([x["netease_id"] for x in todo]).encode()).hexdigest()[:12],
                          "缺歌 %d 首超过 %d 首，请先问宣驰要不要一次补完" % (len(todo), GAPFILL_ASK_LIMIT), t=t0, detail={"songs": todo}))
    else:
        res["plan"] = {"download": len(todo), "dests": sorted({x["dest"] for x in todo}), "would_write_library": True,
                       "would_build": True, "would_push_pages": True, "commit_msg": "gapfill %s: 补 %d 首网盘文件" % (t0.strftime("%Y-%m-%d"), len(todo))}
        if ctx.mode == "live":
            busy = other_download_running()
            if busy:
                res["live"] = {"skipped": "别的下载队列在跑，这次不下：%s" % busy[0][:120]}
            else:
                rows = download_songs(ctx, todo, "gapfill_%s" % stamp(t0))
                fails = [dict(x, row=rows.get(x["netease_id"])) for x in todo if (rows.get(x["netease_id"]) or {}).get("ok") not in ("ok", "skip")]
                with library_lock(ctx, who="gapfill"):
                    lib = load_json(ctx.lib_path)
                    n = write_back_paths(lib, rows)
                    pub = None
                    if n:
                        dump_json(ctx.lib_path, lib)
                        pub = build_and_push(ctx, "gapfill %s: 补 %d 首网盘文件" % (t0.strftime("%Y-%m-%d"), n))
                res["live"] = {"downloaded": {k: v for k, v in rows.items()}, "written_back": n, "published": pub, "failed": fails}
                if fails:
                    wakes.append(wake(ctx, "gapfill", "gapfill:fail:%s:%s" % (t0.strftime("%Y%m%d"), ",".join(sorted(f["netease_id"] for f in fails))),
                                      "补缺歌有 %d 首下载失败，请跟宣驰说明" % len(fails), t=t0, detail={"failed": fails}))
    if skipped:
        res["note"] = "没网易云链接、跳过：" + "、".join("%s《%s》" % (x["artist"], x["title"]) for x in skipped)
    if ctx.mode == "shadow":
        res["login_status"] = vps_login_status_readonly()
    res["wake"] = wakes
    res["wake_lomax"] = bool([w for w in wakes if "dedup" not in w])
    res["wake_reason"] = "；".join(w["reason"] for w in wakes) or ("没有要补的歌，不出声" if not todo else "≤%d 首，脚本自己补，不叫醒（失败才叫）" % GAPFILL_ASK_LIMIT)
    res["finished_at"] = iso()
    return res


def vps_login_status_readonly():
    """只读：cat VPS 上的网易云登录状态文件（不触发检查、不改任何东西）。"""
    try:
        pw = "<REDACTED_VPS_SSHPASS_PATH>" if os.path.exists("<REDACTED_VPS_SSHPASS_PATH>") else "<REDACTED_VPS_SECRET_PATH>"
        env = dict(os.environ, SSHPASS=open(pw).read().strip())
        import harmonia_target as T  # 迁 hora 候选
        if T.is_hora(): r = T.hctl("cat", timeout=60, path="/opt/music-dl/state/login_status.json")
        else: r = subprocess.run(["sshpass", "-e", "ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=15", "root@<REDACTED_HOST>",
                            "cat /opt/music-dl/state/login_status.json"], capture_output=True, text=True, timeout=40, env=env)
        d = json.loads(r.stdout)
        return {k: d.get(k) for k in ("checked_at", "ok", "logged_in", "vip", "flac_ok", "detail")}
    except Exception as e:
        return {"error": type(e).__name__}


# ───────────────────────── C 6 点日推补跑检查 ─────────────────────────
def daily_progress_files(ctx, D):
    ymd = D.replace("-", ""); d_ = D.replace("-", "_")
    pats = ["DAILY_%s_*" % D, "DAILY_%s_*" % d_, "DAILY_ARTIST_%s_*" % ymd, "XUNSHENG_%s_*" % ymd, "run_daily_dl_%s*" % ymd]
    out = set()
    for p in pats:
        out.update(glob.glob(os.path.join(ctx.work, p)))
    return sorted(out)


def progress_paths(ctx, D):
    """当天所有进度文件里下载成功的行：{netease_id: row}，以及 artist|title → row。"""
    by_id, by_name = {}, {}
    for f in daily_progress_files(ctx, D):
        if not f.endswith(".json"):
            continue
        try:
            d = load_json(f)
        except Exception:
            continue
        rows = d.get("results") if isinstance(d, dict) else None
        if not rows and isinstance(d, dict) and d.get("alist_path"):
            rows = [d]
        for r in rows or []:
            if r.get("ok") in ("ok", "skip") and r.get("alist_path"):
                if r.get("song_id"):
                    by_id[str(r["song_id"])] = r
                by_name[norm(r.get("artist"), r.get("title"))] = r
                by_name[norm(r.get("netease_artist"), r.get("netease_title"))] = r
    return by_id, by_name


def evaluate_daily(D, lib, commits, pkg_dir, now_t, recent_files, xs_pend, remote_ok=None, pkg_check_mtime=True):
    """纯判断：返回 {checks, complete, running, mechanical:[...], need_ai:[...]}。"""
    ymd = D.replace("-", "")
    checks, mech, ai = {}, [], []
    running = [f for f, mt in recent_files if now_t.timestamp() - mt < RUNNING_WINDOW_MIN * 60]
    b = next((x for x in lib.get("daily_batches", []) if x.get("date") == D), None)
    if not b:
        checks["daily_batch"] = "缺：library 里没有 %s 的日推" % D
        ai.append("日推 6 首还没选（缺选歌/主题文案）")
    else:
        nofile = [s for s in b.get("songs", []) if not has_file(s)]
        checks["daily_batch"] = "%d 首，缺文件 %d" % (len(b.get("songs", [])), len(nofile))
        if len(b.get("songs", [])) < 6:
            ai.append("日推只有 %d 首（应 6 首）" % len(b.get("songs", [])))
        for s in nofile:
            mech.append({"action": "补文件", "section": "日推", "artist": s.get("artist"), "title": s.get("title"), "netease_id": ne_id(s)})
    a = next((x for x in lib.get("daily_artists", []) if x.get("date") == D), None)
    if not a:
        checks["daily_artist"] = "缺：library 里没有 %s 的每日歌手" % D
        ai.append("每日歌手还没选（缺选人/文案）")
    else:
        nofile = [s for s in a.get("songs", []) if not has_file(s)]
        checks["daily_artist"] = "%s：%d 首，缺文件 %d" % (a.get("name"), len(a.get("songs", [])), len(nofile))
        if len(a.get("songs", [])) < 5:
            ai.append("每日歌手只有 %d 首（应 5 首）" % len(a.get("songs", [])))
        for s in nofile:
            mech.append({"action": "补文件", "section": "每日歌手", "artist": s.get("artist"), "title": s.get("title"), "netease_id": ne_id(s),
                         "dest": (s.get("dest") or "").rstrip("/")})
    checks["xunsheng_pending"] = [p["title"] for p in xs_pend] if xs_pend is not None else "未查"
    if xs_pend:
        ai.append("寻声页还没做：" + "、".join("《%s》" % p["title"] for p in xs_pend))
    pk = {}
    for it in PKG_ITEMS:
        p = os.path.join(pkg_dir, it)
        pk[it] = os.path.exists(p) and (not pkg_check_mtime or os.path.getmtime(p) <= now_t.timestamp())
    checks["package"] = {"dir": pkg_dir, "items": pk}
    miss_mech = [i for i in PKG_MECH if not pk[i]]
    miss_ai = [i for i in PKG_AI if not pk[i]]
    data_ok = not ai and not mech
    if miss_mech:
        mech.append({"action": "生成主站包", "missing": miss_mech, "cmd": "work/make_deploy_variants.py %s" % pkg_dir, "after_data_complete": True})
    if miss_ai:
        ai.append("主站包缺 %s（要写说明/通知）" % "、".join(miss_ai))
    dc = [c for c in commits if re.match(r"^daily %s\b" % re.escape(D), c[2])]
    checks["pages_commit"] = [c[0][:7] + " " + datetime.datetime.fromtimestamp(c[1], SH).strftime("%H:%M") for c in dc]
    checks["pages_remote_in_sync"] = remote_ok
    if not dc or remote_ok is False:
        mech.append({"action": "build+推 Pages", "commit_msg": "daily %s: (ops 补推)" % D, "after_data_complete": True})
    complete = not ai and not mech
    return {"date": D, "checks": checks, "running": running, "complete": complete, "data_complete": data_ok,
            "mechanical": mech, "need_ai": ai}


def remote_in_sync(ctx):
    try:
        r = subprocess.run(["git", "-C", ctx.deploy, "ls-remote", "origin", "refs/heads/main"], capture_output=True, text=True, timeout=60)
        remote = r.stdout.split()[0] if r.stdout.strip() else ""
        return remote == git(ctx, "rev-parse", "HEAD").strip(), remote
    except Exception as e:
        return None, type(e).__name__


def job_dailycheck(ctx, args):
    t0 = now()
    D = args.date or t0.strftime("%Y-%m-%d")
    res = {"item": "dailycheck", "mode": ctx.mode, "started_at": iso(t0), "date": D, "root": ctx.root}
    if ctx.mode == "shadow":
        res["pre"] = {"pre_state_dir": keep_pre_state(ctx, t0, "dailycheck"), "deploy_head": git(ctx, "rev-parse", "HEAD").strip()}
    lib = load_json(ctx.lib_path)
    files = daily_progress_files(ctx, D) + [ctx.lib_path, os.path.join(ctx.work, "RETRY_QUEUE.json")]
    recent = [(f, os.path.getmtime(f)) for f in files if os.path.exists(f)]
    try:
        srv = fetch_server(ctx)
        six = datetime.datetime.strptime(D + " 06:00", "%Y-%m-%d %H:%M").replace(tzinfo=SH).timestamp() * 1000
        xp = xs_pending(lib, srv, before_ts=six)
    except Exception as e:
        xp = None; res["xunsheng_error"] = type(e).__name__
    day0 = datetime.datetime.strptime(D, "%Y-%m-%d").replace(tzinfo=SH).timestamp()
    commits = commits_since(ctx, day0)
    ok, remote = remote_in_sync(ctx)
    pkg = os.path.join(ctx.handoff, "deploy-%s-daily" % D.replace("-", ""))
    ev = evaluate_daily(D, lib, commits, pkg, t0, recent, xp, ok)
    res.update(ev)
    res["remote_head"] = remote
    wakes = []
    if ev["running"]:
        res["decision"] = "6 点那次还在跑（%d 分钟内有改动：%s），不插手，20 分钟后再查" % (RUNNING_WINDOW_MIN, ", ".join(os.path.basename(f) for f in ev["running"]))
        res["wake"] = []; res["wake_lomax"] = False; res["finished_at"] = iso()
        res["exit_code"] = 75
        return res
    if ev["complete"]:
        res["decision"] = "当天齐全，不出声"
    else:
        res["decision"] = "不齐：机械可补 %d 项，需要 AI %d 项" % (len(ev["mechanical"]), len(ev["need_ai"]))
        if ctx.mode == "live":
            res["live"] = daily_resume_live(ctx, D, ev, pkg)
        if ev["need_ai"]:
            wakes.append(wake(ctx, "dailycheck", "dailycheck:%s:%s" % (D, hashlib.sha1("|".join(ev["need_ai"]).encode()).hexdigest()[:10]),
                              "%s 日推缺需要 AI 的部分：%s" % (D, "；".join(ev["need_ai"])), t=t0,
                              files=[f for f in daily_progress_files(ctx, D) if f.endswith((".json", ".md"))], detail={"checks": ev["checks"]}))
        if ctx.mode == "live" and (res.get("live") or {}).get("failed"):
            wakes.append(wake(ctx, "dailycheck", "dailycheck:%s:fail" % D, "%s 补跑机械步骤失败：%s" % (D, res["live"]["failed"]), t=t0))
    res["wake"] = wakes
    res["wake_lomax"] = bool(wakes)
    res["wake_reason"] = "；".join(w["reason"] for w in wakes) or ("齐全" if ev["complete"] else "只缺机械步骤，脚本自己补，不叫醒")
    res["finished_at"] = iso()
    return res


def daily_resume_live(ctx, D, ev, pkg):
    """live 断点续跑（只做机械部分）：①按当天进度文件回填路径，回填不上且有网易云 id 的补下载；②数据齐了才 build+推 Pages；
    ③主站包缺 index/img/img.tar.gz/SHA256 时用 make_deploy_variants.py 重新生成（不碰 INSTRUCTIONS/USER_NOTICE）。"""
    out = {"failed": []}
    by_id, by_name = progress_paths(ctx, D)
    need_dl = []
    lib = load_json(ctx.lib_path)
    def section_songs(lib):
        b = next((x for x in lib.get("daily_batches", []) if x.get("date") == D), None)
        a = next((x for x in lib.get("daily_artists", []) if x.get("date") == D), None)
        return (b or {}).get("songs", []) + (a or {}).get("songs", [])
    for s in section_songs(lib):
        if has_file(s):
            continue
        row = by_id.get(ne_id(s)) or by_name.get(norm(s.get("artist"), s.get("title")))
        if not row and ne_id(s):
            dest = (s.get("dest") or "").rstrip("/") or "/115/音乐库/日推/%s" % D
            need_dl.append({"artist": s.get("artist"), "title": s.get("title"), "netease_id": ne_id(s), "dest": dest})
    rows = {}
    if need_dl:
        if other_download_running():
            out["failed"].append("别的下载队列在跑，补下载推迟"); return out
        rows = download_songs(ctx, need_dl, "daily_%s" % D.replace("-", ""))
    with library_lock(ctx, who="dailycheck"):
        lib = load_json(ctx.lib_path)
        n = 0
        for s in section_songs(lib):
            if has_file(s):
                continue
            row = by_id.get(ne_id(s)) or by_name.get(norm(s.get("artist"), s.get("title"))) or rows.get(ne_id(s))
            if row and row.get("ok") in ("ok", "skip") and row.get("alist_path"):
                s["file_flac" if (row.get("format") or "").lower() == "flac" else "file_mp3"] = row["alist_path"]; n += 1
            else:
                out["failed"].append("%s《%s》没拿到文件" % (s.get("artist"), s.get("title")))
        out["written_back"] = n
        if n:
            dump_json(ctx.lib_path, lib)
        data_ok = all(has_file(s) for s in section_songs(lib)) and not ev["need_ai"]
        need_push = n > 0 or any(m["action"] == "build+推 Pages" for m in ev["mechanical"])
        if data_ok and need_push:
            out["pages"] = build_and_push(ctx, "daily %s: ops 补跑（补 %d 个文件路径）" % (D, n))
            if not out["pages"].get("pushed") and "不发站" in (out["pages"].get("reason") or ""):
                out["failed"].append(out["pages"]["reason"])
        elif need_push:
            out["pages"] = {"pushed": False, "reason": "数据不齐（缺文件或需要 AI），先不推"}
        if data_ok and any(m["action"] == "生成主站包" for m in ev["mechanical"]):
            r = subprocess.run([sys.executable, os.path.join(ctx.work, "make_deploy_variants.py"), pkg], cwd=ctx.root,
                               capture_output=True, text=True, timeout=900)
            out["package"] = {"rc": r.returncode, "tail": (r.stdout + r.stderr)[-800:]}
            if r.returncode != 0:
                out["failed"].append("make_deploy_variants 失败")
    return out


# ───────────────────────── 影子对比 ─────────────────────────
def job_compare(ctx, args):
    """读一份 shadow 结果，看 Lomax 同时段实际做了什么，给出 match / mismatch / lomax_not_run。"""
    rp = args.result
    res = load_json(rp)
    t0 = datetime.datetime.fromisoformat(res["started_at"])
    item = res["item"]
    t_end = now()
    cmt = commits_since(ctx, int(t0.timestamp()) - 120)
    cmp = {"compared_at": iso(t_end), "window": [iso(t0 - datetime.timedelta(minutes=2)), iso(t_end)],
           "lomax_commits": [[h[:7], datetime.datetime.fromtimestamp(ct, SH).strftime("%m-%d %H:%M"), s] for h, ct, s in cmt]}
    pre_dir = (res.get("pre") or {}).get("pre_state_dir")
    pre_lib = load_json(os.path.join(pre_dir, "library.json")) if pre_dir and os.path.exists(os.path.join(pre_dir, "library.json")) else None
    cur_lib = load_json(ctx.lib_path)
    actual_diff = proj_diff(projection(pre_lib), projection(cur_lib)) if pre_lib else None
    cmp["lomax_library_diff"] = actual_diff
    new_files = sorted(os.path.basename(f) for f in glob.glob(os.path.join(ctx.work, "*")) if os.path.isfile(f) and os.path.getmtime(f) >= t0.timestamp() - 120)
    cmp["work_files_touched"] = new_files[:60]
    verdict, why = "unknown", []
    if item == "ratings":
        snap = os.path.join(ctx.work, "ratings_snapshot.json")
        ran = os.path.exists(snap) and os.path.getmtime(snap) >= t0.timestamp() - 120
        rc = [c for c in cmt if c[2].startswith("ratings")]
        pred_pub = res["plan"]["would_push_pages"]
        pdiff = res["plan"]["projection_diff"]
        cmp["lomax_ran"] = ran or bool(rc)
        if not cmp["lomax_ran"]:
            verdict = "lomax_not_run"; why.append("窗口内 ratings_snapshot.json 没更新、也没有 ratings 提交（Lomax 这次没跑或还没跑完）")
        else:
            same_pub = pred_pub == bool(rc)
            # 只比「影子预测会变的键」是否 Lomax 也变成同样的值（窗口内别的写入不算）
            same_vals = all((actual_diff or {}).get(k) == v for k, v in pdiff.items())
            extra = {k: v for k, v in (actual_diff or {}).items() if k not in pdiff and k.startswith(("日推评分", "歌手页", "取消收藏"))}
            cmp["extra_changes_by_lomax"] = extra
            if same_pub and same_vals and not extra:
                verdict = "match"
            else:
                verdict = "mismatch"
                if not same_pub: why.append("是否发布不同：影子=%s，Lomax 提交=%s" % (pred_pub, [c[2] for c in rc]))
                if not same_vals: why.append("预测的改动和 Lomax 实际改动不一样")
                if extra: why.append("Lomax 多改了 %d 处（可能是影子之后又有新评分）" % len(extra))
            if rc and res["plan"].get("commit_msg") and rc[-1][2] != res["plan"]["commit_msg"]:
                why.append("提交说明不同：影子「%s」/ Lomax「%s」（数量口径可能不同，不算硬差异）" % (res["plan"]["commit_msg"], rc[-1][2]))
    elif item == "gapfill":
        acted = bool([c for c in cmt if not c[2].startswith(("ratings", "daily"))]) or any(f.startswith(("RETRY_DONE", "GAPFILL", "OPS_")) for f in new_files)
        def gained_file(k, v):
            if k.startswith("歌单文件"): return v[0] is False and v[1] is True
            if k.startswith("歌手页") and isinstance(v[0], list) and isinstance(v[1], list): return (not v[0][2]) and v[1][2]
            return False
        file_changes = {k: v for k, v in (actual_diff or {}).items() if gained_file(k, v)}
        cmp["file_changes"] = file_changes
        pred = res["plan"].get("download", 0) > 0 or "先问宣驰" in (res["plan"].get("reason") or "")
        cmp["lomax_acted"] = acted or bool(file_changes)
        verdict = "match" if pred == cmp["lomax_acted"] else "mismatch"
        if verdict == "mismatch": why.append("影子要补 %s 首；Lomax 实际%s动作" % (res["plan"].get("download", 0), "有" if cmp["lomax_acted"] else "没有"))
    elif item == "dailycheck":
        D = res["date"]
        dc = [c for c in cmt if c[2].startswith("daily %s" % D)]
        pkg = os.path.join(ctx.handoff, "deploy-%s-daily" % D.replace("-", ""))
        pkg_touched = os.path.isdir(pkg) and max([os.path.getmtime(os.path.join(pkg, x)) for x in os.listdir(pkg)] or [0]) >= t0.timestamp() - 120
        daily_changes = {k: v for k, v in (actual_diff or {}).items() if k.startswith("日推评分|%s" % D)}
        lib_touched = os.path.getmtime(ctx.lib_path) >= t0.timestamp() - 120
        cmp["lomax_acted"] = bool(dc) or pkg_touched
        cmp["library_touched_in_window"] = lib_touched
        if res.get("exit_code") == 75:
            verdict = "deferred"; why.append("影子判定 6 点还在跑（不插手），对比以 20 分钟后的复查为准")
        elif res["complete"]:
            verdict = "match" if not cmp["lomax_acted"] else "mismatch"
            if verdict == "mismatch": why.append("影子说齐全，但 Lomax 窗口内又推了/改了主站包")
        else:
            verdict = "match" if (cmp["lomax_acted"] or lib_touched) else "mismatch"
            if verdict == "mismatch": why.append("影子说不齐、要补，但 Lomax 窗口内没有动作")
        cmp["daily_changes"] = daily_changes
    cmp["verdict"] = verdict
    cmp["why"] = why
    res["compare"] = cmp
    dump_json(rp, res)
    with open(os.path.join(ctx.shadow, "SUMMARY.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"item": item, "run": os.path.basename(rp), "started_at": res["started_at"], "verdict": verdict,
                            "predicted_wake": res.get("wake_lomax"), "why": why}, ensure_ascii=False) + "\n")
    log("对比 %s → %s %s" % (os.path.basename(rp), verdict, "；".join(why)))
    return None


# ───────────────────────── 入口 ─────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("item", choices=["ratings", "gapfill", "dailycheck", "compare"])
    ap.add_argument("result", nargs="?")
    ap.add_argument("--mode", choices=["shadow", "live"], default="shadow")
    ap.add_argument("--root", default=os.environ.get("HARMONIA_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    ap.add_argument("--handoff")
    ap.add_argument("--date")
    ap.add_argument("--fake-download", action="store_true")
    args = ap.parse_args()
    real = "/workspace/harmonia-handoff/music-library"
    if args.fake_download and os.path.realpath(args.root) == os.path.realpath(real):
        sys.exit("--fake-download 只能用在测试副本里")
    if os.path.realpath(args.root) != os.path.realpath(real):
        bad = [f for f in glob.glob(os.path.join(args.root, "work", "*.py"))
               if any(r in open(f, encoding="utf-8", errors="ignore").read() for r in HARDCODED_ROOTS)]
        if bad and args.mode == "live":
            sys.exit("副本里还有脚本写死真目录，先跑 rewrite_hardcoded_paths：%s" % ", ".join(os.path.basename(b) for b in bad[:5]))
    ctx = Ctx(args.root, args.handoff, args.mode, args.fake_download)
    if args.item == "compare":
        return job_compare(ctx, args)
    fn = {"ratings": job_ratings, "gapfill": job_gapfill, "dailycheck": job_dailycheck}[args.item]
    t0 = now()
    try:
        res = fn(ctx, args)
    except Exception as e:
        import traceback
        res = {"item": args.item, "mode": ctx.mode, "started_at": iso(t0), "error": "%s: %s" % (type(e).__name__, e),
               "traceback": traceback.format_exc()[-3000:]}
        res["wake"] = [wake(ctx, args.item, "%s:error:%s" % (args.item, stamp(t0)), "%s 脚本出错：%s" % (args.item, res["error"][:200]),
                            notify=("Feynman",), t=t0)]
        save_result(ctx, args.item, t0, res)
        log("出错：", res["error"])
        sys.exit(1)
    save_result(ctx, args.item, t0, res)
    log("%s 完成：%s" % (args.item, res.get("wake_reason") or res.get("decision")))
    if ctx.mode == "shadow":
        prune_state(ctx)
    sys.exit(res.get("exit_code", 0))


if __name__ == "__main__":
    main()
