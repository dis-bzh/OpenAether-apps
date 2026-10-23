#!/usr/bin/env python3
"""The Zitadel Login V2 UI is reachable through the Gateway, and its image optimizer is not (apps #161).

Three static facts, each of which was missing or can silently regress:
  - the Zitadel HTTPRoute sends /ui/v2/login to zitadel-login:3000 (without it the authorize step ends on a 404);
  - /ui/v2/login/_next/image is a longer prefix that goes to the server, which answers 404 (the optimizer carries the Next.js
    advisories recorded as not affected in the VEX file: keep it unreachable while those statements stand);
  - the Gateway may reach zitadel-login on its targetPort 3000 (Cilium enforces on the pod port), and no wider than that.
Static: it proves the manifests, not the browser flow (that is a lab check).
"""
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
LOGIN, OPTIMIZER = "/ui/v2/login", "/ui/v2/login/_next/image"


def rules():
    route = next(d for d in yaml.safe_load_all((REPO / "apps/base/identity/zitadel/httproute.yaml").read_text()) if d)
    return [(m["path"]["value"], b["name"], b["port"]) for r in route["spec"]["rules"] for m in r["matches"] for b in r["backendRefs"]]


def gateway_login_egress():
    for d in yaml.safe_load_all((REPO / "apps/base/istio/networkpolicy.yaml").read_text()):
        if d and d.get("kind") == "CiliumNetworkPolicy" and d["metadata"]["name"] == "openaether-gateway":
            return [e for e in d["spec"]["egress"]
                    if any(t.get("matchLabels", {}).get("app.kubernetes.io/name") == "zitadel-login" for t in e.get("toEndpoints", []))]
    return None


def main():
    bad = 0
    rs = rules()
    by_path = {p: (n, port) for p, n, port in rs}
    if by_path.get(LOGIN) != ("zitadel-login", 3000):
        print(f"FAIL {LOGIN} does not go to zitadel-login:3000 (got {by_path.get(LOGIN)})"); bad += 1
    if by_path.get(OPTIMIZER) != ("zitadel", 8080):
        print(f"FAIL {OPTIMIZER} is not sent to the server (got {by_path.get(OPTIMIZER)}): the image optimizer would be reachable"); bad += 1
    if by_path.get("/") != ("zitadel", 8080):
        print("FAIL the catch-all / no longer goes to zitadel:8080"); bad += 1
    others = set(by_path) - {LOGIN, OPTIMIZER, "/"}
    if others:
        print(f"FAIL unexpected path(s) on the Zitadel route: {sorted(others)} (register them in docs/public-exposure.yaml first)"); bad += 1
    eg = gateway_login_egress()
    if eg is None:
        print("FAIL CiliumNetworkPolicy openaether-gateway not found in apps/base/istio/networkpolicy.yaml"); bad += 1
    else:
        ports = sorted(p["port"] for e in eg for tp in e.get("toPorts", []) for p in tp["ports"])
        ns = {t["matchLabels"].get("io.kubernetes.pod.namespace") for e in eg for t in e["toEndpoints"]}
        if ports != ["3000"] or ns != {"services-identity"}:
            print(f"FAIL the Gateway egress to zitadel-login must be services-identity port 3000 only (got ports {ports}, namespaces {ns})"); bad += 1
    # Grafana calls Zitadel server-side by its public name, which is the Gateway (lab 25, D1)
    gcnp = [d for d in yaml.safe_load_all((REPO / "apps/base/observability/networkpolicy.yaml").read_text())
            if d and d.get("kind") == "CiliumNetworkPolicy" and d["spec"].get("endpointSelector", {}).get("matchLabels", {}).get("app.kubernetes.io/name") == "grafana"]
    to_gw = [e for d in gcnp for e in d["spec"].get("egress", [])
             if any(t.get("matchLabels", {}).get("io.kubernetes.pod.namespace") == "services-gateway" for t in e.get("toEndpoints", []))]
    gports = sorted(p["port"] for e in to_gw for tp in e.get("toPorts", []) for p in tp["ports"])
    if not gcnp or gports != ["443"]:
        print(f"FAIL Grafana's egress to the Gateway must be port 443 only (got {gports}): its token exchange goes through the public name"); bad += 1
    print("ok" if not bad else f"{bad} failure(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
