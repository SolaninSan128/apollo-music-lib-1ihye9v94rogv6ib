"""hora 端执行：发布 staging 包到 WEB（只增图片→校验→备份首页→最后原子覆盖首页）。
用法: pub_remote.py publish <staging> <web> <tag> | rollback <web> <backupfile>"""
import hashlib, os, shutil, sys, tarfile, json
def _open(p):
    import time
    for i in range(30):  # scp 断线重试后残留 sftp-server 可能短暂占用
        try: return open(p, 'rb')
        except PermissionError:
            if i == 29: raise
            time.sleep(2)
def sha(p):
    h = hashlib.sha256()
    with _open(p) as f:
        for c in iter(lambda: f.read(1 << 20), b''): h.update(c)
    return h.hexdigest()
def sums(p):
    d = {}
    for l in open(p, encoding='utf-8'):
        if l.strip(): s, n = l.split(None, 1); d[n.strip()] = s
    return d
def publish(st, web, tag):
    rep = {'added': 0, 'skipped_existing': 0, 'existing_mismatch': []}
    S = sums(os.path.join(st, 'SHA256')); I = sums(os.path.join(st, 'IMG_SHA256'))
    for n in ('index.html', 'img.tar.gz'):
        if sha(os.path.join(st, n)) != S[n]: raise SystemExit('SHA MISMATCH ' + n)
    ex = os.path.join(st, 'x'); shutil.rmtree(ex, ignore_errors=True)
    with tarfile.open(os.path.join(st, 'img.tar.gz')) as t:
        for m in t.getmembers():
            if not m.isfile() or '..' in m.name or os.path.isabs(m.name) or not m.name.startswith('img/'): raise SystemExit('BAD TAR ' + m.name)
        t.extractall(ex)
    for n, s in I.items():
        if sha(os.path.join(ex, n)) != s: raise SystemExit('IMG SHA MISMATCH ' + n)
    os.makedirs(os.path.join(web, 'img'), exist_ok=True)
    for n in I:  # 只增不覆盖
        dst = os.path.join(web, n)
        if os.path.exists(dst):
            rep['skipped_existing'] += 1
            if sha(dst) != I[n]: rep['existing_mismatch'].append(n)
            continue
        tmp = dst + '.part'; shutil.copyfile(os.path.join(ex, n), tmp); os.replace(tmp, dst); rep['added'] += 1
    for n in I:
        if not os.path.exists(os.path.join(web, n)): raise SystemExit('IMG MISSING AFTER ' + n)
    idx = os.path.join(web, 'index.html'); bk = None
    if os.path.exists(idx):
        bk = idx + '.bak.' + tag
        k = 1
        while os.path.exists(bk): bk = idx + '.bak.%s.%d' % (tag, k); k += 1  # 不覆盖已有备份
        shutil.copy2(idx, bk); rep['backup'] = bk; rep['old_sha'] = sha(idx)
    tmp = idx + '.part'; shutil.copyfile(os.path.join(st, 'index.html'), tmp)
    if sha(tmp) != S['index.html']: raise SystemExit('INDEX TMP MISMATCH')
    os.replace(tmp, idx); rep['index_sha'] = sha(idx); rep['index_ok'] = rep['index_sha'] == S['index.html']
    shutil.rmtree(ex, ignore_errors=True); print(json.dumps(rep, ensure_ascii=False))
def rollback(web, bk):
    idx = os.path.join(web, 'index.html'); tmp = idx + '.part'; shutil.copyfile(bk, tmp); os.replace(tmp, idx)
    print(json.dumps({'rolled_back_to': bk, 'index_sha': sha(idx)}))
if __name__ == '__main__':
    a = sys.argv[1:]
    publish(*a[1:4]) if a[0] == 'publish' else rollback(*a[1:3])
