#!/usr/bin/env python3
"""Render every brick path of the Flux DAG with `kubectl kustomize` into one file each.

The shared first step of the manifest scan (kubeconform, trivy config) and of the SBOM:
what the cluster is told to run is the rendered output, not the source tree.

Usage: python3 scripts/dev/render-dag.py <out-dir>
Needs kubectl (kustomize is built in) and PyYAML (pick.py). Exits 1 if a brick does not build.
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pick import REPO, load_dag  # noqa: E402


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    paths = sorted({b.path for b in load_dag().values() if b.path})
    failed = 0
    for path in paths:
        res = subprocess.run(["kubectl", "kustomize", str(REPO / path.lstrip("./"))],
                             capture_output=True, text=True)
        if res.returncode != 0:
            print(f"{path}: {res.stderr.strip()}", file=sys.stderr)
            failed += 1
            continue
        (out / (path.strip("./").replace("/", "__") + ".yaml")).write_text(res.stdout)
    print(f"{len(paths) - failed}/{len(paths)} bricks rendered into {out}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
