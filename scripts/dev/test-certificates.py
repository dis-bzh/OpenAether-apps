#!/usr/bin/env python3
"""Certificate guards (review of B18/B19, static rung: reads manifests, proves nothing about a cluster).

1. Every cert-manager Certificate names privateKey.rotationPolicy: cert-manager 1.18 flips the unwritten default from
   Never to Always, which would re-key the 10-year listener CA at its next renewal (docs/patching.md, step 1).
2. Every HTTPRoute hostname is in the dnsNames of the Gateway Certificate: the OpenBao role forbids wildcards and bare
   domains, and a route without a name on the certificate is served with a name mismatch while the Gateway stays Programmed.
3. Every DestinationRule `credentialName` is readable by the gateway: istiod refuses to serve a Secret over SDS to a Gateway-API
   gateway unless its ServiceAccount may `list secrets` in the gateway namespace (a Role, not limited by resourceNames: the
   SubjectAccessReview carries no name), and the Secret exists there as a Certificate output (the Vault UI answered 503, lab 11).
4. The offline-signing runbook signs the intermediate for MORE than the gateway leaf's duration: OpenBao refuses a leaf beyond
   the CA's notAfter, and cert-manager then backs off for hours (a lab that signed 7 days had no gateway TLS for an hour).
"""
import re
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


certs, routes, others = [], [], []
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
        elif d.get("kind") in ("DestinationRule", "Gateway", "Role", "RoleBinding"):
            others.append((f.relative_to(REPO).as_posix(), d))

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

GW_NS = "services-gateway"
gateways = [d for p, d in others if d["kind"] == "Gateway" and d["metadata"].get("namespace") == GW_NS]
check("found the services-gateway Gateway", len(gateways) == 1)
# Istio names the ServiceAccount of an auto-deployed gateway <gateway name>-<gateway class>.
sa = f"{gateways[0]['metadata']['name']}-{gateways[0]['spec']['gatewayClassName']}" if gateways else ""
can_list = {d["metadata"]["name"] for p, d in others if d["kind"] == "Role" and d["metadata"].get("namespace") == GW_NS
            and any("secrets" in r.get("resources", []) and "list" in r.get("verbs", []) and not r.get("resourceNames")
                    for r in d.get("rules", []))}
bound = any(d["kind"] == "RoleBinding" and d["metadata"].get("namespace") == GW_NS and d["roleRef"]["name"] in can_list
            and any(x.get("kind") == "ServiceAccount" and x.get("name") == sa and x.get("namespace") == GW_NS
                    for x in d.get("subjects", []))
            for p, d in others)
creds = [(p, d) for p, d in others if d["kind"] == "DestinationRule"
         and d.get("spec", {}).get("trafficPolicy", {}).get("tls", {}).get("credentialName")]
check("found the DestinationRule credentialName", len(creds) >= 1)
for path, d in creds:
    name = d["spec"]["trafficPolicy"]["tls"]["credentialName"]
    check(f"credentialName {name} ({path}) is a Secret issued in {GW_NS}",
          any(c["metadata"].get("namespace") == GW_NS and c["spec"].get("secretName") == name for p, c in certs))
    check("the Role and RoleBinding are in the services-gateway kustomization",
          "gateway-sds-rbac.yaml" in (REPO / "apps/base/services-gateway/kustomization.yaml").read_text())
    check(f"ServiceAccount {sa} may list secrets in {GW_NS} (Role without resourceNames + RoleBinding)", bound)

runbook = (REPO / "apps/base/foundation/vault/pki-root-offline-runbook.md").read_text()
signing = re.search(r"-extensions v3_intermediate_ca -days (\d+)", runbook)
dur = re.fullmatch(r"(\d+)h", str(gw["spec"].get("duration", ""))) if gw else None
check("the runbook signs the intermediate with a -days and the gateway leaf has a duration in hours", bool(signing and dur))
if signing and dur:
    check(f"intermediate {signing.group(1)} days outlives the gateway leaf ({dur.group(1)} h)", int(signing.group(1)) * 24 > int(dur.group(1)))
check("the runbook says to re-trigger the gateway Certificate after the import", "cmctl renew openaether-tls" in runbook)

sys.exit(1 if failures else 0)
