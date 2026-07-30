#!/usr/bin/env python3
"""check-object-collisions.py — detects two bricks rendering the same object.

Each apps/base/<brick> is `kubectl kustomize`-clean on its own, but Flux picks
several bricks into the same cluster (see pick.py) — two bricks defining the
same (apiVersion, kind, namespace, name) then race for ownership. Caught for
real on 2026-07-29: apps/base/storage vendored its own local-path-provisioner
copy that collided with apps/base/platform/local-path-provisioner whenever a
profile picked both (see apps/base/storage/kustomization.yaml). A single
`kubectl kustomize <path>` can't see this — only comparing rendered output
ACROSS bricks can.

Usage: python3 scripts/check-object-collisions.py
Dependency: kubectl (kustomize is built in), PyYAML.
"""

import collections
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pick import REPO, load_dag  # noqa: E402


def render(path):
    target = REPO / path.lstrip("./")
    result = subprocess.run(
        ["kubectl", "kustomize", str(target)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None, result.stderr
    return list(yaml.safe_load_all(result.stdout)), None


def main():
    bricks = load_dag()
    paths = sorted({b.path for b in bricks.values() if b.path})

    owners = collections.defaultdict(set)
    build_errors = []
    for path in paths:
        docs, err = render(path)
        if err:
            build_errors.append(f"{path}: {err.strip()}")
            continue
        for doc in docs or []:
            if not isinstance(doc, dict) or not doc.get("kind"):
                continue
            md = doc.get("metadata", {})
            key = (doc.get("apiVersion"), doc["kind"], md.get("namespace"), md.get("name"))
            owners[key].add(path)

    if build_errors:
        print(f"{len(build_errors)} brick(s) failed to build:", file=sys.stderr)
        for e in build_errors:
            print(f"  - {e}", file=sys.stderr)

    collisions = {k: sorted(v) for k, v in owners.items() if len(v) > 1}
    if collisions:
        print(f"{len(collisions)} cross-brick object collision(s):", file=sys.stderr)
        for (api, kind, ns, name), sources in collisions.items():
            print(f"  - {kind}/{name} (ns={ns}, {api}) rendered by: {', '.join(sources)}", file=sys.stderr)

    if build_errors or collisions:
        sys.exit(1)
    print(f"OK — {len(paths)} bricks built, {len(owners)} objects, no cross-brick collisions.")


if __name__ == "__main__":
    main()
