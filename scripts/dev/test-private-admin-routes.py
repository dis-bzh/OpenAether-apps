#!/usr/bin/env python3
"""Test of apps/flux/components/private-admin-routes (opt-in: the OpenBao and Grafana routes leave the shared Gateway).

Renders the Flux DAG with and without the component, then does what Flux does with a Kustomization's `spec.patches`: builds
the Kustomization's `spec.path` with those patches and checks that exactly the intended HTTPRoute disappears and nothing else
does. Without the component nothing changes (the default). Needs kubectl and PyYAML. It does NOT run Flux: that Flux merges
`spec.patches` into a path's kustomization.yaml is Flux's documented behaviour, to be seen on a cluster (LAB-CHECKLIST).
Rung: mocked/static.   Usage: python3 scripts/dev/test-private-admin-routes.py
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
BASE = REPO / "apps/flux/base"
COMPONENT = REPO / "apps/flux/components/private-admin-routes"
# Flux Kustomization -> the HTTPRoute the component must delete from its path
EXPECTED = {"foundation-vault": ("foundation-vault", "openbao"), "observability": ("services-observability", "grafana")}
PASS = FAIL = 0


def check(desc, ok, detail=""):
    global PASS, FAIL
    PASS += bool(ok)
    FAIL += not ok
    print(("  ok   " if ok else "  FAIL ") + desc + ("" if ok else f" -> {detail}"))


def build(directory):
    r = subprocess.run(["kubectl", "kustomize", "--load-restrictor=LoadRestrictionsNone", str(directory)],
                       capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"kubectl kustomize {directory} failed: {r.stderr}")
    return [d for d in yaml.safe_load_all(r.stdout) if d]


def render(work, resources, components=(), patches=()):
    """A kustomization in `work` (relative paths, as Flux's generated one would have)."""
    rel = lambda p: os.path.relpath(p, work)  # noqa: E731
    k = {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization",
         "resources": [rel(p) for p in resources]}
    if components:
        k["components"] = [rel(p) for p in components]
    if patches:
        k["patches"] = list(patches)
    (Path(work) / "kustomization.yaml").write_text(yaml.safe_dump(k))
    return build(work)


def ident(d):
    return (d["kind"], d["metadata"].get("namespace"), d["metadata"]["name"])


with tempfile.TemporaryDirectory() as tmp:
    plain = {d["metadata"]["name"]: d for d in render(tmp, [BASE]) if d["kind"] == "Kustomization"}
    with_c = {d["metadata"]["name"]: d for d in render(tmp, [BASE], [COMPONENT]) if d["kind"] == "Kustomization"}

    check("without the component no Flux Kustomization carries patches for the Gateway routes",
          not any(any("HTTPRoute" in str(p) for p in d["spec"].get("patches", [])) for d in plain.values()))
    changed = {n for n in with_c if with_c[n] != plain.get(n)}
    check("the component changes exactly the two Kustomizations it names", changed == set(EXPECTED), sorted(changed))
    check("it changes nothing but spec.patches",
          all({k: v for k, v in with_c[n]["spec"].items() if k != "patches"}
              == {k: v for k, v in plain[n]["spec"].items() if k != "patches"} for n in EXPECTED))

    for name, (ns, route) in EXPECTED.items():
        spec = with_c[name]["spec"]
        path = REPO / spec["path"]
        before = {ident(d) for d in render(tmp, [path])}
        after = {ident(d) for d in render(tmp, [path], patches=spec["patches"])}
        gone = before - after
        check(f"{name}: the carried patches delete HTTPRoute {ns}/{route} and nothing else",
              gone == {("HTTPRoute", ns, route)} and not after - before, sorted(gone))
    zit = {ident(d) for d in render(tmp, [REPO / "apps/base/identity/zitadel"])}
    check("Zitadel's route is not touched (the identity provider stays reachable)",
          ("HTTPRoute", "services-identity", "zitadel") in zit)

print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
