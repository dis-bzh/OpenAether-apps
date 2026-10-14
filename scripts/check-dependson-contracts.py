#!/usr/bin/env python3
"""check-dependson-contracts.py — a Flux Kustomization must depend (transitively) on whoever provides what it uses.

pick.py --validate only checks the DAG's shape. Nothing caught a brick rendering an ExternalSecret on the ClusterSecretStore
`openbao` without waiting for external-secrets-stores, or an istio.io object applied before the Istio CRDs (#38).
This renders every DAG path with `kubectl kustomize`, derives what each brick uses from the rendered objects and
requires the provider to be in its dependsOn closure. Prints `MISSING <kustomization> <provider> (<why>)`, exits 1.

Usage: python3 scripts/check-dependson-contracts.py     Dependency: kubectl, PyYAML.
"""

import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pick import REPO, load_dag  # noqa: E402


def needs(doc):
    """(provider Kustomization, reason) pairs for one rendered object."""
    group = str(doc.get("apiVersion", "")).split("/")[0]
    out = []
    if group.endswith("istio.io"):
        out.append(("istio", f"{doc['kind']} {group}"))
    if group == "gateway.networking.k8s.io":
        out.append(("platform-gateway-api", f"{doc['kind']} {group}"))
    if group == "external-secrets.io" and doc.get("kind") == "ExternalSecret":
        ref = (doc.get("spec") or {}).get("secretStoreRef") or {}
        if ref.get("name") == "openbao":
            out.append(("external-secrets-stores", "ExternalSecret on the store openbao"))
    return out


def closure(bricks, name):
    seen, todo = set(), list(bricks[name].deps)
    while todo:
        n = todo.pop()
        if n not in seen and n in bricks:
            seen.add(n)
            todo += bricks[n].deps
    return seen


def main():
    bricks = load_dag()
    missing, failed = [], []
    for name, brick in sorted(bricks.items()):
        if not brick.path:
            continue
        r = subprocess.run(["kubectl", "kustomize", str(REPO / brick.path.lstrip("./"))],
                           capture_output=True, text=True)
        if r.returncode != 0:
            failed.append(f"{name}: {r.stderr.strip()}")
            continue
        have = closure(bricks, name)
        for doc in yaml.safe_load_all(r.stdout):
            if not isinstance(doc, dict) or not doc.get("kind"):
                continue
            for provider, why in needs(doc):
                if provider != name and provider not in have:
                    line = f"MISSING {name} {provider} ({why})"
                    if line not in missing:
                        missing.append(line)
    for f in failed:
        print(f"BUILD FAILED {f}", file=sys.stderr)
    print("\n".join(missing))
    if missing or failed:
        return 1
    print(f"OK — {len(bricks)} Kustomizations: every cross-Kustomization contract is covered by dependsOn.")
    return 0


sys.exit(main())
