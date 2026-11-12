"""Hash-gated installer. Fails on drift. Does not copy redacted files onto the host."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

from unified_writer.patch_catalog import (
    ADD_FILES,
    CATALOG,
    GUARD,
    GUARD_V2,
    GUARD_V3,
    PACKAGE_REVISION,
)

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


def assert_loaded_tree() -> Path:
    """Refuse to run if a different unified_writer was imported first."""
    import unified_writer
    loaded = Path(unified_writer.__file__).resolve().parent
    expected = Path(__file__).resolve().parent
    if loaded != expected:
        raise SystemExit(f"stale_package_shadow loaded={loaded} expected={expected}")
    rev_path = expected / "REVISION"
    rev = rev_path.read_text(encoding="utf-8").strip()
    if rev != PACKAGE_REVISION:
        raise SystemExit(f"revision_file_mismatch file={rev} code={PACKAGE_REVISION}")
    return expected


def _finished(text: str, spec: dict) -> bool:
    """A file is done only for the newest marker this revision knows about."""
    if spec.get("verify_only"):
        return False
    if spec.get("v3_anchors") or spec.get("done_marker") == GUARD_V3:
        return GUARD_V3 in text
    if spec.get("v2_anchors"):
        return GUARD_V2 in text
    return spec.get("marker", GUARD) in text


def _body_text(text: str, spec: dict) -> str:
    patterns = [re.compile(item) for item in spec.get("secret_line_res") or []]
    if not patterns:
        return text
    lines = []
    for line in text.splitlines():
        lines.append("<SECRET_LINE>" if any(item.fullmatch(line) for item in patterns) else line)
    body = "\n".join(lines)
    if text.endswith("\n"):
        body += "\n"
    return body


def _body_sha(text: str, spec: dict) -> str:
    return hashlib.sha256(_body_text(text, spec).encode("utf-8")).hexdigest()


def _apply_anchors(text: str, anchors: list, label: str) -> str:
    for anchor in anchors:
        count = text.count(anchor["old"])
        if count != 1:
            raise SystemExit(f"anchor {label} count={count}")
        text = text.replace(anchor["old"], anchor["new"], 1)
    return text


def apply_spec(spec: dict, dest: Path, fixture: bool, check_only: bool) -> str:
    if not dest.is_file():
        raise SystemExit(f"missing {spec['id']}: {dest}")
    text = dest.read_text(encoding="utf-8")
    if spec.get("verify_only"):
        digest = _sha(dest)
        allowed = {spec["src_sha256"]}
        if fixture:
            allowed.add(spec["pack_sha256"])
        if digest not in allowed:
            raise SystemExit(f"drift {spec['id']}: {digest}")
        return "stdout_only"
    if _finished(text, spec):
        return "already"
    digest = _sha(dest)
    allowed = {spec["src_sha256"]}
    if fixture:
        allowed.add(spec["pack_sha256"])
    body_ok = bool(spec.get("body_sha256")) and _body_sha(text, spec) == spec["body_sha256"]
    original = digest in allowed or body_ok
    has_guard = GUARD in text
    if not original and not has_guard:
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
    if original and GUARD not in updated and spec.get("anchors"):
        updated = _apply_anchors(updated, spec["anchors"], spec["id"])
    if spec.get("v2_anchors") and GUARD_V2 not in updated:
        updated = _apply_anchors(updated, spec["v2_anchors"], spec["id"] + ":v2")
    if spec.get("v3_anchors") and GUARD_V3 not in updated:
        updated = _apply_anchors(updated, spec["v3_anchors"], spec["id"] + ":v3")
    if spec.get("anchors") and GUARD not in updated and GUARD_V3 not in updated:
        raise SystemExit(f"patch lost marker: {spec['id']}")
    if not check_only:
        _backup(dest)
        dest.write_text(updated, encoding="utf-8")
    return "upgraded" if has_guard and not original else "patched"


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
    parser.add_argument("--root", default="")
    parser.add_argument("--publish-root", default="")
    parser.add_argument("--music-dl-root", default="")
    parser.add_argument("--layout", choices=("host", "fixture"), default="host")
    parser.add_argument("--fixture", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args(argv)
    loaded = assert_loaded_tree()
    if args.self_check:
        sys.stdout.write(json.dumps({
            "ok": True,
            "package_revision": PACKAGE_REVISION,
            "loaded_from": str(loaded),
            "formal_switch_blocked": True,
        }, ensure_ascii=False, indent=2) + "\n")
        return 0
    if not args.root:
        raise SystemExit("need --root")
    roots = _roots(args)
    report = []
    package_before = ""
    if args.layout == "host":
        dest_pkg = Path(args.root) / "work" / "ops" / "unified_writer"
        rev_file = dest_pkg / "REVISION"
        if rev_file.is_file():
            package_before = rev_file.read_text(encoding="utf-8").strip()
        if not args.check:
            src_pkg = PKG / "unified_writer"
            if dest_pkg.exists() and not (dest_pkg / "prod_guard.py").exists():
                raise SystemExit(f"drift package dir: {dest_pkg}")
            shutil.copytree(src_pkg, dest_pkg, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            cache = dest_pkg / "__pycache__"
            if cache.exists():
                shutil.rmtree(cache)
    package_status = "refreshed" if package_before != PACKAGE_REVISION else "current"
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
        "package": str(Path(args.root) / "work" / "ops" / "unified_writer") if args.layout == "host" else "",
        "package_revision": PACKAGE_REVISION,
        "loaded_from": str(loaded),
        "package_revision_before": package_before,
        "package_status": package_status if args.layout == "host" else "not_copied",
        "import_path": "work/ops (directory that contains the unified_writer package)",
        "note": "revision is checked against this file's package; a stale work/ops copy is not a successful already",
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
