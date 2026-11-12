#!/usr/bin/env python3
"""每日推送发站入口（迁 hora 候选）。默认 HARMONIA_TARGET=yecaoyun（旧源，等同 INSTRUCTIONS.md 手工步骤）。
python3 harmonia_publish.py <deploy-dir> [--tag T] [--execute] [--rollback BACKUP]
默认 dry-run 只打印计划；--execute 才写。hora/hora-test 走 pub_remote.py（同目录上传）。
顺序：校验包 sha → 只增图片(不覆盖) → 校验图片 → 备份首页 → 最后覆盖首页 → 校验首页 sha。"""
import argparse, hashlib, os, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harmonia_target as T
WEB = "/var/www/musicbox"
def sums(p): return {l.split(None, 1)[1].strip(): l.split()[0] for l in open(p) if l.strip()}
def lsha(p): return hashlib.sha256(open(p, 'rb').read()).hexdigest()
def main():
    ap = argparse.ArgumentParser(); ap.add_argument("dir"); ap.add_argument("--tag", default=time.strftime("%Y%m%d-%H%M%S") + "-before-publish")
    ap.add_argument("--execute", action="store_true"); ap.add_argument("--rollback"); a = ap.parse_args()
    S = sums(os.path.join(a.dir, "SHA256"))
    for n in ("index.html", "img.tar.gz"):
        if lsha(os.path.join(a.dir, n)) != S[n]: sys.exit("本地包 sha 不符: " + n)
    print("[%s] 包校验 OK index=%s… 图片=%d" % (T.label(), S["index.html"][:12], len(sums(os.path.join(a.dir, "IMG_SHA256")))))
    web = T.rpath(WEB)
    if a.rollback:
        print("计划: 回滚 %s/index.html <- %s" % (web, a.rollback))
        if not a.execute: return print("dry-run，未写")
        if T.is_hora(): print(T._ssh('%s C:/harmonia/pub/pub_remote.py rollback "%s" "%s"' % (T.PY, web, a.rollback)).stdout)
        else: T._ssh("cp -p %s %s/index.html.part && mv -f %s/index.html.part %s/index.html && sha256sum %s/index.html" % (a.rollback, WEB, WEB, WEB, WEB))
        return
    if T.is_hora():
        st = "C:/harmonia/pub/staging-%s-%d" % (a.tag, int(time.time()))  # 每次唯一，避免断线残留 sftp 锁
        print("计划: 上传到 %s → pub_remote publish → %s（备份 index.html.bak.%s）" % (st, web, a.tag))
        if not a.execute: return print("dry-run，未写")
        T._ssh('mkdir "%s"' % st.replace("/", "\\"), check=False)
        T._scp([os.path.join(a.dir, f) for f in ("index.html", "SHA256", "IMG_SHA256")], st + "/")
        # 大包分块上传：每块每次尝试用唯一文件名，断线残留锁不影响重试；远端拼接后由 pub_remote 校验 sha
        import tempfile; parts = []; tmpd = tempfile.mkdtemp(prefix="pubchunk-")
        with open(os.path.join(a.dir, "img.tar.gz"), "rb") as f:
            for n, blk in enumerate(iter(lambda: f.read(4 << 20), b"")):
                for k in range(6):
                    name = "img.part%03d.a%d" % (n, k); lp = os.path.join(tmpd, name); open(lp, "wb").write(blk)
                    try:
                        r = subprocess.run(["scp", "-q"] + T.HOPT + [lp, "%s:%s/%s" % (T.HHOST, st, name)], timeout=180); os.remove(lp)
                        if r.returncode == 0: parts.append(name); break
                    except subprocess.TimeoutExpired: pass
                    time.sleep(5)
                else: sys.exit("分块上传失败 #%d" % n)
        os.rmdir(tmpd)
        cat = "import sys,os;d=sys.argv[1];o=open(os.path.join(d,'img.tar.gz'),'wb');[o.write(open(os.path.join(d,p),'rb').read()) for p in sys.argv[2:]];o.close()"
        open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "_cat.py"), "w").write(cat)
        T._scp([os.path.join(os.path.dirname(os.path.abspath(__file__)), "_cat.py")], "C:/harmonia/pub/")
        T._ssh('%s C:/harmonia/pub/_cat.py "%s" %s' % (T.PY, st, " ".join(parts)), timeout=300)
        T._scp([os.path.join(os.path.dirname(os.path.abspath(__file__)), "pub_remote.py")], "C:/harmonia/pub/")
        r = T._ssh('%s C:/harmonia/pub/pub_remote.py publish "%s" "%s" %s' % (T.PY, st, web, a.tag), timeout=900, check=False)
        print(r.stdout, r.stderr); T._ssh('rmdir /s /q "%s"' % st.replace("/", "\\"), check=False); sys.exit(r.returncode)
    st = "/root/pub-staging-" + a.tag
    cmd = ("cd %s && sha256sum -c --ignore-missing SHA256 && mkdir -p x && tar -xzf img.tar.gz -C x && (cd x && sha256sum -c --quiet ../IMG_SHA256) "
           "&& rsync -a --ignore-existing x/img/ %s/img/ && cp -p %s/index.html %s/index.html.bak.%s && cp index.html %s/index.html.part && mv -f %s/index.html.part %s/index.html "
           "&& sha256sum %s/index.html && rm -rf %s") % (st, WEB, WEB, WEB, a.tag, WEB, WEB, WEB, WEB, st)
    print("计划(yecaoyun 旧源): 上传到 %s; 远端: %s" % (st, cmd))
    if not a.execute: return print("dry-run，未写")
    T._ssh("mkdir -p " + st); T._scp([os.path.join(a.dir, f) for f in ("index.html", "img.tar.gz", "SHA256", "IMG_SHA256")], st + "/"); print(T._ssh(cmd, timeout=900).stdout)
if __name__ == "__main__": main()
