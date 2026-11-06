#!/usr/bin/env python3
"""In-cluster transport and network-policy hygiene (B25, static rung: reads manifests, proves nothing about a cluster).

Zitadel and Grafana reach CNPG with TLS (`require`), Istio states its TLS floor, pods are denied the cloud metadata
endpoints, the cert-manager egress rule names no ACME host while no issuer is ACME, and the ready-made same-namespace allow
(apps/components/allow-same-namespace) renders what the default-deny header says it is (apps #112).
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
failures = 0


def check(name, ok):
    global failures
    print(("ok   " if ok else "FAIL ") + name)
    failures += 0 if ok else 1


def load(rel):
    return [d for d in yaml.safe_load_all((REPO / rel).read_text()) if d]


zit = next(d for d in load("apps/base/identity/zitadel/helmrelease.yaml") if d["kind"] == "HelmRelease")["spec"]["values"]["zitadel"]["configmapConfig"]["Database"]["Postgres"]
for who in ("User", "Admin"):
    check(f"zitadel {who} database SSL mode is require", zit[who]["SSL"]["Mode"] == "require")

graf = next(d for d in load("apps/base/observability/grafana.yaml") if d.get("kind") == "HelmRelease")
check("grafana ssl_mode is require", graf["spec"]["values"]["grafana.ini"]["database"]["ssl_mode"] == "require")

istio = next(d for d in load("apps/base/istio/istiod.yaml") if d.get("kind") == "HelmRelease" and d["metadata"]["name"] == "istiod")
# tlsDefaults.minProtocolVersion is inert (the field covers non-ISTIO_MUTUAL traffic and only ecdhCurves/cipherSuites);
# a documented control that does nothing is worse than none, so the key must not come back without a test that proves it.
check("istiod does not carry the inert tlsDefaults.minProtocolVersion",
      "minProtocolVersion" not in istio["spec"]["values"]["meshConfig"].get("tlsDefaults", {}))

np = REPO / "apps/base/platform/network-policies"
kust = yaml.safe_load((np / "kustomization.yaml").read_text())
check("deny-cloud-metadata.yaml is in the kustomization", "deny-cloud-metadata.yaml" in kust["resources"])
deny = load("apps/base/platform/network-policies/deny-cloud-metadata.yaml")[0]
spec = deny["spec"]
cidrs = {c for r in spec.get("egressDeny", []) for c in r.get("toCIDR", [])}
check("metadata deny covers both endpoints", cidrs == {"169.254.169.254/32", "169.254.42.42/32"})
check("metadata deny selects every pod", spec["endpointSelector"] == {})
check("metadata deny has NO allow `egress` (it would intersect with allow-dns / allow-kube-api)", "egress" not in spec)

hosts = [f.get("matchName", f.get("matchPattern", "")) for r in
         load("apps/base/platform/network-policies/allow-infra-egress-restricted.yaml")[0]["spec"]["egress"]
         for f in r.get("toFQDNs", [])]
acme_issuer = any(d.get("kind") in ("ClusterIssuer", "Issuer") and "acme" in d.get("spec", {})
                  for p in (REPO / "apps").rglob("*.yaml") if "gotk" not in str(p)
                  for d in yaml.safe_load_all(p.read_text()) if isinstance(d, dict))
check("no ACME host in the egress allow-list while no issuer is ACME", acme_issuer or not any("letsencrypt" in h for h in hosts))

# apps #112: the default-deny ingress also blocks a new namespace's own pods (lab 11, lab 25: HBONE 15008 timed out until a CNP
# allowed it). The ready-made allow is a Kustomize component, one CNP; the including Kustomization sets the namespace.
with tempfile.TemporaryDirectory() as t:
    (Path(t) / "kustomization.yaml").write_text(yaml.safe_dump({
        "apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization", "namespace": "demo-ns",
        "components": [os.path.relpath(REPO / "apps/components/allow-same-namespace", t)]}))
    r = subprocess.run(["kubectl", "kustomize", "--load-restrictor", "LoadRestrictionsNone", t], capture_output=True, text=True)
rendered = [d for d in yaml.safe_load_all(r.stdout) if d] if r.returncode == 0 else []
one = rendered[0] if len(rendered) == 1 else {}
rule = (one.get("spec") or {}).get("ingress") or []
check("allow-same-namespace renders exactly one CiliumNetworkPolicy in the namespace that includes it",
      r.returncode == 0 and one.get("kind") == "CiliumNetworkPolicy" and one["metadata"].get("namespace") == "demo-ns")
check("...it selects every pod of the namespace and admits `fromEndpoints: [{}]` (the same namespace) on any port, nothing else",
      one.get("spec", {}).get("endpointSelector") == {} and rule == [{"fromEndpoints": [{}]}])
check("the default-deny header points at the component, not at a template that is 'not shipped yet'",
      "not shipped yet" not in (np / "default-deny-all.yaml").read_text() and "allow-same-namespace" in (np / "default-deny-all.yaml").read_text())

sys.exit(1 if failures else 0)
