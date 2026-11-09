"""Hash-gated installer. Fails on drift. Does not copy redacted files onto the host."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

from unified_writer.patch_catalog import ADD_FILES, CATALOG, GUARD

PKG = Path(__file__).resolve().parents[1]


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _roots(args) -> dict[str, Path]:
    music = Path(args.root)
    if args.layout == "fixture":
        return {"music": music, "publish": music, "musicdl": music}
    if not args.publish_root or not args.music_dl_root:
        raise SystemExit("host layout needs --publish-root and --music-dl-root")
    return {
        "music": music,
        "publish": Path(args.publish_root),
        "musicdl": Path(args.music_dl_root),
    }


def _dest(spec: dict, roots: dict[str, Path], layout: str) -> Path:
    rel = spec["fixture"] if layout == "fixture" else spec["host"]
    return roots[spec["root"]] / rel


def _backup(path: Path) -> None:
    dest = Path(str(path) + ".bak.unified-writer-v1")
    if dest.exists():
        return
    shutil.copy2(path, dest)


def _already(text: str, spec: dict) -> bool:
    """A file we already rewrote contains the guard marker, even if some context lines remain."""
    return spec.get("marker", GUARD) in text


def apply_spec(spec: dict, dest: Path, fixture: bool, check_only: bool) -> str:
    if not dest.is_file():
        raise SystemExit(f"missing {spec['id']}: {dest}")
    text = dest.read_text(encoding="utf-8")
    if _already(text, spec):
        return "already"
    digest = _sha(dest)
    allowed = {spec["src_sha256"]}
    if fixture:
        allowed.add(spec["pack_sha256"])
    if digest not in allowed:
        raise SystemExit(f"drift {spec['id']}: {digest}")
    if spec.get("replace_from"):
        new = (PKG / spec["replace_from"]).read_text(encoding="utf-8")
        if GUARD not in new:
            raise SystemExit(f"replacement missing guard marker: {spec['id']}")
        if not check_only:
            _backup(dest)
            dest.write_text(new, encoding="utf-8")
            dest.chmod(0o755)
        return "replaced"
    updated = text
    for anchor in spec["anchors"]:
        count = updated.count(anchor["old"])
        if count != 1:
            raise SystemExit(f"anchor {spec['id']} count={count}")
        updated = updated.replace(anchor["old"], anchor["new"], 1)
    if GUARD not in updated:
        raise SystemExit(f"patch lost marker: {spec['id']}")
    if not check_only:
        _backup(dest)
        dest.write_text(updated, encoding="utf-8")
    return "patched"


def add_file(spec: dict, dest: Path, check_only: bool) -> str:
    source = PKG / spec["source"]
    blob = source.read_text(encoding="utf-8")
    if dest.exists():
        if dest.read_text(encoding="utf-8") == blob:
            return "already"
        raise SystemExit(f"drift add {spec['id']}: {dest}")
    if not check_only:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(blob, encoding="utf-8")
    return "added"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="patch-install")
    parser.add_argument("--root", required=True)
    parser.add_argument("--publish-root", default="")
    parser.add_argument("--music-dl-root", default="")
    parser.add_argument("--layout", choices=("host", "fixture"), default="host")
    parser.add_argument("--fixture", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    roots = _roots(args)
    report = []
    if not args.check and args.layout == "host":
        dest_pkg = Path(args.root) / "work" / "ops" / "unified_writer"
        src_pkg = PKG / "unified_writer"
        if dest_pkg.exists() and not (dest_pkg / "prod_guard.py").exists():
            raise SystemExit(f"drift package dir: {dest_pkg}")
        shutil.copytree(src_pkg, dest_pkg, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    try:
        for spec in CATALOG:
            dest = _dest(spec, roots, args.layout)
            status = apply_spec(spec, dest, args.fixture, args.check)
            report.append({"id": spec["id"], "status": status, "path": str(dest)})
        for spec in ADD_FILES:
            dest = _dest(spec, roots, args.layout)
            status = add_file(spec, dest, args.check)
            report.append({"id": spec["id"], "status": status, "path": str(dest)})
    except SystemExit as exc:
        if exc.code not in (None, 0):
            sys.stderr.write(str(exc.code) + "\n")
            return 76
        raise
    payload = {
        "ok": True,
        "ready_for_install": True,
        "ready_for_live": False,
        "formal_switch_blocked": True,
        "check_only": args.check,
        "files": report,
        "note": "hashes matched and anchors applied or already present; not installed on a live host by the test suite",
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
