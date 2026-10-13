#!/usr/bin/env python3
"""check-externalsecret-namespaces.py — every External Secrets object lives where the store allows.

The openbao ClusterSecretStore reads every secret/ path in OpenBao, so it is
restricted with `spec.conditions[].namespaces`. An ExternalSecret rendered by a
brick in a namespace outside that list would stay NotReady on a cluster, and
nothing in `kubectl kustomize` shows it. This builds every brick (like
check-object-collisions.py) and fails when:
  - an ExternalSecret / PushSecret / SecretStore sits outside the list,
  - a ClusterExternalSecret / ClusterPushSecret or a second ClusterSecretStore exists,
  - the store has no `conditions` made of explicit `namespaces` (a label selector
    is spoofable by whoever can label a namespace),
  - the Kyverno audit policy excludes a different set of namespaces than the store allows.

Usage: python3 scripts/check-externalsecret-namespaces.py
Dependency: kubectl (kustomize is built in), PyYAML.
"""

import re
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

STORE = "openbao"
POLICY = "restrict-external-secrets-namespaces"
NAMESPACED = {"ExternalSecret", "PushSecret", "SecretStore"}
CLUSTER_SCOPED = {"ClusterExternalSecret", "ClusterPushSecret"}


def allowed_namespaces(store):
    """The namespaces of the store's conditions, or an error string."""
    conds = store.get("spec", {}).get("conditions") or []
    if not conds:
        return None, f"ClusterSecretStore/{STORE} has no spec.conditions: any namespace can use it"
    names = set()
    for c in conds:
        extra = set(c) - {"namespaces"}
        if extra:
            return None, (f"ClusterSecretStore/{STORE} conditions use {sorted(extra)}: only explicit "
                          "`namespaces` are accepted (labels and regexes are spoofable or too wide)")
        names.update(c.get("namespaces") or [])
    if not names:
        return None, f"ClusterSecretStore/{STORE} conditions list no namespace"
    return names, None


def check(docs):
    """docs: iterable of (brick path, object). Returns a list of error strings."""
    docs = [(p, d) for p, d in docs if isinstance(d, dict) and d.get("kind")]
    errors = []
    stores = [(p, d) for p, d in docs if d["kind"] == "ClusterSecretStore"]
    for p, d in stores:
        if d["metadata"]["name"] != STORE:
            errors.append(f"{p}: ClusterSecretStore/{d['metadata']['name']} — only {STORE} is allowed")
    ours = [d for _, d in stores if d["metadata"]["name"] == STORE]
    if not ours:
        return errors + [f"no ClusterSecretStore/{STORE} rendered by any brick"]
    allowed, err = allowed_namespaces(ours[0])
    if err:
        return errors + [err]

    for p, d in docs:
        md = d["metadata"]
        if d["kind"] in CLUSTER_SCOPED:
            errors.append(f"{p}: {d['kind']}/{md['name']} — not used by the platform (copies OpenBao data across namespaces)")
        elif d["kind"] in NAMESPACED and md.get("namespace") not in allowed:
            errors.append(f"{p}: {d['kind']}/{md['name']} in namespace {md.get('namespace')!r}, not in the "
                          f"ClusterSecretStore/{STORE} conditions {sorted(allowed)}")

    for p, d in docs:
        if d["kind"] == "ClusterPolicy" and d["metadata"]["name"] == POLICY:
            excluded = set()
            for rule in d["spec"]["rules"]:
                for e in (rule.get("exclude") or {}).get("any", []):
                    excluded.update(e.get("resources", {}).get("namespaces", []))
            if excluded != allowed:
                errors.append(f"{p}: {POLICY} excludes {sorted(excluded)} but the store allows {sorted(allowed)}")
            break
    else:
        errors.append(f"no ClusterPolicy/{POLICY} rendered (the Audit guard of the store)")
    return errors


def render(path):
    from pick import REPO
    target = REPO / path.lstrip("./")
    r = subprocess.run(["kubectl", "kustomize", str(target)], capture_output=True, text=True)
    if r.returncode != 0:
        return None, r.stderr
    return list(yaml.safe_load_all(r.stdout)), None


def flux_paths():
    """Every ./apps/base/... path a Flux file points at, overlays included (the local
    profile's MinIO ExternalSecret is in none of the base Kustomizations)."""
    from pick import REPO
    found = set()
    for f in (REPO / "apps/flux").rglob("*.yaml"):
        found.update(re.findall(r"\./apps/base/[\w./-]+", f.read_text()))
    return {p for p in found if (REPO / p.lstrip("./") / "kustomization.yaml").exists()}


def main():
    from pick import load_dag
    paths = sorted({b.path for b in load_dag().values() if b.path} | flux_paths())
    docs, errors = [], []
    for path in paths:
        objs, err = render(path)
        if err:
            errors.append(f"{path}: build failed: {err.strip()}")
            continue
        docs += [(path, o) for o in objs or []]
    errors += check(docs)
    if errors:
        print(f"{len(errors)} problem(s):", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)
    n = sum(1 for _, d in docs if isinstance(d, dict) and d.get("kind") in NAMESPACED)
    print(f"OK — {n} External Secrets objects, all in the namespaces the ClusterSecretStore/{STORE} allows.")


if __name__ == "__main__":
    main()
