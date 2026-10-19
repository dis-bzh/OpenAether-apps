#!/usr/bin/env python3
"""In-cluster transport and network-policy hygiene (B25, static rung: reads manifests, proves nothing about a cluster).

Zitadel and Grafana reach CNPG with TLS (`require`), Istio states its TLS floor, pods are denied the cloud metadata
endpoints, and the cert-manager egress rule names no ACME host while no issuer is ACME.
"""
import sys
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

sys.exit(1 if failures else 0)
