#!/usr/bin/env python3
"""助手侧：把服务端评分应用到 library.json，并 build → deploy → push 一条龙。

用法：python3 work/apply_ratings.py              # 读取 KV → 应用 → 写 library.json → 打印口味档案摘要（不构建）
      python3 work/apply_ratings.py --publish    # 同上，有变化则 build.py → 复制到 deploy/ → git commit + push
      python3 work/apply_ratings.py --dry-run    # 只打印将要发生的变化
规则：
  - 只应用 library.json daily_batches 里**已存在**的 (date,id)；其余（可能是滥用写入）一律忽略并报告。
  - rating=1：该歌曲写入 rated（歌手页）；若已在 rated 则保持，不重复。
  - 由 1 改成 2/3/取消：从 rated 移除（只移除来自该日推荐、artist|title 匹配的条目）。
  - 服务端评分是用户最新意图：song['rating'] 同步为服务端值（None=取消），并记录 rated_at。
  - 口味档案摘要写到 work/taste_digest.txt 并打印，助手可粘进口味档案。
  - 八音盒详情页「收藏」按钮（2026-10-20 起）：键为 2000-01-01|f<Spotify id 或哈希>，与日推评分同一个 Worker。
    只应用能在曲库（rated / 歌单 / 日推 / unfavorited 存档）里按 歌手+歌名 找到的歌；
    rating=1 → 进 rated（已有则置 1；在 unfavorited 存档里则原样恢复；否则从歌单/日推条目复制，含网盘文件路径）；
    rating=null（取消收藏）→ 从 rated 移到 library.json 的 unfavorited 存档（不删数据，可再收藏恢复）。
  - 同一首歌有多条记录（日推键 + 收藏键）时，以 ts 最新的一条决定是否在 rated 里。
  - 2026-10-20 收藏清零（library.fav_reset.ts）：ts 早于它的 rating=1 记录一律当作「未收藏」处理（不进 rated、不改回 1）。
    口味条目（rated 里 taste=True，如网易云歌单同步）不算收藏：留在歌手页。
  - 2026-10-20 18:21 起收藏与评分解耦（library.fav_since.ts）：rated[].fav 是「我的收藏」/心形的唯一依据；rating 只是评分。
    评分记录（任何时间）照常同步 rating；ts ≥ fav_since 的心形点击设/取消 fav，日推点「收藏」= rating 1 + fav，
    日推改成别的分/取消 = fav 取消；同一首歌以最后一次为准。早于 fav_since 的记录永远不设 fav。
  - 2026-10-20 19:20 起「任何地方收藏的歌都进歌手页」：心形/日推「收藏」的歌如果歌手页（rated）里没有，
    从来源条目（歌单 / 日推 / 每日歌手）复制进 rated（含网盘文件、封面、曲风、网易云、专辑），fav=True，
    标 from_fav=<来源>，rating 保持原值（只点心形则 None）。取消收藏时只拿掉 from_fav 条目（存进 unfavorited，可恢复）；
    本来就在歌手页的歌一律不动（不重复加、不移除，只改 fav 标记）。
    新歌手：library.artists 补一条；照片优先用每日歌手里的 Apple Music 照片，否则先用歌曲封面并记进 work/ARTIST_PHOTO_TODO.json。
  - 2026-10-22 起：任何途径进 rated 的歌都带上来源条目的 file_flac/file_mp3（及缺的封面、曲风、专辑链接），
    运行结束前再兜底扫一遍 rated（work/play_fields.py），缺文件的按 歌手+歌名 / Spotify id / 网易云 id 补路径。
"""
import json, os, subprocess, sys, datetime, importlib.util
HERE=os.path.dirname(os.path.abspath(__file__)); ROOT=os.path.dirname(HERE)
LIBP=os.path.join(ROOT,"library.json"); DEPLOY=os.path.join(ROOT,"deploy")
spec=importlib.util.spec_from_file_location("poll_ratings",os.path.join(HERE,"poll_ratings.py")); pr=importlib.util.module_from_spec(spec); spec.loader.exec_module(pr)
LABEL=pr.LABEL
def norm(s): return (s or "").strip().lower()
import re as _re, copy as _copy
sys.path.insert(0,HERE)
from play_fields import copy_play_fields, fill_missing_files  # 2026-10-22：进 rated 必带网盘路径
FAV_PREFIX="2000-01-01|f"
def make_fk(lib):
    C=lib.get("artist_canon") or {}
    def akey(s): return _re.sub(r"[./_'’\"“”·・\-]+","",_re.sub(r"\s+","",(s or "").lower()))
    def canon(a):
        a=(a or "").strip()
        if a in C: return C[a]
        return C.get(akey(a),a)
    def fk(a,t): return _re.sub(r"\s+"," ",(canon(a)+"|"+(t or "")).lower()).strip()
    return fk

