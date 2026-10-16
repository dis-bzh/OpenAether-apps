#!/usr/bin/env python3
"""Test of apps/flux/components/psa-restricted (opt-in: Pod Security enforce goes from baseline to restricted).

Renders the Flux DAG with and without the component, then does what Flux does with a Kustomization's `spec.patches`: builds
the Kustomization's `spec.path` with those patches and checks that exactly the intended namespaces move to
enforce=restricted, that warn/audit stay as they were, and that nothing else changes. Without the component nothing changes
(the default). It does NOT run Flux or an apiserver: which pods restricted would refuse is in LAB-CHECKLIST (B29).
Rung: mocked/static.   Usage: python3 scripts/dev/test-psa-restricted.py   (needs kubectl and PyYAML)
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
BASE = REPO / "apps/flux/base"
COMPONENT = REPO / "apps/flux/components/psa-restricted"
ENFORCE = "pod-security.kubernetes.io/enforce"
# Flux Kustomization -> the namespaces its path declares that the component must raise
EXPECTED = {
    "namespaces": {"foundation-networking", "foundation-policy-engine"},
    "identity": {"services-identity"},
    "cluster-api-operator": {"capi-system", "capi-operator-system"},
    "cluster-api-providers": {"capi-outscale-system"},
}
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
    rel = lambda p: os.path.relpath(p, work)  # noqa: E731
    k = {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization",
         "resources": [rel(p) for p in resources]}
    if components:
        k["components"] = [rel(p) for p in components]
    if patches:
        k["patches"] = list(patches)
    (Path(work) / "kustomization.yaml").write_text(yaml.safe_dump(k))
    return build(work)


with tempfile.TemporaryDirectory() as tmp:
    plain = {d["metadata"]["name"]: d for d in render(tmp, [BASE]) if d["kind"] == "Kustomization"}
    with_c = {d["metadata"]["name"]: d for d in render(tmp, [BASE], [COMPONENT]) if d["kind"] == "Kustomization"}
    check("without the component no Flux Kustomization patches a Namespace",
          not any("Namespace" in str(p) for d in plain.values() for p in d["spec"].get("patches", [])))
    changed = {n for n in with_c if with_c[n] != plain.get(n)}
    check("the component changes exactly the four Kustomizations it names", changed == set(EXPECTED), sorted(changed))
    check("it changes nothing but spec.patches",
          all({k: v for k, v in with_c[n]["spec"].items() if k != "patches"}
              == {k: v for k, v in plain[n]["spec"].items() if k != "patches"} for n in EXPECTED))

    for name, want in EXPECTED.items():
        spec = with_c[name]["spec"]
        path = REPO / spec["path"]
        before = {d["metadata"]["name"]: d for d in render(tmp, [path]) if d["kind"] == "Namespace"}
        after = {d["metadata"]["name"]: d for d in render(tmp, [path], patches=spec["patches"]) if d["kind"] == "Namespace"}
        moved = {n for n in after if after[n]["metadata"]["labels"].get(ENFORCE) != before[n]["metadata"]["labels"].get(ENFORCE)}
        check(f"{name}: exactly {sorted(want)} change their enforce level", moved == want, sorted(moved))
        check(f"{name}: they were baseline and become restricted",
              all(before[n]["metadata"]["labels"][ENFORCE] == "baseline" and after[n]["metadata"]["labels"][ENFORCE] == "restricted"
                  for n in want & set(before)))
        others = {n: after[n]["metadata"] for n in after if n not in want}
        check(f"{name}: every other namespace and every other label is untouched",
              others == {n: before[n]["metadata"] for n in others}
              and all({k: v for k, v in after[n]["metadata"]["labels"].items() if k != ENFORCE}
                      == {k: v for k, v in before[n]["metadata"]["labels"].items() if k != ENFORCE} for n in want))

print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
