#!/usr/bin/env python3
"""Build index.html from library.json + covers/; images go to img/ (2026-10-28 img-split, see work/IMG_SPLIT.md).

Usage:  python3 build.py
- Spotify tracks without a cover file get their cover fetched via Spotify oEmbed
  (https://open.spotify.com/oembed?url=<track url>) and saved to covers/<track id>.jpg;
  the filename is written back to library.json.
- Songs without Spotify (or with a failed fetch) use a generated gradient+note placeholder.
Never opens any song page in a browser; only plain HTTP requests are used.
"""
import base64, json, os, re, sys, urllib.parse, urllib.request
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "work"))
import img_split  # work/img_split.py（2026-10-28）

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(HERE, "library.json")
COVERS = os.path.join(HERE, "covers")
OUT = os.path.join(HERE, "index.html")
UA = {"User-Agent": "Mozilla/5.0"}


def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def track_id(url):
    m = re.search(r"open\.spotify\.com/(?:intl-\w+/)?track/([A-Za-z0-9]+)", url or "")
    return m.group(1) if m else None


def compress(data, max_px=300, quality=82):
    """Downscale to <=300px and re-encode as JPEG (keeps index.html small). Falls back to raw bytes without PIL."""
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(data)).convert("RGB")
        im.thumbnail((max_px, max_px))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality, optimize=True)
        return buf.getvalue()
    except Exception as e:
        print("WARN no compression:", e, file=sys.stderr)
        return data


def fetch_cover(song):
    tid = track_id(song.get("spotify"))
    if not tid:
        return ""
    fname = tid + ".jpg"
    path = os.path.join(COVERS, fname)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return fname
    try:
        oe = json.loads(http_get("https://open.spotify.com/oembed?url=" +
                                 urllib.parse.quote(song["spotify"], safe=":/")))
        data = http_get(oe["thumbnail_url"])
        with open(path, "wb") as f:
            f.write(compress(data))
        print("fetched cover", fname)
        return fname
    except Exception as e:
        print("WARN cover fetch failed for", song.get("title"), e, file=sys.stderr)
        return ""