def main():
    dry="--dry-run" in sys.argv; publish="--publish" in sys.argv
    lib=json.load(open(LIBP,encoding="utf-8"))
    srv_raw=pr.fetch_all()
    RESET=(lib.get("fav_reset") or {}).get("ts",0)
    # 收藏（心形）与评分解耦（2026-10-20 第二次调整）：评分照常同步；只有 ts ≥ FAV_SINCE 的心形点击 / 日推点「收藏」才改 fav
    FAV_SINCE=max(RESET,(lib.get("fav_since") or {}).get("ts",0))
    srv=srv_raw
    n_reset=sum(1 for v in srv.values() if v.get("rating")==1 and v.get("ts",0)<FAV_SINCE)
    songs={}
    for b in lib["daily_batches"]:
        for s in b["songs"]: songs[b["date"]+"|"+s["id"]]=(b,s)
    fk=make_fk(lib)
    ignored=[k for k in srv if k not in songs and not k.startswith(FAV_PREFIX) and not k.startswith("2000-01-02|x")]  # 2026-10-28：2000-01-02|x… 是「寻声」请求，见 work/XUNSHENG.md
    lines=[]; changed=False
    rated=lib["rated"]
    unfav=lib.setdefault("unfavorited",[])
    def tsday(ts): return datetime.datetime.fromtimestamp(ts/1000,datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d")
    # 同一首歌在多天日推里出现：评分以最新一条日推记录为准
    latest_rate={}
    for k,v in srv.items():
        if k in songs:
            kk=fk(songs[k][1]["artist"],songs[k][1]["title"]); latest_rate[kk]=max(latest_rate.get(kk,0),v.get("ts",0))
    # 收藏事件：ts ≥ FAV_SINCE 的心形键记录 + 日推评分记录（日推点「收藏」=收藏，改成别的/取消=不再收藏），最后一次为准
    fav_ev={}
    for k,v in srv.items():
        if v.get("ts",0)<FAV_SINCE: continue
        if k in songs: kk=fk(songs[k][1]["artist"],songs[k][1]["title"])
        elif k.startswith(FAV_PREFIX): kk=fk(v.get("artist"),v.get("title"))
        else: continue
        if kk not in fav_ev or fav_ev[kk][0]<=v.get("ts",0): fav_ev[kk]=(v.get("ts",0),v.get("rating")==1,k,v)
    def find_rated(s):
        for i,r in enumerate(rated):
            if norm(r["artist"])==norm(s["artist"]) and norm(r["title"])==norm(s["title"]): return i
        for i,r in enumerate(rated):
            if fk(r["artist"],r["title"])==fk(s["artist"],s["title"]): return i
        return None
    # ① 日推评分 → 日推条目 rating + 歌手页 rated 的评分（不碰 fav）
    for k,v in sorted(srv.items(),key=lambda kv:kv[1]["ts"]):
        if k not in songs: continue
        b,s=songs[k]; new=v["rating"]; old=s.get("rating")
        when=tsday(v["ts"])
        if new!=old:
            lines.append("  [%s] 《%s》- %s：%s → %s"%(b["date"],s["title"],s["artist"],LABEL[old] if old is not None else "未评",LABEL[new]))
            s["rating"]=new; s["rated_at"]=when if new is not None else ""
            changed=True
        if v.get("ts",0)<latest_rate.get(fk(s["artist"],s["title"]),0): continue
        ri=find_rated(s)
        if new==1 and ri is None:
            rated.append({"artist":s["artist"],"title":s["title"],"album":s.get("album",""),"year":str(s.get("year","")),"rating":1,"fav":False,"rated_date":when,"rating_note":"每日推荐点分收藏（自动同步）","intro":s.get("intro",""),"intro_date":b["date"],"intro_found":bool(s.get("intro_found",True)),"spotify":s.get("spotify",""),"netease":s.get("netease",""),"netease_note":s.get("netease_note",""),"note":"","cover":s.get("cover",""),"live":s.get("live",[])})
            copy_play_fields(rated[-1],s)  # 带上网盘文件路径/曲风/专辑链接，否则「我的收藏」里不能播放
            lines.append("      ＋ 进入歌手页（评分 收藏）：%s - %s"%(s["artist"],s["title"])); changed=True
        elif new==1 and ri is not None and rated[ri].get("rating")!=1 and (rated[ri].get("taste") or rated[ri].get("fav")):
            rated[ri]["rating"]=1; rated[ri]["rated_date"]=when; lines.append("      ＋ 评分改为收藏（歌手页条目）：%s - %s"%(s["artist"],s["title"])); changed=True
        elif new!=1 and ri is not None and rated[ri].get("rating")==1 and (rated[ri].get("taste") or rated[ri].get("fav")):
            rated[ri]["rating"]=None; lines.append("      － 评分不再是收藏（仍在歌手页）：%s - %s"%(s["artist"],s["title"])); changed=True
        elif new!=1 and ri is not None and rated[ri].get("rating")==1:
            r=rated.pop(ri); lines.append("      － 移出歌手页：%s - %s"%(r["artist"],r["title"])); changed=True
    # ② 收藏（fav）：只看 FAV_SINCE 之后的心形 / 日推「收藏」点击
    def ne_id(s):
        m=_re.search(r"[?&]id=(\d+)",(s or {}).get("netease") or "")
        return m.group(1) if m else (str(s.get("netease_id")) if s and s.get("netease_id") else "")
    def find_src(a,t,ne=""):
        r=_find_src(a,t)
        if r[0] is not None or not ne: return r
        for p in lib.get("playlists",[]):
            for s in p["songs"]:
                if ne_id(s)==ne: return _find_src(s["artist"],s["title"])
        for b in lib.get("daily_batches",[]):
            for s in b["songs"]:
                if ne_id(s)==ne: return _find_src(s["artist"],s["title"])
        for d in lib.get("daily_artists",[]):
            for s in d.get("songs",[]):
                if ne_id(s)==ne: return _find_src(s["artist"],s["title"])
        for x in lib.get("xunsheng",[]):
            for s in _xs_songs(x):
                if ne_id(s)==ne: return _find_src(s["artist"],s["title"])
        return None,""
    def _find_src(a,t):
        kk=fk(a,t)
        for p in lib.get("playlists",[]):
            for s in p["songs"]:
                if fk(s["artist"],s["title"])==kk: return s,"歌单："+p.get("name","")
        for b in lib.get("daily_batches",[]):
            for s in b["songs"]:
                if fk(s["artist"],s["title"])==kk: return s,"每日推荐 "+b["date"]
        for d in lib.get("daily_artists",[]):
            for s in d.get("songs",[]):
                if fk(s["artist"],s["title"])==kk:
                    s=dict(s)
                    for f in ("why","starter","netease_id","dest"): s.pop(f,None)
                    return s,"每日歌手 "+d["date"]
        # 2026-10-30：寻声扩散页里点的收藏也能进歌手页
        for x in lib.get("xunsheng",[]):
            for s in _xs_songs(x):
                if fk(s["artist"],s["title"])==kk:
                    s=dict(s)
                    for f in ("why","starter","netease_id","dest"): s.pop(f,None)
                    return s,"寻声 "+((x.get("seed") or {}).get("title") or x.get("id",""))
        return None,""
    def _xs_songs(x):
        out=[]
        def walk(o):
            if isinstance(o,dict):
                if o.get("artist") and o.get("title"): out.append(o)
                for v in o.values(): walk(v)
            elif isinstance(o,list):
                for v in o: walk(v)
        walk(x.get("sections") or {})
        return out
    C=lib.get("artist_canon") or {}
    def canon_name(a):
        a=(a or "").strip(); ak=_re.sub(r"[./_'’\"“”·・\-]+","",_re.sub(r"\s+","",a.lower()))
        return C.get(a) or C.get(ak) or a
    photo_todo=[]
    def ensure_artist(e):
        name=e["artist"]; arts=lib.setdefault("artists",[])
        if any(canon_name(a.get("name"))==name for a in arts): return
        # 歌手页已有这位歌手（其他歌曲）→ 不新建
        if any(r is not e and canon_name(r.get("artist"))==name and (r.get("rating")==1 or r.get("taste") or r.get("fav")) for r in rated): return
        ent={"name":name,"artist_image":"","note":"收藏带进歌手页（%s）"%tsday(int(datetime.datetime.now().timestamp()*1000))}
        for d in lib.get("daily_artists",[]):
            if canon_name(d.get("name"))==name and d.get("photo"):
                ent["artist_image"]=d["photo"]; ent["photo_source"]=d.get("photo_credit") or "每日歌手照片"
        if not ent["artist_image"]:
            ent["note"]+="；暂无艺人照片，先用歌曲封面（待从 Apple Music 艺人页补）"
            photo_todo.append({"artist":name,"song":e["title"],"fallback_cover":e.get("cover",""),"added":tsday(int(datetime.datetime.now().timestamp()*1000)),"todo":"从 Apple Music 艺人页取照片存 covers/artists/ 并填 library.artists[].artist_image"})
        arts.append(ent); lines.append("      ＋ 歌手页新歌手：%s（%s）"%(name,"Apple Music 照片" if ent["artist_image"] else "暂用歌曲封面，已记 TODO"))
    fav_unknown=[]
    for kk,(ts,on,k,v) in sorted(fav_ev.items(),key=lambda x:x[1][0]):
        when=tsday(ts); how="日推点「%s」"%LABEL.get(v.get("rating"),"取消") if k in songs else "点心形"
        ri=next((i for i,r in enumerate(rated) if fk(r["artist"],r["title"])==kk),None)
        vne=ne_id(v)
        if ri is None and vne: ri=next((i for i,r in enumerate(rated) if ne_id(r)==vne),None)
        ui=next((i for i,r in enumerate(unfav) if fk(r["artist"],r["title"])==kk),None)
        if on:
            if ri is not None:
                if not rated[ri].get("fav"):
                    rated[ri]["fav"]=True; rated[ri]["fav_date"]=when
                    rated[ri]["rating_note"]=((rated[ri].get("rating_note") or "")+"；%s %s→收藏"%(when,how)).lstrip("；")
                    lines.append("  [收藏] 《%s》- %s（%s）"%(rated[ri]["title"],rated[ri]["artist"],how)); changed=True
            elif ui is not None:
                e=unfav.pop(ui); e["fav"]=True; e["fav_date"]=when; e.pop("unfav_date",None)
                if e.get("rating")==1 and k.startswith(FAV_PREFIX): e["rating"]=None
                e["rating_note"]=((e.get("rating_note") or "")+"；%s 在八音盒重新收藏"%when).lstrip("；")
                rated.append(e); lines.append("  [收藏] 《%s》- %s：从取消收藏存档恢复"%(e["title"],e["artist"])); changed=True
            else:
                src,where=find_src(v.get("artist"),v.get("title"),vne)
                if src is None: fav_unknown.append(k); continue
                e=_copy.deepcopy(src)
                for f in ("rating","rated_at","time_sec","id","kind","reason","youtube"): e.pop(f,None)
                if not e.get("netease") and src.get("netease_id"): e["netease"]="https://music.163.com/song?id=%s"%src["netease_id"]
                # 评分保持原值：日推点「收藏」= 1；只点心形 = None
                e.update({"rating":(1 if (k in songs and v.get("rating")==1) else None),"fav":True,"fav_date":when,"rated_date":when,"rating_note":"在八音盒点收藏（自动同步，来自%s）"%where,"source":"hb_fav","from_fav":where})
                e["artist"]=canon_name(e["artist"])
                e.setdefault("intro",""); e.setdefault("intro_date",""); e.setdefault("intro_found",bool(e.get("intro")))
                e.setdefault("note",""); e.setdefault("netease_note",""); e.setdefault("cover","")
                e["year"]=str(e.get("year","")) if e.get("year","")!="" else ""
                rated.append(e); lines.append("  [收藏] 《%s》- %s：新进歌手页/我的收藏（来自%s）"%(e["title"],e["artist"],where)); changed=True
                ensure_artist(e)
        else:
            if ri is not None and rated[ri].get("fav"):
                r=rated[ri]; r["fav"]=False; r.pop("fav_date",None)
                r["rating_note"]=((r.get("rating_note") or "")+"；%s 取消收藏（%s）"%(when,how)).lstrip("；")
                if not (r.get("from_fav") or r.get("source")=="hb_fav") or r.get("rating") in (1,2) or r.get("taste"):
                    lines.append("  [取消收藏] 《%s》- %s：移出我的收藏（歌手页条目保留不动）"%(r["title"],r["artist"]))
                else:
                    rated.pop(ri); r["unfav_date"]=when; unfav.append(r)
                    lines.append("  [取消收藏] 《%s》- %s：移出歌手页/我的收藏（条目存进 unfavorited，可恢复）"%(r["title"],r["artist"]))
                changed=True
    # ③ 兜底：rated 里任何缺网盘文件的歌（含以上新进的），从日推/每日歌手/歌单里的同一首歌补路径
    _filled,_ = fill_missing_files(lib,fk,extra=True)
    for a,t,where,path in _filled:
        lines.append("      ＋ 补网盘文件：%s - %s ← %s（%s）"%(a,t,where,path)); changed=True
    if fav_unknown: print("收藏记录里有 %d 条在曲库找不到对应歌曲，已忽略：%s"%(len(fav_unknown),", ".join(fav_unknown[:5])))
    if not unfav: lib.pop("unfavorited",None)
    # 口味档案摘要
    tally={1:[],2:[],3:[]}
    for k,v in srv_raw.items():  # 口味摘要按原始评分（清零只影响收藏状态，不抹口味）
        if k in songs and v["rating"] in tally: tally[v["rating"]].append((songs[k][0]["date"],songs[k][1]))
    dg=["【每日推荐 · 口味档案摘要 · %s】"%datetime.date.today().isoformat()]
    for r_,name in ((1,"收藏（点 1）"),(2,"还不错（点 2）"),(3,"没感觉（点 3）")):
        if tally[r_]:
            dg.append("%s %d 首："%(name,len(tally[r_]))+"；".join("%s《%s》(%s)"%(s["artist"],s["title"],d[5:]) for d,s in sorted(tally[r_],key=lambda x:x[0])))
    arts={}
    for d,s in tally[1]: arts[s["artist"]]=arts.get(s["artist"],0)+1
    if arts: dg.append("收藏最多的艺人："+"、".join("%s×%d"%(a,n) for a,n in sorted(arts.items(),key=lambda x:-x[1])[:5]))
    skip=[s["artist"] for d,s in tally[3]]
    if skip: dg.append("没感觉的方向（少推）："+"、".join(sorted(set(skip))))
    digest="\n".join(dg)
    if n_reset: print("收藏分界（fav_since）前的 rating=1 记录 %d 条：只算评分，不算收藏"%n_reset)
    print("收藏（fav）现有 %d 首"%sum(1 for r in rated if r.get("fav")))
    print("服务端 %d 条评分；忽略 %d 条（不在已知日推里）%s"%(len(srv),len(ignored),(": "+", ".join(ignored[:5])) if ignored else ""))
    print("\n".join(lines) if lines else "  （library.json 无需变化）")
    print("\n"+digest)
    if dry: return
    open(os.path.join(HERE,"taste_digest.txt"),"w",encoding="utf-8").write(digest+"\n")
    if changed:
        json.dump(lib,open(LIBP,"w",encoding="utf-8"),ensure_ascii=False,indent=1)
        print("\nlibrary.json 已更新")
    pr_snap=os.path.join(HERE,"ratings_snapshot.json")
    json.dump(srv,open(pr_snap,"w",encoding="utf-8"),ensure_ascii=False,indent=1,sort_keys=True)
    if photo_todo:
        tp=os.path.join(HERE,"ARTIST_PHOTO_TODO.json")
        old=json.load(open(tp,encoding="utf-8")) if os.path.exists(tp) else []
        have={x["artist"] for x in old}
        old+= [x for x in photo_todo if x["artist"] not in have]
        json.dump(old,open(tp,"w",encoding="utf-8"),ensure_ascii=False,indent=1)
        print("新歌手缺照片 %d 位，已记进 work/ARTIST_PHOTO_TODO.json"%len(photo_todo))
    if publish and changed:
        # 入库流程：新歌补专辑链接与曲风标签（增量，已有的跳过；联网失败不阻断发布）
        for sc,args in (("artist_norm.py",["--fix"]),("ne_resolve.py",[]),("album_links.py",[]),("genre_fill.py",[])):
            subprocess.call([sys.executable,os.path.join(HERE,sc)]+args,cwd=ROOT)
        subprocess.check_call([sys.executable,os.path.join(ROOT,"build.py")],cwd=ROOT)
        for f in ("index.html","apple-touch-icon.png","manifest.webmanifest"):
            p=os.path.join(ROOT,f)
            if os.path.exists(p): subprocess.check_call(["cp","-f",p,os.path.join(DEPLOY,f)])
        subprocess.check_call(["git","add","index.html"],cwd=DEPLOY)
        if subprocess.call(["git","diff","--staged","--quiet"],cwd=DEPLOY)!=0:
            subprocess.check_call(["git","commit","-m","ratings: sync from Cloudflare KV (%d changes)"%len([l for l in lines if l.startswith("  [")])],cwd=DEPLOY)
            subprocess.check_call(["git","push","origin","main"],cwd=DEPLOY)
            print("已 build → deploy → push")
    elif publish: print("无变化，不发布")
if __name__=="__main__": main()
