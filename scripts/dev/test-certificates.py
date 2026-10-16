#!/usr/bin/env python3
"""Certificate guards (review of B18/B19, static rung: reads manifests, proves nothing about a cluster).

1. Every cert-manager Certificate names privateKey.rotationPolicy: cert-manager 1.18 flips the unwritten default from
   Never to Always, which would re-key the 10-year listener CA at its next renewal (docs/patching.md, step 1).
2. Every HTTPRoute hostname is in the dnsNames of the Gateway Certificate: the OpenBao role forbids wildcards and bare
   domains, and a route without a name on the certificate is served with a name mismatch while the Gateway stays Programmed.
"""
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
SKIP = ("platform/gateway-api/",)  # vendored CRDs, not routes
failures = 0


def check(name, ok):
    global failures
    print(("ok   " if ok else "FAIL ") + name)
    failures += 0 if ok else 1


certs, routes = [], []
for f in sorted((REPO / "apps").rglob("*.yaml")):
    if any(s in f.as_posix() for s in SKIP):
        continue
    try:
        docs = [d for d in yaml.safe_load_all(f.read_text()) if isinstance(d, dict)]
    except yaml.YAMLError:
        continue  # Helm-templated or non-manifest files are not ours to judge
    for d in docs:
        if d.get("kind") == "Certificate" and str(d.get("apiVersion", "")).startswith("cert-manager.io/"):
            certs.append((f.relative_to(REPO).as_posix(), d))
        elif d.get("kind") == "HTTPRoute":
            routes.append((f.relative_to(REPO).as_posix(), d))

check("found the Certificates", len(certs) >= 4)
for path, d in certs:
    pk = d["spec"].get("privateKey", {})
    check(f"{d['metadata']['name']} ({path}) names privateKey.rotationPolicy", pk.get("rotationPolicy") in ("Never", "Always"))
ca = next((d for p, d in certs if d["metadata"]["name"] == "openbao-ca"), None)
check("openbao-ca keeps its key (rotationPolicy Never)", ca is not None and ca["spec"]["privateKey"].get("rotationPolicy") == "Never")

gw = next((d for p, d in certs if d["metadata"]["name"] == "openaether-tls"), None)
names = set(gw["spec"].get("dnsNames", [])) if gw else set()
check("Gateway Certificate has no wildcard dnsName", gw is not None and not any(n.startswith("*") for n in names))
seen = 0
for path, d in routes:
    for h in d["spec"].get("hostnames", []):
        seen += 1
        check(f"HTTPRoute host {h} ({path}) is a dnsName of the Gateway Certificate", h in names)
check("found the HTTPRoute hostnames", seen >= 4)

sys.exit(1 if failures else 0)