def main():
    os.makedirs(COVERS, exist_ok=True)
    lib = json.load(open(LIB, encoding="utf-8"))
    changed = False
    # 发站前自检（2026-10-22）：歌手页/我的收藏、每日推荐、每日歌手、歌单里缺网盘文件的歌，
    # 从别处的同一首歌（歌手+歌名 / Spotify track id / 网易云 id）补路径；补不上的列清单
    play_fields = None
    try:
        sys.path.insert(0, os.path.join(HERE, "work"))
        import play_fields
        filled, unresolved = play_fields.fill_missing_files(lib, scopes=("rated", "daily", "daily_artist", "playlist"))
        for sec, a, t, where, path in filled:
            print("自检补路径：[%s] %s - %s ← %s（%s）" % (sec, a, t, where, path))
        if filled:
            changed = True
        fav_nofile = [e for sec, e in unresolved if sec == "歌手页/收藏" and e.get("fav")]
        if fav_nofile:
            print("WARN 自检：%d 首收藏歌没有网盘文件、也找不到可补的来源（在「我的收藏」里不能播放）：" % len(fav_nofile), file=sys.stderr)
            for e in fav_nofile:
                print("    - %s - %s  %s" % (e.get("artist"), e.get("title"), e.get("spotify") or e.get("netease") or ""), file=sys.stderr)
        bysec = {}
        for sec, e in unresolved:
            if sec == "歌手页/收藏" and e.get("fav"): continue
            k = sec.split(" ")[0] if sec.startswith("每日") else sec
            bysec.setdefault(k, []).append(e)
        for k, arr in bysec.items():
            print("WARN 自检：%s 有 %d 首没有网盘文件、别处也找不到（只能用 Spotify/网易云）：%s" % (k, len(arr), "、".join("%s - %s" % (e.get("artist"), e.get("title")) for e in arr[:12]) + ("…" if len(arr) > 12 else "")), file=sys.stderr)
        if not filled and not unresolved:
            print("自检：所有栏目的歌都有网盘文件")
    except Exception as e:
        print("WARN 播放路径自检跳过：", e, file=sys.stderr)
    # 图片拆分（2026-10-28 img-split）：图片不再 base64 塞进网页，而是写成 img/<内容sha1前12位>.<扩展名>，
    # 网页里 DATA.covers 只放相对路径；大图另存缩略图（DATA.covers_t），列表/卡片用缩略图，大图位用原图。
    # img/ 只增不删：同图同名自动去重，旧文件留着（线上旧版网页也还能引用）。见 work/IMG_SPLIT.md
    covers = {}     # 原 key → "img/xxxx.jpg"（显示用全图）
    covers_t = {}   # 原 key → "img/yyyy.jpg"（缩略图，仅大图才有）
    def embed(f, kind="cover"):
        if not f or f in covers:
            return
        if f.startswith("daily_artist/"):
            kind = "daily_artist"  # 每日歌手图：原图（过大才高质量重存），另出缩略图
        path = os.path.join(COVERS, f)
        if not os.path.exists(path):
            print("WARN missing image", f, file=sys.stderr)
            return
        full, thumb = img_split.publish(open(path, "rb").read(), kind)
        covers[f] = full
        if thumb:
            covers_t[f] = thumb

    all_songs = [s for p in lib["playlists"] for s in p["songs"]] + lib.get("rated", []) + [s for b in lib.get("daily_batches", []) for s in b.get("songs", [])]
    # 寻声页（2026-10-28）：种子歌 + 四块里的歌，封面同样用 Spotify oEmbed 取
    for x in lib.get("xunsheng", []):
        S = x.get("sections", {})
        all_songs += [x.get("seed", {})] + S.get("same_artist", {}).get("same", []) + S.get("same_artist", {}).get("other", []) + S.get("same_flavor", []) + S.get("step_out", {}).get("roots", []) + S.get("step_out", {}).get("next", [])
    for s in all_songs:
        f = s.get("cover") or ""
        if not f or not os.path.exists(os.path.join(COVERS, f)):
            f = fetch_cover(s)
            if f != (s.get("cover") or ""):
                s["cover"] = f
                changed = True
        embed(f)

    for a in lib.get("artists", []):
        embed(a.get("artist_image") or "")

    # 每日主题封面（covers/themes/...）
    for b in lib.get("daily_batches", []):
        tc = b.get("theme_cover") or ""
        if not tc:
            continue
        path = os.path.join(COVERS, tc)
        if not os.path.exists(path):
            print("WARN missing theme cover", tc, file=sys.stderr)
            continue
        embed(tc, "theme")

    # 每日歌手（covers/daily_artist/...）：歌手照片、代表作/专辑封面，按原图嵌入（不缩图）
    for d in lib.get("daily_artists", []):
        imgs = [d.get("photo") or ""] + [x.get("cover") or "" for x in d.get("songs", []) + d.get("albums", [])] + [v.get("thumb") or "" for v in d.get("live", [])]
        for f in imgs:
            if not f or f in covers:
                continue
            path = os.path.join(COVERS, f)
            if not os.path.exists(path):
                print("WARN missing daily-artist image", f, file=sys.stderr)
                continue
            embed(f, "daily_artist")

    img_split.save_cache()
    if changed:
        json.dump(lib, open(LIB, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    tpl = open(os.path.join(HERE, "template.html"), encoding="utf-8").read()
    # 直接播放 / 动态共用的 HTTPS 主机：以 work/PLAY_RESOLVE.url 为准（quick tunnel 主机名会变）
    pr_file = os.path.join(HERE, "work", "PLAY_RESOLVE.url")
    if os.path.exists(pr_file):
        pr = open(pr_file, encoding="utf-8").read().strip()
        if re.match(r"^https://[^\s\"']+/music/resolve$", pr):
            tpl, n = re.subn(r'var PLAY_RESOLVE="[^"]*";', 'var PLAY_RESOLVE=' + json.dumps(pr) + ';', tpl, count=1)
            if not n:
                print("WARN: template 里没找到 PLAY_RESOLVE 定义", file=sys.stderr)
        else:
            print("WARN: work/PLAY_RESOLVE.url 格式不对，保留 template 里的地址", file=sys.stderr)
    # 动态写接口口令（DONGTAI_API.md：写进前端即公开，只防路人误写）
    tok_file = os.path.join(HERE, "dongtai", "MUSIC_FEED_TOKEN")
    tok = open(tok_file, encoding="utf-8").read().strip() if os.path.exists(tok_file) else ""
    if not tok:
        print("WARN: 没有 dongtai/MUSIC_FEED_TOKEN，「关注」写入会失败", file=sys.stderr)
    tpl = tpl.replace('/*__FEED_TOKEN__*/""', json.dumps(tok))
    # 曲风：内部来源记录（genre_sources / genre_checked）只留在 library.json，不进发布包
    import copy
    pub = copy.deepcopy(lib)
    songs_all = [x for p in pub["playlists"] for x in p["songs"]] + pub.get("rated", []) + [x for b in pub.get("daily_batches", []) for x in b.get("songs", [])]
    missing = [x["title"] for x in songs_all if not x.get("genre_checked")]
    if missing:
        print("WARN: %d 首歌还没有曲风标签记录（新入库的歌？）请先运行：python3 work/genre_fill.py" % len(missing), file=sys.stderr)
    try:
        sys.path.insert(0, os.path.join(HERE, "work"))
        import artist_norm
        bad = sorted({x["artist"] for x in songs_all if artist_norm.anomalies(x["artist"])})
        if bad:
            print("WARN: 艺人名有异常（连接词前缀/别名未并）：%s ——请运行 python3 work/artist_norm.py --fix" % bad, file=sys.stderr)
        none = [x["title"] for x in songs_all if not x.get("netease") and "已按" not in (x.get("netease_note") or "")]
        if none:
            print("WARN: %d 首歌还没查过网易云：请运行 python3 work/ne_resolve.py" % len(none), file=sys.stderr)
    except Exception as e:
        print("WARN artist check skipped:", e, file=sys.stderr)
    for x in songs_all:
        x.pop("genre_sources", None); x.pop("genre_checked", None)
    # 曲风科普里的歌手头像（library.json 里是 data:image），发布包里同样拆成 img/ 文件
    for g in (pub.get("genre_guides") or {}).values():
        for a in g.get("artists", []) or []:
            if isinstance(a.get("img"), str) and a["img"].startswith("data:image"):
                full, _ = img_split.publish(base64.b64decode(a["img"].split(",", 1)[1]), "cover")
                a["img"] = full
    img_split.save_cache()
    payload = json.dumps({"library": pub, "covers": covers, "covers_t": covers_t}, ensure_ascii=False).replace("</", "<\\/")
    html = tpl.replace("/*__DATA__*/null", payload)
    open(OUT, "w", encoding="utf-8").write(html)
    compare_with_live(lib, play_fields)
    print("wrote %s (%.1f KB), %d playlists, %d rated songs, %d covers" % (
        OUT, os.path.getsize(OUT) / 1024, len(lib["playlists"]), len(lib.get("rated", [])), len(covers)))
    used = sorted(set(covers.values()) | set(covers_t.values()) | set(re.findall(r'"(img/[0-9a-f]{12}\.[a-z]+)"', payload)))
    missing_f = [u for u in used if not os.path.exists(os.path.join(HERE, u))]
    open(os.path.join(HERE, "work", "img_used.txt"), "w").write("\n".join(used) + "\n")
    print("img: 本版网页引用 %d 个图片文件（%.1f MB），img/ 目录共 %d 个；清单 work/img_used.txt" % (
        len(used), sum(os.path.getsize(os.path.join(HERE, u)) for u in used if os.path.exists(os.path.join(HERE, u))) / 1048576,
        len([x for x in os.listdir(os.path.join(HERE, "img")) if re.match(r"^[0-9a-f]{12}\.", x)])))
    if missing_f:
        print("WARN: %d 个引用的图片文件不存在：%s" % (len(missing_f), missing_f[:10]), file=sys.stderr)
    if "data:image" in html:
        print("WARN: 网页里还有 data:image（应全部拆成 img/ 文件）", file=sys.stderr)
    if os.path.getsize(OUT) > 50 * 1024 * 1024:
        print("WARN: index.html is over 50 MB (GitHub warns >50MB, rejects >100MB)", file=sys.stderr)


LIVE_URL = "https://solaninsan128.github.io/apollo-music-lib-1ihye9v94rogv6ib/"


def _lib_from_html(h):
    m = re.search(r'\{"library":', h)
    return json.JSONDecoder().raw_decode(h[m.start():])[0]["library"] if m else None


def compare_with_live(lib, play_fields):
    """发站前对比（2026-10-22）：这次能直接播放的歌比线上版本少了 → 醒目警告（不中断 build）。
    线上版本取正式站（带防缓存参数）；拿不到就用 deploy/index.html。"""
    if play_fields is None:
        return
    old, where = None, ""
    try:
        import time
        old = _lib_from_html(http_get(LIVE_URL + "?cb=%d" % int(time.time()), timeout=20).decode("utf-8", "replace")); where = "正式站"
    except Exception as e:
        p = os.path.join(HERE, "deploy", "index.html")
        if os.path.exists(p):
            try:
                old = _lib_from_html(open(p, encoding="utf-8").read()); where = "deploy/index.html"
            except Exception:
                old = None
    if not old:
        print("WARN 播放对比跳过：拿不到上次发布的版本", file=sys.stderr)
        return
    a, b = play_fields.playable_keys(old), play_fields.playable_keys(lib)
    lost = sorted(a - b)
    print("播放对比：线上（%s）能播 %d 条，这次能播 %d 条（+%d / -%d）" % (where, len(a), len(b), len(b - a), len(lost)))
    if lost:
        bar = "!" * 60
        print(bar, file=sys.stderr)
        print("!!! 警告：这次 build 比线上少了 %d 条能直接播放的歌，先别发站，查清原因：" % len(lost), file=sys.stderr)
        for sec, k in lost[:40]:
            print("!!!   [%s] %s" % (sec, k), file=sys.stderr)
        print(bar, file=sys.stderr)


if __name__ == "__main__":
    main()
