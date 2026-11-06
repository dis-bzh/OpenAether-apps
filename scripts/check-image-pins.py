#!/usr/bin/env python3
"""check-image-pins.py - no image the DAG deploys floats, and the ones pinned by tag only are counted.

Reads the rendered output of every DAG path (scripts/dev/render-dag.py) and the images named in HelmRelease
values. Errors: an image with neither a tag nor a digest, or tagged `latest`/`main`/`master`/`edge` (it resolves to
whatever was pushed last), a CloudNativePG PostgreSQL image whose tag is not a dated system build
(<major>.<minor>-<12 digits>-system-bookworm|trixie; the plain MM.mm tag is frozen and deprecated upstream), and two
different tags for one PostgreSQL major. Notice: how many are pinned by tag only; a re-pushed tag reaches the cluster
unchecked, so `--require-digest` turns that into an error once Renovate's pinDigests has rewritten the lines.
Images the charts render (helm template) are not looked at here: scripts/dev/gen-sbom.py --helm lists them.

Usage: python3 scripts/check-image-pins.py [--rendered DIR] [--require-digest]
Dependencies: kubectl (to render, unless --rendered is given), PyYAML.
"""
import argparse
import importlib.util
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("gen", HERE / "dev/gen-sbom.py")
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)

FLOATING = {"latest", "main", "master", "edge", "dev", "nightly"}
CNPG_PG = "ghcr.io/cloudnative-pg/postgresql"
CNPG_PG_TAG = re.compile(r"(\d+)\.\d+-\d{12}-system-(?:bookworm|trixie)")


def audit(docs):
    """(errors, tag_only) over every image reference found in the documents."""
    refs = {}
    for d in docs:
        found = set()
        gen.walk_images(d, found)
        for values in [d.get("spec", {}).get("values")] if d.get("kind") == "HelmRelease" else []:
            gen.walk_images(values or {}, found)
        for r in found:
            if gen.valid(r):
                refs.setdefault(r, set()).add(f"{d.get('kind')}/{(d.get('metadata') or {}).get('name')}")
    errors, tag_only, pg_tags = [], [], {}
    for ref, where in sorted(refs.items()):
        name, tag, digest = gen.split_image(ref)
        w = ", ".join(sorted(where))
        if not tag and not digest:
            errors.append(f"{ref} ({w}): no tag and no digest, it floats on the registry's default")
        elif tag in FLOATING and not digest:
            errors.append(f"{ref} ({w}): the tag {tag!r} moves")
        elif not digest:
            tag_only.append(ref)
        if name == CNPG_PG and tag and tag not in FLOATING:
            m = CNPG_PG_TAG.fullmatch(tag)
            if m:
                pg_tags.setdefault(m[1], set()).add(tag)
            else:
                errors.append(f"{ref} ({w}): not a dated system build (<major>.<minor>-<12 digits>-system-bookworm|trixie)")
    for major, tags in sorted(pg_tags.items()):
        if len(tags) > 1:
            errors.append(f"PostgreSQL {major} runs under {len(tags)} tags ({', '.join(sorted(tags))}): one tag per major")
    return errors, tag_only


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--rendered")
    ap.add_argument("--require-digest", action="store_true")
    a = ap.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        rendered = a.rendered
        if not rendered:
            rendered = tmp
            subprocess.run([sys.executable, str(HERE / "dev/render-dag.py"), rendered], check=True,
                           stdout=subprocess.DEVNULL)
        errors, tag_only = audit(gen.load_docs(rendered))
    for e in errors:
        print(f"error: {e}", file=sys.stderr)
    if a.require_digest:
        for t in tag_only:
            print(f"error: {t}: pinned by tag, no @sha256", file=sys.stderr)
        errors += tag_only
    if errors:
        sys.exit(1)
    print(f"OK - no floating image; {len(tag_only)} pinned by tag only, none by digest yet.")


if __name__ == "__main__":
    main()
