#!/usr/bin/env python3
"""Test of the opt-in Pod Security components: psa-restricted (enforce goes from baseline to restricted) and psa-baseline
(from privileged to baseline, for the namespaces that host nothing privileged).

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
ENFORCE = "pod-security.kubernetes.io/enforce"
# component -> (level before, level after, Flux Kustomization -> the namespaces its path declares that the component must move)
CASES = {
    "psa-restricted": ("baseline", "restricted", {
        "namespaces": {"foundation-networking", "foundation-policy-engine"},
        "identity": {"services-identity"},
        "cluster-api-operator": {"capi-system", "capi-operator-system"},
        "cluster-api-providers": set(),  # capi-outscale-system stays baseline (see the component)
    }),
    "psa-baseline": ("privileged", "baseline", {
        "namespaces": {"services-observability"},
        "istio": {"foundation-service-mesh", "services-gateway"},
        "storage": {"foundation-storage"},
    }),
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
    check("without any component no Flux Kustomization patches a Namespace",
          not any("Namespace" in str(p) for d in plain.values() for p in d["spec"].get("patches", [])))
for comp, (LEVEL_FROM, LEVEL_TO, EXPECTED) in CASES.items():
  print(comp)
  COMPONENT = REPO / "apps/flux/components" / comp
  with tempfile.TemporaryDirectory() as tmp:
    plain = {d["metadata"]["name"]: d for d in render(tmp, [BASE]) if d["kind"] == "Kustomization"}
    with_c = {d["metadata"]["name"]: d for d in render(tmp, [BASE], [COMPONENT]) if d["kind"] == "Kustomization"}
    changed = {n for n in with_c if with_c[n] != plain.get(n)}
    check("the component changes exactly the Kustomizations it names", changed == set(EXPECTED), sorted(changed))
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
        check(f"{name}: they were {LEVEL_FROM} and become {LEVEL_TO}",
              all(before[n]["metadata"]["labels"][ENFORCE] == LEVEL_FROM and after[n]["metadata"]["labels"][ENFORCE] == LEVEL_TO
                  for n in want & set(before)))
        others = {n: after[n]["metadata"] for n in after if n not in want}
        check(f"{name}: every other namespace and every other label is untouched",
              others == {n: before[n]["metadata"] for n in others}
              and all({k: v for k, v in after[n]["metadata"]["labels"].items() if k != ENFORCE}
                      == {k: v for k, v in before[n]["metadata"]["labels"].items() if k != ENFORCE} for n in want))


# The restricted level also has to admit the pods the operator renders for the CAPI providers: lab 15A, on a real cluster,
# found cabpt refused at re-creation (no seccomp / runAsNonRoot); the Outscale provider is left at baseline. The component
# carries manifestPatches on those provider CRs; check they are rendered, and that the plain DAG has none.
with tempfile.TemporaryDirectory() as tmp:
    comp = REPO / "apps/flux/components/psa-restricted"
    with_c = {d["metadata"]["name"]: d for d in render(tmp, [BASE], [comp]) if d["kind"] == "Kustomization"}
    spec = with_c["cluster-api-providers"]["spec"]
    path = REPO / spec["path"]
    plain_p = [d for d in render(tmp, [path]) if d["kind"].endswith("Provider")]
    after_p = {(d["kind"], d["metadata"]["name"]): d for d in render(tmp, [path], patches=spec["patches"]) if d["kind"].endswith("Provider")}
    check("without the component no provider carries manifestPatches", not any("manifestPatches" in d["spec"] for d in plain_p))
    for key, dep in {("BootstrapProvider", "talos"): "cabpt-controller-manager", ("ControlPlaneProvider", "talos"): "cacppt-controller-manager"}.items():
        mp = [yaml.safe_load(m) for m in after_p[key]["spec"].get("manifestPatches", [])]
        sc = mp[0]["spec"]["template"]["spec"]["securityContext"] if mp else {}
        check(f"{key[0]}/{key[1]}: a manifest patch makes {dep} restricted-compliant (runAsNonRoot + RuntimeDefault)",
              len(mp) == 1 and mp[0]["metadata"]["name"] == dep and sc.get("runAsNonRoot") is True
              and sc.get("seccompProfile", {}).get("type") == "RuntimeDefault", mp)
    check("the others (scaleway, openstack, core) are left alone",
          all("manifestPatches" not in d["spec"] for k, d in after_p.items() if k not in {("BootstrapProvider", "talos"), ("ControlPlaneProvider", "talos")}))

print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
