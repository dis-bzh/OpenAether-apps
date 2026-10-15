#!/usr/bin/env python3
"""Two mistakes a real cluster taught (apps #47, 2026-10-15), checked statically so they cannot come back.

1. The VictoriaMetrics CRs take `spec.securityContext` (pod and container fields in ONE flat block, copied by the
   operator onto every container). `containerSecurityContext` is not a field of these CRs: the CRD keeps unknown fields,
   the API server accepts it, the operator ignores it, and the pods stay without a container-level securityContext.
   The images also run as root, so `runAsNonRoot` without an explicit `runAsUser` fails with CreateContainerConfigError.
2. `Gateway.spec.infrastructure` is not in the Gateway API v1.1.0 CRDs: applying it fails the dry-run of the `istio`
   Kustomization, and everything behind it. It is allowed only once the vendored bundle is v1.2 or later.

Static: it reads the manifests, not a cluster. It also proves it would have caught each mistake.
"""
import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
VM_DIR = REPO / "apps/base/observability/vm-customresources"
GATEWAY_CRDS = REPO / "apps/base/platform/gateway-api/standard-install.yaml"
VM_KINDS = {"VMAgent": [()], "VMAlert": [()], "VMAlertmanager": [()],
            "VMCluster": [("vminsert",), ("vmstorage",), ("vmselect",)]}


def vm_problems(doc):
    """Problems of one VictoriaMetrics CR (a dict)."""
    out = []
    kind, name = doc.get("kind"), doc.get("metadata", {}).get("name")
    if kind not in VM_KINDS:
        return out
    for path in VM_KINDS[kind]:
        node = doc.get("spec", {})
        for key in path:
            node = node.get(key, {})
        where = f"{kind}/{name}" + (f".{path[0]}" if path else "")
        if "containerSecurityContext" in node:
            out.append(f"{where}: containerSecurityContext is not a field of this CR, the operator ignores it; use securityContext")
        sc = node.get("securityContext") or {}
        want = {"runAsNonRoot": True, "allowPrivilegeEscalation": False}
        for k, v in want.items():
            if sc.get(k) is not v:
                out.append(f"{where}: securityContext.{k} must be {v}")
        if (sc.get("capabilities") or {}).get("drop") != ["ALL"]:
            out.append(f"{where}: securityContext.capabilities.drop must be [ALL]")
        if (sc.get("seccompProfile") or {}).get("type") != "RuntimeDefault":
            out.append(f"{where}: securityContext.seccompProfile.type must be RuntimeDefault")
        if not isinstance(sc.get("runAsUser"), int) or sc["runAsUser"] == 0:
            out.append(f"{where}: securityContext.runAsUser must be a non-zero uid (these images run as root)")
    return out


def gateway_problems(doc, bundle):
    if doc.get("kind") != "Gateway" or "infrastructure" not in doc.get("spec", {}):
        return []
    if bundle >= (1, 2):
        return []
    return [f"Gateway/{doc['metadata']['name']}: spec.infrastructure needs Gateway API v1.2 CRDs, the vendored bundle is v{bundle[0]}.{bundle[1]}"]


def bundle_version():
    m = re.search(r"bundle-version: v(\d+)\.(\d+)", GATEWAY_CRDS.read_text())
    if not m:
        sys.exit("✗ no bundle-version annotation in the vendored Gateway API CRDs")
    return int(m.group(1)), int(m.group(2))


GOOD_SC = {"runAsNonRoot": True, "runAsUser": 65534, "allowPrivilegeEscalation": False,
           "capabilities": {"drop": ["ALL"]}, "seccompProfile": {"type": "RuntimeDefault"}}


def self_test():
    ok = True
    def expect(label, problems, want_nonempty):
        nonlocal ok
        if bool(problems) != want_nonempty:
            ok = False
            print(f"✗ self-test: {label}: {problems}")
    agent = {"kind": "VMAgent", "metadata": {"name": "a"}, "spec": {"securityContext": dict(GOOD_SC)}}
    expect("a complete block passes", vm_problems(agent), False)
    wrong = {"kind": "VMAgent", "metadata": {"name": "a"}, "spec": {"containerSecurityContext": dict(GOOD_SC)}}
    expect("the ignored field name is caught", vm_problems(wrong), True)
    root = {"kind": "VMAlert", "metadata": {"name": "a"}, "spec": {"securityContext": {k: v for k, v in GOOD_SC.items() if k != "runAsUser"}}}
    expect("runAsNonRoot without a uid is caught", vm_problems(root), True)
    cluster = {"kind": "VMCluster", "metadata": {"name": "c"}, "spec": {"vminsert": {"securityContext": dict(GOOD_SC)}, "vmstorage": {}, "vmselect": {"securityContext": dict(GOOD_SC)}}}
    expect("a VMCluster component without the block is caught", vm_problems(cluster), True)
    gw = {"kind": "Gateway", "metadata": {"name": "g"}, "spec": {"infrastructure": {"parametersRef": {}}}}
    expect("spec.infrastructure on v1.1 is caught", gateway_problems(gw, (1, 1)), True)
    expect("spec.infrastructure on v1.2 is allowed", gateway_problems(gw, (1, 2)), False)
    return ok


def main():
    problems = []
    seen = 0
    for f in sorted(VM_DIR.glob("*.yaml")):
        for doc in yaml.safe_load_all(f.read_text()):
            if doc and doc.get("kind") in VM_KINDS:
                seen += 1
                problems += [f"{f.name}: {p}" for p in vm_problems(doc)]
    if seen != 4:
        problems.append(f"expected 4 VictoriaMetrics CRs (agent, alert, alertmanager, cluster), found {seen}")
    bundle = bundle_version()
    for f in sorted((REPO / "apps").rglob("*.yaml")):
        text = f.read_text()
        if "kind: Gateway" not in text or f == GATEWAY_CRDS:
            continue
        for doc in yaml.safe_load_all(text):
            if isinstance(doc, dict):
                problems += [f"{f.relative_to(REPO)}: {p}" for p in gateway_problems(doc, bundle)]
    if not self_test():
        sys.exit(1)
    for p in problems:
        print("✗", p)
    if problems:
        sys.exit(1)
    print(f"✓ {seen} VictoriaMetrics CRs carry a complete spec.securityContext with a non-root uid; no Gateway uses spec.infrastructure on bundle v{bundle[0]}.{bundle[1]}; the self-test catches both mistakes")


main()
