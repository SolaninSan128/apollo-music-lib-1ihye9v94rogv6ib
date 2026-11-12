#!/usr/bin/env python3
"""助手侧：读取 Cloudflare KV 里的全部评分，输出与上次快照的差异，并保存快照。

用法：python3 work/poll_ratings.py            # 读取 + 打印差异 + 更新快照
      python3 work/poll_ratings.py --no-save  # 只看差异不更新快照
      python3 work/poll_ratings.py --json     # 机器可读输出（含 diff）
首选读 Worker GET /ratings（Durable Object 强一致）；失败时用 <CF_CRED_PATH> 读 KV 镜像（令牌不打印、不落盘）。
"""
import json, os, sys, urllib.request, urllib.parse
HERE=os.path.dirname(os.path.abspath(__file__))
SNAP=os.path.join(HERE,"ratings_snapshot.json")
NSID=open(os.path.join(HERE,"..","worker","<KV_NS_FILE>")).read().strip()
TOK=open("<CF_CRED_PATH>").read().strip(); ACC=open("<CF_CRED_PATH>").read().strip()
LABEL={1:"收藏",2:"还不错",3:"没感觉",None:"取消评分"}

def api(path):
    req=urllib.request.Request("https://api.cloudflare.com/client/v4/accounts/%s/storage/kv/namespaces/%s%s"%(ACC,NSID,path),headers={"Authorization":"Bearer "+TOK,"User-Agent":"harmonia-poll"})
    with urllib.request.urlopen(req,timeout=30) as r: return json.load(r)

WORKER="https://harmonia-ratings.harmonia-box.workers.dev"
def fetch_worker():
    """首选：Worker GET /ratings（Durable Object，强一致，不需要令牌）。"""
    req=urllib.request.Request(WORKER+"/ratings",headers={"User-Agent":"harmonia-poll","Origin":"https://solaninsan128.github.io"})
    with urllib.request.urlopen(req,timeout=30) as r: return json.load(r)["ratings"]

def fetch_all():
    try: return fetch_worker()
    except Exception as e: print("（Worker 读取失败，改读 KV 镜像：%s）"%type(e).__name__,file=sys.stderr)
    return fetch_kv()

def fetch_kv():
    out={}; cursor=None
    while True:
        p="/keys?prefix=r:&limit=1000"+("&cursor="+urllib.parse.quote(cursor) if cursor else "")
        d=api(p)
        for k in d["result"]:
            m=k.get("metadata")
            if m: out[k["name"][2:]]=m
        cursor=(d.get("result_info") or {}).get("cursor")
        if not cursor: break
    return out

def diff(old,new):
    added,changed,removed=[],[],[]
    for k,v in new.items():
        o=old.get(k)
        if o is None: added.append((k,v))
        elif o.get("rating")!=v.get("rating"): changed.append((k,o,v))
    for k,o in old.items():
        if k not in new: removed.append((k,o))
    return added,changed,removed

def main():
    save="--no-save" not in sys.argv
    new=fetch_all()
    old=json.load(open(SNAP,encoding="utf-8")) if os.path.exists(SNAP) else {}
    a,c,r=diff(old,new)
    if "--json" in sys.argv:
        print(json.dumps({"total":len(new),"added":a,"changed":[(k,o.get("rating"),v.get("rating")) for k,o,v in c],"removed":r},ensure_ascii=False))
    else:
        print("服务端评分共 %d 条；上次快照 %d 条"%(len(new),len(old)))
        for k,v in sorted(a,key=lambda x:x[1]["ts"]): print("  新增  %s  《%s》- %s → %s"%(k.split("|")[0],v["title"],v["artist"],("寻声请求" if v["rating"]==1 else "撤回寻声") if k.startswith("2000-01-02|x") else LABEL[v["rating"]]))
        for k,o,v in sorted(c,key=lambda x:x[2]["ts"]): print("  改分  %s  《%s》- %s：%s → %s"%(k.split("|")[0],v["title"],v["artist"],LABEL[o.get("rating")] if o.get("rating") is not None else "未评",LABEL[v["rating"]]))
        for k,o in r: print("  服务端已无  %s"%k)
        if not (a or c or r): print("  （没有变化）")
    if save:
        json.dump(new,open(SNAP,"w",encoding="utf-8"),ensure_ascii=False,indent=1,sort_keys=True)
        if "--json" not in sys.argv: print("快照已更新 → work/ratings_snapshot.json")
if __name__=="__main__": main()
