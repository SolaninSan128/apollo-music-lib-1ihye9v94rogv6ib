#!/usr/bin/env python3
"""build.py 之后运行：从 music-library/index.html（原始 build，无图标块）生成两个发站文件。
  VPS 版：在 <head> 后插入 work/logo_block_20261024.html（与线上一致的「音波·青」图标块）
  Pages 版：确保不含图标块（GitHub Pages 备份没有 icons/，不放图标标签）
用法：python3 work/make_deploy_variants.py <输出目录>   → <dir>/index.html（VPS）、<dir>/index_pages.html、<dir>/SHA256
2026-10-26 起用它代替手工补图标块；template.html 保持无图标块，避免 Pages 版带上 VPS 专用路径。
2026-10-28 图片拆分起：另把本版网页引用的图片（work/img_used.txt，build.py 生成）复制到 <dir>/img/，
  并打包 <dir>/img.tar.gz（解包即得 img/…），逐个文件的 sha256 写 <dir>/IMG_SHA256。见 work/IMG_SPLIT.md。"""
import hashlib, os, re, shutil, sys, tarfile
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.join(HERE, "..")
BLOCK = open(os.path.join(HERE, "logo_block_20261024.html"), encoding="utf-8").read().strip() + "\n"
RX = re.compile(r"<!-- logo20261024 start -->.*?<!-- logo20261024 end -->\n?", re.S)
def main(out):
    src = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
    pages = RX.sub("", src)
    assert "<head>\n" in pages, "build 输出里没找到 <head>"
    vps = pages.replace("<head>\n", "<head>\n" + BLOCK, 1)
    assert "https://harmonia.corgihora.bid/music/resolve" in vps, "PLAY_RESOLVE 不是 harmonia 域名"
    os.makedirs(out, exist_ok=True)
    lines = []
    for name, txt in (("index.html", vps), ("index_pages.html", pages)):
        p = os.path.join(out, name); open(p, "w", encoding="utf-8").write(txt)
        h = hashlib.sha256(open(p, "rb").read()).hexdigest()
        lines.append("%s  %s" % (h, name)); print(name, h, os.path.getsize(p))
    assert "data:image" not in pages, "网页里还有 data:image——build.py 没走图片拆分？"
    used = [x.strip() for x in open(os.path.join(HERE, "img_used.txt")) if x.strip()]
    os.makedirs(os.path.join(out, "img"), exist_ok=True)
    il = []
    for u in used:
        assert re.match(r"^img/[0-9a-f]{12}\.(jpg|png|webp|gif)$", u), u
        src = os.path.join(ROOT, u); dst = os.path.join(out, u)
        if not os.path.exists(dst): shutil.copy2(src, dst)
        il.append("%s  %s" % (hashlib.sha256(open(dst, "rb").read()).hexdigest(), u))
    open(os.path.join(out, "IMG_SHA256"), "w").write("\n".join(il) + "\n")
    tp = os.path.join(out, "img.tar.gz")
    with tarfile.open(tp, "w:gz") as t:
        for u in used:
            ti = t.gettarinfo(os.path.join(out, u), arcname=u); ti.uid = ti.gid = 0; ti.uname = ti.gname = ""; ti.mode = 0o644; ti.mtime = 0
            t.addfile(ti, open(os.path.join(out, u), "rb"))
    lines.append("%s  img.tar.gz" % hashlib.sha256(open(tp, "rb").read()).hexdigest())
    print("img: %d 个文件，%.1f MB → %s" % (len(used), sum(os.path.getsize(os.path.join(out, u)) for u in used) / 1048576, tp))
    open(os.path.join(out, "SHA256"), "w").write("\n".join(lines) + "\n")
if __name__ == "__main__":
    main(sys.argv[1])
